"""
registry.py — records the last time the WEB app drove each session.

We store, per session, the transcript file mtime captured at the end of the
most recent web-driven turn. On each poll the app compares the session's
current mtime to this value: if the file has been written *since* our web turn
(e.g. the CLI resumed it), the session is no longer "web" — it flips back to
its transcript origin (cli/vscode). Persisted to JSON so it survives restarts.
"""

from __future__ import annotations

import json
import os
import threading

_PATH = os.path.join(os.path.dirname(__file__), ".web_sessions.json")
_lock = threading.Lock()


def _load() -> dict[str, float]:
    try:
        with open(_PATH, "r", encoding="utf-8") as fh:
            return {k: float(v) for k, v in json.load(fh).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def _save(data: dict[str, float]) -> None:
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, _PATH)


def web_mtimes() -> dict[str, float]:
    with _lock:
        return _load()


def get_web_mtime(session_id: str):
    with _lock:
        return _load().get(session_id)


def set_web_mtime(session_id: str, mtime: float) -> None:
    """Record that the web app last drove this session at the given mtime."""
    if mtime is None:
        return
    with _lock:
        data = _load()
        data[session_id] = float(mtime)
        _save(data)
