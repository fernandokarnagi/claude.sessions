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


def rekey(old_id: str, new_id: str) -> None:
    """Move a pin onto a new id (see tmuxio.reset).

    Pinning a session to the To-do inbox is a statement about the work, not the
    conversation, so a /clear shouldn't quietly drop it out of the list you
    triage from. Unpinning the old id is half the point: it's a frozen stub
    afterwards and has no business sitting in an inbox of things that need you.
    """
    if old_id == new_id:
        return
    with _lock:
        ids = _load()
        if old_id not in ids:
            return
        ids.discard(old_id)
        ids.add(new_id)
        _save(ids)
