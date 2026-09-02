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
           "asked_at": "<iso>|null", "archived_at": "<iso>|null",
           "pin_id": "<pid>|null"}
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


def is_pending(rec: dict) -> bool:
    """Still waiting to be sent: not archived, and not already asked.

    This is what the 📋 badge counts. An asked task stays in the active list so
    you can see what you sent, but it's no longer queued work — counting it made
    the rail read as if there were things left to do when there weren't."""
    return not rec.get("archived_at") and not rec.get("asked_at")


def pending_count(session_id: str) -> int:
    """Badge count for one session (see is_pending)."""
    with _lock:
        return sum(1 for r in _load()["tasks"].get(session_id, []) if is_pending(r))


def list_tasks(session_id: str, archived: bool = False) -> list[dict]:
    """One session's tasks in list order (the sequence you plan to ask them in).

    Active tasks by default; pass archived=True for the archive. Archived
    records stay in the same list so unarchiving restores their place.
    """
    with _lock:
        items = _load()["tasks"].get(session_id, [])
        return [r for r in items if bool(r.get("archived_at")) == archived]


def add_task(session_id: str, text: str, asked: bool = False,
             pin_id: str | None = None) -> dict:
    """Queue a task. asked=True records it as already sent — that's how a
    message typed straight into the composer gets logged here.

    pin_id ties the task back to the pin that spawned it (see pins.add_pin):
    pinning a message queues the follow-up to send about it. The link is a
    label, not an owner — deleting either side leaves the other alone, because
    a task you have already edited is work in its own right.
    """
    rec = {
        "id": uuid.uuid4().hex[:12],
        "text": text.strip(),
        "created_at": _now(),
        "updated_at": _now(),
        "asked_at": _now() if asked else None,
        "archived_at": None,
        "pin_id": pin_id,
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
    """{session_id: pending task count} in one file read (board/rail badges).

    Asked and archived tasks are both out of the queue, so neither inflates the
    badge — see is_pending."""
    with _lock:
        out = {}
        for sid, items in _load()["tasks"].items():
            n = sum(1 for r in items if is_pending(r))
            if n:
                out[sid] = n
        return out


def rekey(old_id: str, new_id: str) -> None:
    """Move a session's *live* to-do list onto a new id (see tmuxio.reset).

    A /clear wipes the conversation, not the work — the queue of things you
    still meant to ask is exactly what you want to survive it.

    Only the pending ones travel. A task that was already asked, or archived,
    belongs to the conversation that dealt with it; carrying it over would put
    finished work back in the new session's queue and re-ask it. Those stay on
    the old id, which reset archives, so the record follows its own transcript.

    Appends, so a list already sitting on the new id isn't clobbered.
    """
    if old_id == new_id:
        return
    with _lock:
        data = _load()
        items = data["tasks"].get(old_id) or []
        moving = [r for r in items if is_pending(r)]
        if not moving:
            return
        staying = [r for r in items if not is_pending(r)]
        if staying:
            data["tasks"][old_id] = staying
        else:
            data["tasks"].pop(old_id, None)
        data["tasks"].setdefault(new_id, []).extend(moving)
        _save(data)
