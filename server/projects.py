"""
projects.py — user-defined groupings of sessions.

A Project is just a title + description with a set of member sessions. A session
may belong to zero, one, or many projects. Nothing here touches Claude Code's or
Antigravity's transcripts — the store is a single JSON file, fully reversible.

Shape of .projects.json:
    {
      "projects": {
        "<pid>": {"title": "...", "description": "...", "created_at": "<iso>"}
      },
      "tags": {"<session_id>": ["<pid>", ...]}
    }
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone

_PATH = os.path.join(os.path.dirname(__file__), ".projects.json")
_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    try:
        with open(_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"projects": {}, "tags": {}}
    data.setdefault("projects", {})
    data.setdefault("tags", {})
    return data


def _save(data: dict) -> None:
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, _PATH)


def _counts(data: dict) -> dict[str, int]:
    counts: dict[str, int] = {pid: 0 for pid in data["projects"]}
    for pids in data["tags"].values():
        for pid in pids:
            if pid in counts:
                counts[pid] += 1
    return counts


def _pub(pid: str, rec: dict, count: int) -> dict:
    return {
        "id": pid,
        "title": rec.get("title", ""),
        "description": rec.get("description", ""),
        "created_at": rec.get("created_at", ""),
        "session_count": count,
    }


def list_projects() -> list[dict]:
    """All projects with a live member count, newest first."""
    with _lock:
        data = _load()
        counts = _counts(data)
        out = [_pub(pid, rec, counts.get(pid, 0))
               for pid, rec in data["projects"].items()]
    out.sort(key=lambda p: p["created_at"], reverse=True)
    return out


def get_project(pid: str) -> dict | None:
    with _lock:
        data = _load()
        rec = data["projects"].get(pid)
        if rec is None:
            return None
        return _pub(pid, rec, _counts(data).get(pid, 0))


def create_project(title: str, description: str = "") -> dict:
    pid = uuid.uuid4().hex[:12]
    with _lock:
        data = _load()
        data["projects"][pid] = {
            "title": title.strip() or "Untitled project",
            "description": description.strip(),
            "created_at": _now(),
        }
        _save(data)
    return _pub(pid, data["projects"][pid], 0)


def update_project(pid: str, title: str | None = None,
                   description: str | None = None) -> bool:
    with _lock:
        data = _load()
        rec = data["projects"].get(pid)
        if rec is None:
            return False
        if title is not None:
            rec["title"] = title.strip() or rec.get("title", "Untitled project")
        if description is not None:
            rec["description"] = description.strip()
        _save(data)
    return True


def delete_project(pid: str) -> bool:
    with _lock:
        data = _load()
        if pid not in data["projects"]:
            return False
        del data["projects"][pid]
        # Drop the project from every session's tag list.
        for sid in list(data["tags"].keys()):
            data["tags"][sid] = [p for p in data["tags"][sid] if p != pid]
            if not data["tags"][sid]:
                del data["tags"][sid]
        _save(data)
    return True


def sessions_for(pid: str) -> list[str]:
    """Session ids tagged into this project."""
    with _lock:
        data = _load()
        return [sid for sid, pids in data["tags"].items() if pid in pids]


def projects_for(session_id: str) -> list[dict]:
    """Projects (id + title) this session is tagged into."""
    with _lock:
        data = _load()
        pids = data["tags"].get(session_id, [])
        return [{"id": p, "title": data["projects"][p]["title"]}
                for p in pids if p in data["projects"]]


def tags_by_session() -> dict[str, list[dict]]:
    """Whole tag map at once: {session_id: [{id, title}, ...]}. One file read —
    use this when decorating many sessions (board/search/triage) to avoid a
    per-session load."""
    with _lock:
        data = _load()
        projs = data["projects"]
        out: dict[str, list[dict]] = {}
        for sid, pids in data["tags"].items():
            out[sid] = [{"id": p, "title": projs[p]["title"]}
                        for p in pids if p in projs]
    return out


def tag(session_id: str, pid: str) -> bool:
    """Add the session to a project. False if the project doesn't exist."""
    with _lock:
        data = _load()
        if pid not in data["projects"]:
            return False
        pids = data["tags"].setdefault(session_id, [])
        if pid not in pids:
            pids.append(pid)
        _save(data)
    return True


def untag(session_id: str, pid: str) -> None:
    with _lock:
        data = _load()
        if session_id in data["tags"]:
            data["tags"][session_id] = [p for p in data["tags"][session_id] if p != pid]
            if not data["tags"][session_id]:
                del data["tags"][session_id]
        _save(data)
