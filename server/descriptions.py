"""
descriptions.py — user-written notes attached to a session.

A description is dashboard-only: what this session is *for*, in your words. The
transcript-derived title tells you what was said first; this tells you why the
session exists. Same shape and rules as overrides.py (session_id -> text, empty
text deletes the entry), kept in its own file so a note never risks the title
map.
"""

from __future__ import annotations

import json
import os
import threading

_PATH = os.path.join(os.path.dirname(__file__), ".descriptions.json")
_lock = threading.Lock()

# Long enough for a paragraph of context, short enough that the store stays a
# quick read on every board request.
MAX_LEN = 2000


def _load() -> dict[str, str]:
    try:
        with open(_PATH, "r", encoding="utf-8") as fh:
            return {str(k): str(v) for k, v in json.load(fh).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def _save(data: dict[str, str]) -> None:
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, _PATH)


def all_descriptions() -> dict[str, str]:
    with _lock:
        return _load()


def get(session_id: str):
    with _lock:
        return _load().get(session_id)


def set_description(session_id: str, text: str) -> None:
    """Write one session's note. Empty text removes it."""
    text = (text or "").strip()[:MAX_LEN]
    with _lock:
        data = _load()
        if text:
            data[session_id] = text
        else:
            data.pop(session_id, None)
        _save(data)


def clear(session_id: str) -> None:
    with _lock:
        data = _load()
        if data.pop(session_id, None) is not None:
            _save(data)


def rekey(old_id: str, new_id: str) -> None:
    """Move one session's entry onto a new id (see tmuxio.reset).

    A /clear gives the same live REPL a new session id. Moving rather than
    copying is deliberate: the old id is a frozen stub transcript, and leaving
    the entry behind would show it on the board wearing the same note.
    """
    with _lock:
        data = _load()
        if old_id in data and old_id != new_id:
            data[new_id] = data.pop(old_id)
            _save(data)
