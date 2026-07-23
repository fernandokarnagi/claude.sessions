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
           "asked_at": "<iso>|null"}
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


def list_tasks(session_id: str) -> list[dict]:
    """Tasks for one session, oldest first (the order you added them)."""
    with _lock:
        return list(_load()["tasks"].get(session_id, []))


def add_task(session_id: str, text: str) -> dict:
    rec = {
        "id": uuid.uuid4().hex[:12],
        "text": text.strip(),
        "created_at": _now(),
        "updated_at": _now(),
        "asked_at": None,
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
    """{session_id: task count} in one file read (for board/rail badges)."""
    with _lock:
        return {sid: len(items) for sid, items in _load()["tasks"].items() if items}
