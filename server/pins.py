"""
pins.py — per-session pinned messages.

A pin is a snapshot of one message from the history (user or assistant) that
you want to keep at hand: the decision, the command, the error you keep
scrolling back to. Unlike a task it is never sent anywhere — it is a bookmark,
so the only things you can do to it are read it and delete it.

The text is copied, not referenced. History is filtered, capped and re-fetched
on every poll, so a pin that pointed at an event index would drift; a copy is
still there after a /clear rekeys the session onto a new id.

Shape of .pins.json:
    {
      "pins": {
        "<session_id>": [
          {"id": "<pid>", "text": "...", "kind": "user|assistant",
           "ts": "<iso of the original message>|null", "created_at": "<iso>"}
        ]
      }
    }
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone

_PATH = os.path.join(os.path.dirname(__file__), ".pins.json")
_lock = threading.RLock()

# What a pin can be taken from. The history renders other kinds (tool, result,
# thinking), but pinning those is noise — anything worth keeping out of a tool
# run is quoted in the reply that follows it.
KINDS = ("user", "assistant")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    try:
        with open(_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"pins": {}}
    data.setdefault("pins", {})
    return data


def _save(data: dict) -> None:
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, _PATH)


def list_pins(session_id: str) -> list[dict]:
    """One session's pins, newest first — the one you just pinned is the one
    you are most likely to want back."""
    with _lock:
        items = list(_load()["pins"].get(session_id, []))
    return sorted(items, key=lambda r: r.get("created_at") or "", reverse=True)


def count(session_id: str) -> int:
    with _lock:
        return len(_load()["pins"].get(session_id, []))


def add_pin(session_id: str, text: str, kind: str = "assistant",
            ts: str | None = None) -> dict:
    """Pin a message. Re-pinning the same text is a no-op: the button sits on
    every message and stays there after a repaint, so a double click would
    otherwise stack duplicates."""
    text = text.strip()
    rec = {
        "id": uuid.uuid4().hex[:12],
        "text": text,
        "kind": kind if kind in KINDS else "assistant",
        "ts": ts or None,
        "created_at": _now(),
    }
    with _lock:
        data = _load()
        items = data["pins"].setdefault(session_id, [])
        for existing in items:
            if existing.get("text") == text:
                return existing
        items.append(rec)
        _save(data)
    return rec


def delete_pin(session_id: str, pid: str) -> bool:
    with _lock:
        data = _load()
        items = data["pins"].get(session_id, [])
        kept = [r for r in items if r.get("id") != pid]
        if len(kept) == len(items):
            return False
        if kept:
            data["pins"][session_id] = kept
        else:
            data["pins"].pop(session_id, None)
        _save(data)
    return True


def delete_all(session_id: str) -> int:
    """Unpin everything for one session. Returns how many went."""
    with _lock:
        data = _load()
        items = data["pins"].pop(session_id, [])
        if items:
            _save(data)
        return len(items)


def counts_by_session() -> dict[str, int]:
    """{session_id: pin count} in one file read (board / rail badges)."""
    with _lock:
        return {sid: len(items) for sid, items in _load()["pins"].items() if items}


def rekey(old_id: str, new_id: str) -> None:
    """Carry pins onto a new session id (see tmuxio.reset).

    All of them travel, unlike tasks: a pin is a note about the work, not a
    queued message, so none of it is "already dealt with" by the conversation
    that got cleared. Appends, so a list already on the new id survives.
    """
    if old_id == new_id:
        return
    with _lock:
        data = _load()
        items = data["pins"].pop(old_id, None)
        if not items:
            return
        data["pins"].setdefault(new_id, []).extend(items)
        _save(data)
