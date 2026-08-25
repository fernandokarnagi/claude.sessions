"""subagents.py — sub-agent runs that belong to a Claude Code session.

Claude Code does NOT write sub-agent turns into the session transcript. It
writes the `Agent`/`Task` tool_use into the main JSONL, then parks the whole
sub-agent conversation in a sibling directory:

    ~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl      <- main transcript
    ~/.claude/projects/<encoded-cwd>/<session-uuid>/subagents/
        agent-<id>.jsonl        <- the sub-agent's own turns
        agent-<id>.meta.json    <- {"agentType", "description", "toolUseId"}

That split is why a session delegating to sub-agents looked idle: the main file
gets no writes for the whole run, so its mtime — the dashboard's only liveness
signal — went stale and the status decayed to WAITING while work was very much
happening.

This module reads that directory. `for_session` returns one record per
sub-agent run; `latest_mtime` gives the freshest write across them, which the
parser folds into the status age so a delegating session stays live.

Whether a run is still going is NOT read from here — a sub-agent transcript has
no end marker. It comes from the main transcript: an `Agent`/`Task` tool_use
whose `tool_result` hasn't landed yet is still running (see parser._build_summary,
which collects those ids into `open_agent_calls`).
"""

from __future__ import annotations

import json
import os

# meta.json cache: path -> (mtime, parsed dict). The file is written once when
# the run starts and never touched again, so this effectively never re-reads.
_meta_cache: dict[str, tuple[float, dict]] = {}

# Tool names that spawn a sub-agent. Older Claude Code called it Task; current
# builds call it Agent. Both appear in transcripts on disk.
AGENT_TOOLS = {"Agent", "Task"}

_MAX_DESC = 120


def dir_for(transcript_path: str) -> str:
    """The subagents directory belonging to a transcript (may not exist)."""
    return os.path.join(os.path.splitext(transcript_path)[0], "subagents")


def _read_meta(path: str) -> dict:
    try:
        st = os.stat(path)
    except OSError:
        return {}
    cached = _meta_cache.get(path)
    if cached and cached[0] == st.st_mtime:
        return cached[1]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    _meta_cache[path] = (st.st_mtime, meta)
    return meta


def _first_prompt(path: str) -> str | None:
    """The sub-agent's opening instruction — its description when meta has none."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except ValueError:
                    continue
                msg = evt.get("message")
                if not isinstance(msg, dict) or msg.get("role") != "user":
                    continue
                c = msg.get("content")
                if isinstance(c, str) and c.strip():
                    return c.strip()[:_MAX_DESC]
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip():
                            return b["text"].strip()[:_MAX_DESC]
                return None
    except OSError:
        pass
    return None


def _count_turns(path: str) -> int:
    """Lines in a sub-agent transcript — one per turn, close enough for a badge."""
    try:
        with open(path, "rb") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


def for_session(transcript_path: str, open_ids: set[str] | None = None,
                deep: bool = False) -> list[dict]:
    """Sub-agent runs for one session, oldest first.

    `open_ids` are the tool_use ids of Agent/Task calls the main transcript has
    not yet received a result for — those runs are marked `running`. `deep`
    also counts turns and recovers a description from the transcript when the
    meta file has none; the board skips it (one extra file read per run, over
    every session, on a 1.5s poll).
    """
    open_ids = open_ids or set()
    d = dir_for(transcript_path)
    out: list[dict] = []
    try:
        entries = [e for e in os.scandir(d) if e.name.endswith(".jsonl")]
    except OSError:
        return out                       # no subagents dir — the common case

    for e in entries:
        agent_id = e.name[:-len(".jsonl")]
        meta = _read_meta(os.path.join(d, agent_id + ".meta.json"))
        tool_id = meta.get("toolUseId")
        try:
            st = e.stat()
        except OSError:
            continue
        desc = (meta.get("description") or "").strip()
        if not desc and deep:
            desc = _first_prompt(e.path) or ""
        out.append({
            "agent_id": agent_id.replace("agent-", "", 1),
            "agent_type": meta.get("agentType") or "agent",
            "description": desc[:_MAX_DESC],
            "tool_use_id": tool_id,
            "running": bool(tool_id) and tool_id in open_ids,
            "mtime": st.st_mtime,
            "started_at": st.st_ctime,
            "turns": _count_turns(e.path) if deep else None,
        })
    out.sort(key=lambda s: s["started_at"])
    return out


def latest_mtime(transcript_path: str) -> float:
    """Freshest write across a session's sub-agent transcripts, 0 if there are none.

    Deliberately its own stat sweep rather than a by-product of `for_session`:
    the board calls this on every session, every poll, and one scandir with no
    JSON parsing is what keeps that affordable.
    """
    d = dir_for(transcript_path)
    newest = 0.0
    try:
        for e in os.scandir(d):
            if not e.name.endswith(".jsonl"):
                continue
            try:
                m = e.stat().st_mtime
            except OSError:
                continue
            if m > newest:
                newest = m
    except OSError:
        return 0.0
    return newest
