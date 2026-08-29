"""
agents.py — the read-only agent roster, read from Claude's own agents folder.

Claude Code already has one place where sub-agents are defined: markdown files
in ~/.claude/agents, each with YAML frontmatter naming the agent and describing
when to use it. Workflows used to keep a second roster of their own, which meant
the same persona was written twice and the two drifted apart. They don't any
more: a stage names agent ids, and those ids resolve here.

Nothing in this module writes. Adding, editing, or removing an agent is done by
editing the files — the same way Claude Code sees them — so there is exactly one
definition of any agent on this machine.

A file with no frontmatter is still listed: Claude Code ignores it, but the
operator plainly meant it as an agent by putting it there, and silently dropping
it would look like the dashboard had lost the file. Its id comes from the
filename and its description from the first prose line.
"""

from __future__ import annotations

import os
import re
import threading
import time

import yaml

# Overridable so tests can point at a scratch directory.
AGENTS_DIR = os.environ.get(
    "CLAUDE_AGENTS_DIR", os.path.expanduser("~/.claude/agents"))

# A single agent file is a system prompt, not a document store. Anything past
# this is not an agent, and reading it would cost more than it could be worth.
_MAX_BYTES = 256 * 1024
# Re-stat the directory at most this often; the roster is read on every editor
# paint and every stage compose.
_TTL = 2.0

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
_HEADING_RE = re.compile(r"^#+\s*")

_lock = threading.RLock()
_cache: dict = {"at": 0.0, "sig": None, "agents": []}


def _first_prose(body: str) -> str:
    """The first line that reads like a sentence — used as the description when
    the file has no frontmatter to take one from."""
    for line in body.splitlines():
        line = _HEADING_RE.sub("", line).strip()
        if line and not line.startswith(("```", "<!--", "---")):
            return line
    return ""


def _parse(path: str) -> dict | None:
    """One agent file → a roster entry, or None if it can't be read."""
    try:
        if os.path.getsize(path) > _MAX_BYTES:
            return None
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError):
        return None

    stem = os.path.splitext(os.path.basename(path))[0]
    meta: dict = {}
    body = text
    m = _FRONTMATTER_RE.match(text)
    if m:
        try:
            parsed = yaml.safe_load(m.group(1))
            if isinstance(parsed, dict):
                meta = parsed
        except yaml.YAMLError:
            meta = {}          # malformed frontmatter — treat as a plain file
        body = text[m.end():]

    name = str(meta.get("name") or "").strip() or stem
    tools = meta.get("tools")
    if isinstance(tools, list):
        tools = ", ".join(str(t).strip() for t in tools if str(t).strip())
    return {
        # The id is what a stage stores. Claude keys its agents by the
        # frontmatter name, so that is the id here too — filename only when
        # there is no frontmatter to take one from.
        "id": name,
        "name": name,
        "description": str(meta.get("description") or "").strip() or _first_prose(body),
        "model": str(meta.get("model") or "").strip(),
        "tools": str(tools or "").strip(),
        "prompt": body.strip(),
        "file": path,
        "declared": bool(m),   # False = no frontmatter, so Claude itself skips it
    }


def _signature(dirpath: str) -> tuple:
    """Cheap fingerprint of the folder: which .md files exist, and their mtimes.
    Changes whenever an agent is added, edited, or removed."""
    try:
        entries = sorted(
            (e.name, e.stat().st_mtime)
            for e in os.scandir(dirpath)
            if e.is_file() and e.name.endswith(".md")
        )
    except OSError:
        return ()
    return tuple(entries)


def list_agents(force: bool = False) -> list[dict]:
    """Every agent defined in the folder, sorted by name.

    Cached against the folder's contents+mtimes, so editing an agent file shows
    up on the next read without restarting the dashboard.
    """
    now = time.monotonic()
    with _lock:
        if not force and now - _cache["at"] < _TTL:
            return [dict(a) for a in _cache["agents"]]
        sig = _signature(AGENTS_DIR)
        if not force and sig == _cache["sig"]:
            _cache["at"] = now
            return [dict(a) for a in _cache["agents"]]
        agents = []
        seen: set[str] = set()
        for name, _mtime in sig:
            rec = _parse(os.path.join(AGENTS_DIR, name))
            if rec is None or rec["id"] in seen:
                continue           # two files claiming one name: first wins
            seen.add(rec["id"])
            agents.append(rec)
        agents.sort(key=lambda a: a["name"].lower())
        _cache.update({"at": now, "sig": sig, "agents": agents})
        return [dict(a) for a in agents]


def by_id() -> dict[str, dict]:
    return {a["id"]: a for a in list_agents()}


def get_many(ids: list[str]) -> tuple[list[dict], list[str]]:
    """(found agents, ids with no file) — in the order the stage asked for.

    Missing ids are returned rather than dropped: an agent file deleted after a
    workflow was written is something the operator has to see, not something to
    quietly compose around.
    """
    known = by_id()
    found, missing = [], []
    for aid in ids:
        rec = known.get(aid)
        (found.append(rec) if rec else missing.append(aid))
    return found, missing
