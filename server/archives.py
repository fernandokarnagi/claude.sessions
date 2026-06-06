"""
archives.py — tracks sessions the user has archived.

Archived sessions are hidden from the normal dashboard/needs-attention listings
and collected in the board's ARCHIVED lane. Archiving never touches Claude
Code's transcripts; the set is persisted to JSON and is fully reversible.
"""

from __future__ import annotations

import json
import os
import threading

_PATH = os.path.join(os.path.dirname(__file__), ".archived.json")
_lock = threading.Lock()


def _load() -> set[str]:
    try:
        with open(_PATH, "r", encoding="utf-8") as fh:
            return set(json.load(fh))
    except (OSError, ValueError):
        return set()


def _save(ids: set[str]) -> None:
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(sorted(ids), fh)
    os.replace(tmp, _PATH)


def archived_ids() -> set[str]:
    with _lock:
        return _load()


def is_archived(session_id: str) -> bool:
    with _lock:
        return session_id in _load()


def set_archived(session_id: str, archived: bool) -> None:
    with _lock:
        ids = _load()
        if archived:
            ids.add(session_id)
        else:
            ids.discard(session_id)
        _save(ids)
