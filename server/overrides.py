"""
overrides.py — user-defined title overrides for sessions.

Renaming in the dashboard never touches Claude Code's transcripts. Instead we
keep a session_id -> custom title map here, persisted to JSON. The app layer
applies the override on top of the transcript-derived title.
"""

from __future__ import annotations

import json
import os
import threading

_PATH = os.path.join(os.path.dirname(__file__), ".title_overrides.json")
_lock = threading.Lock()


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


def all_titles() -> dict[str, str]:
    with _lock:
        return _load()


def get_title(session_id: str):
    with _lock:
        return _load().get(session_id)


def set_title(session_id: str, title: str) -> None:
    title = (title or "").strip()
    with _lock:
        data = _load()
        if title:
            data[session_id] = title
        else:
            data.pop(session_id, None)  # empty title -> revert to original
        _save(data)


def clear_title(session_id: str) -> None:
    with _lock:
        data = _load()
        if data.pop(session_id, None) is not None:
            _save(data)
