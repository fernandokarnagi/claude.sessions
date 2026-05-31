"""
summaries.py — cache of "what's expected from you" summaries per session.

Keyed by session_id, storing the transcript mtime the summary was generated
for plus the summary text. The mtime acts as the waiting-episode key: if the
session works again (mtime advances) and later re-waits, the cached summary is
stale and gets regenerated. Persisted to JSON.
"""

from __future__ import annotations

import json
import os
import threading

_PATH = os.path.join(os.path.dirname(__file__), ".waiting_summaries.json")
_lock = threading.Lock()


def _load() -> dict:
    try:
        with open(_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, _PATH)


def get(session_id: str, mtime: float):
    """Return the cached summary text if it matches the current mtime, else None."""
    with _lock:
        entry = _load().get(session_id)
    if entry and abs(float(entry.get("mtime", -1)) - float(mtime)) < 1.0:
        return entry.get("summary")
    return None


def set(session_id: str, mtime: float, summary: str) -> None:
    with _lock:
        data = _load()
        data[session_id] = {"mtime": float(mtime), "summary": summary}
        _save(data)
