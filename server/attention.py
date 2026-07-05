"""
attention.py — tracks sessions the user has manually flagged for attention.

The Attention page shows live-tmux sessions plus any the user explicitly marks
here (so a session with no live REPL can still be pinned to that inbox). Marking
never touches Claude Code's transcripts; the set is persisted to JSON and is
fully reversible.
"""

from __future__ import annotations

import json
import os
import threading

_PATH = os.path.join(os.path.dirname(__file__), ".attention.json")
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


def marked_ids() -> set[str]:
    with _lock:
        return _load()


def is_marked(session_id: str) -> bool:
    with _lock:
        return session_id in _load()


def set_marked(session_id: str, marked: bool) -> None:
    with _lock:
        ids = _load()
        if marked:
            ids.add(session_id)
        else:
            ids.discard(session_id)
        _save(ids)
