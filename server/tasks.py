"""
tasks.py — per-session task list.

A task is a canned message you intend to ask a session. It lives only in this
dashboard: nothing is sent anywhere until you press "Ask" in the UI, which drops
the text into the composer and submits it.

Shape of .tasks.json:
    {
      "tasks": {
        "<session_id>": [
          {"id": "<tid>", "text": "...", "created_at": "<iso>", "updated_at": "<iso>",
           "asked_at": "<iso>|null", "archived_at": "<iso>|null"}
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

_PATH = os.path.join(os.path.dirname(__file__), ".tasks.json")
_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    try:
        with open(_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"tasks": {}}
    data.setdefault("tasks", {})
    return data


def _save(data: dict) -> None:
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, _PATH)


def list_tasks(session_id: str, archived: bool = False) -> list[dict]:
    """One session's tasks in list order (the sequence you plan to ask them in).

    Active tasks by default; pass archived=True for the archive. Archived
    records stay in the same list so unarchiving restores their place.
    """
    with _lock:
        items = _load()["tasks"].get(session_id, [])
        return [r for r in items if bool(r.get("archived_at")) == archived]


def add_task(session_id: str, text: str, asked: bool = False) -> dict:
    """Queue a task. asked=True records it as already sent — that's how a
    message typed straight into the composer gets logged here."""
    rec = {
        "id": uuid.uuid4().hex[:12],
        "text": text.strip(),
        "created_at": _now(),
        "updated_at": _now(),
        "asked_at": _now() if asked else None,
        "archived_at": None,
    }
    with _lock:
        data = _load()
        data["tasks"].setdefault(session_id, []).append(rec)
        _save(data)
    return rec


def update_task(session_id: str, tid: str, text: str | None = None,
                asked: bool = False) -> dict | None:
    with _lock:
        data = _load()
        for rec in data["tasks"].get(session_id, []):
            if rec.get("id") != tid:
                continue
            if text is not None:
                rec["text"] = text.strip()
            if asked:
                rec["asked_at"] = _now()
            rec["updated_at"] = _now()
            _save(data)
            return rec
    return None


def move_task(session_id: str, tid: str, delta: int) -> list[dict] | None:
    """Shift one active task `delta` places in the ask sequence.

    Steps are counted over the *visible* (non-archived) tasks so an archived
    record sitting between two active ones doesn't swallow a move. Returns the
    new active list, or None if the task isn't there. Moves past either end
    clamp (no wrap-around).
    """
    with _lock:
        data = _load()
        items = data["tasks"].get(session_id, [])
        visible = [i for i, r in enumerate(items) if not r.get("archived_at")]
        pos = next((p for p, i in enumerate(visible)
                    if items[i].get("id") == tid), None)
        if pos is None:
            return None
        new_pos = max(0, min(len(visible) - 1, pos + delta))
        if new_pos != pos:
            rec = items.pop(visible[pos])
            # Re-index against the list minus the record we just removed. Moving
            # up lands before the target, which keeps its index; moving down
            # lands after it, and every later target shifted down by one.
            rest = [i for i, r in enumerate(items) if not r.get("archived_at")]
            at = rest[new_pos] if new_pos < pos else rest[new_pos - 1] + 1
            items.insert(at, rec)
            _save(data)
        return [r for r in items if not r.get("archived_at")]


def set_archived(session_id: str, tid: str, archived: bool) -> dict | None:
    """Archive or restore one task. Archived tasks keep their list position."""
    with _lock:
        data = _load()
        for rec in data["tasks"].get(session_id, []):
            if rec.get("id") != tid:
                continue
            rec["archived_at"] = _now() if archived else None
            rec["updated_at"] = _now()
            _save(data)
            return rec
    return None


def delete_archived(session_id: str) -> int:
    """Empty one session's task archive. Returns how many were deleted.

    Active tasks are untouched — only records carrying an archived_at go.
    """
    with _lock:
        data = _load()
        items = data["tasks"].get(session_id, [])
        kept = [r for r in items if not r.get("archived_at")]
        removed = len(items) - len(kept)
        if removed:
            if kept:
                data["tasks"][session_id] = kept
            else:
                data["tasks"].pop(session_id, None)
            _save(data)
        return removed


def delete_task(session_id: str, tid: str) -> bool:
    with _lock:
        data = _load()
        items = data["tasks"].get(session_id, [])
        kept = [r for r in items if r.get("id") != tid]
        if len(kept) == len(items):
            return False
        if kept:
            data["tasks"][session_id] = kept
        else:
            data["tasks"].pop(session_id, None)
        _save(data)
    return True


def counts_by_session() -> dict[str, int]:
    """{session_id: active task count} in one file read (board/rail badges).

    Archived tasks are out of the way, so they don't inflate the badge."""
    with _lock:
        out = {}
        for sid, items in _load()["tasks"].items():
            n = sum(1 for r in items if not r.get("archived_at"))
            if n:
                out[sid] = n
        return out
