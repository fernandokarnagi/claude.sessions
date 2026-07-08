"""
parser.py — turn Claude Code session transcripts into summaries and details.

Claude Code writes each session as JSONL at:
    ~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl

This module reads those files and exposes two functions:
    list_sessions(limit, offset)  -> dashboard summaries, most-recently-active first
    get_session(session_id)       -> full ordered detail for one session

All logic is pure/file-based so it can be unit-tested without a server.
"""

from __future__ import annotations

import fnmatch
import glob
import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional

from . import models

# ---- Tunable constants -------------------------------------------------------

# Location of Claude Code session transcripts. Override with the
# CLAUDE_PROJECTS_DIR environment variable; defaults to ~/.claude/projects.
PROJECTS_DIR = os.path.expanduser(
    os.environ.get("CLAUDE_PROJECTS_DIR", "~/.claude/projects")
)

# Throwaway summarizer runs use this cwd; sessions created there are internal
# to the dashboard and excluded from listings/search.
SUMMARIZER_CWD = os.path.expanduser("~/.claude_dashboard_summarizer")

# `/model` local-command stdout, e.g. "Set model to <b>glm-5.2:cloud</b>" or
# "Kept model as kimi-2.7:cloud" — captures the full routing name (with any
# `:cloud`/provider suffix) that assistant messages don't record.
_MODEL_CMD_RE = re.compile(r"model (?:to|as)\s+(\S+)", re.IGNORECASE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    """Remove ANSI SGR escapes (e.g. the bold codes around the model name)."""
    return _ANSI_RE.sub("", s).replace("</local-command-stdout>", "").strip()


def _resolve_model(model, full_by_base, last_full):
    """Best display name for the session's model.

    `model` is the resolved id from assistant messages (e.g. `glm-5.2`,
    `kimi-k2.7-code`, `gemma4:31b`, `claude-opus-4-8`) or None. Priority:
      1. The exact name the user set via /model this session (full_by_base),
         which keeps a `:cloud` suffix — e.g. set `glm-5.2:cloud`, messages
         report `glm-5.2`.
      2. A known launch name from the runclaude_*.sh registry — recovers the
         real cloud/local form (e.g. `gemma4:31b` -> `gemma4:31b-cloud`, while
         local `gemma4:12b` stays as-is).
      3. The resolved id as-is (no guessed suffix).
    """
    if not model:
        return last_full
    full = full_by_base.get(model.split(":")[0].lower())
    if full:
        return full
    return models.canonical(model) or model

# Status thresholds, in seconds, based on time since the file was last written.
# Tiers (each is the UPPER bound of that status):
THINKING_MAX_AGE = 30            # < 30s        -> THINKING (actively working)
WAITING_MAX_AGE = 30 * 60        # 30s … 30min  -> WAITING
SITTING_MAX_AGE = 2 * 3600       # 30min … 2h   -> SITTING
SLEEPING_MAX_AGE = 24 * 3600     # 2h … 24h     -> SLEEPING
# >= 24h -> ENDED (logs have no real end-marker; this is an idle assumption)

PREVIEW_LEN = 160              # truncation for activity/result previews

# ---- In-memory cache ---------------------------------------------------------
# Keyed by file path -> (mtime, size, summary_dict). Re-parse only on change.
_summary_cache: dict[str, tuple[float, int, dict]] = {}


# ---- Data shapes -------------------------------------------------------------

@dataclass
class Tokens:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_creation

    def as_dict(self) -> dict:
        d = asdict(self)
        d["total"] = self.total
        return d


# ---- Helpers -----------------------------------------------------------------

def _project_label(cwd: Optional[str], path: str) -> str:
    """Prefer the real cwd from events; fall back to decoding the dir name."""
    if cwd:
        return cwd
    raw = os.path.basename(os.path.dirname(path))
    return "/" + raw.lstrip("-").replace("-", "/")


def _iter_events(path: str):
    """Yield parsed JSON objects from a transcript, skipping bad lines."""
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _short(text: Any) -> str:
    s = str(text).strip().replace("\n", " ")
    return s[:PREVIEW_LEN]


def render_activity(evt: dict) -> Optional[dict]:
    """Render one event into {kind, ts, text}, or None if not displayable.

    kind is one of: user, assistant, thinking, tool, result.
    Assistant content blocks expand into multiple activities (newest logic
    keeps them in original order; callers slice as needed).
    """
    msg = evt.get("message")
    if not isinstance(msg, dict):
        return None
    role = msg.get("role")
    content = msg.get("content")
    ts = evt.get("timestamp")

    if isinstance(content, str):
        return {"kind": role, "ts": ts, "text": content}

    if isinstance(content, list):
        # Collapse a content list into a single representative activity line.
        # (Detail view expands every block; dashboard only needs a summary.)
        parts = []
        kind = role
        for b in content:
            bt = b.get("type")
            if bt == "text":
                kind = "assistant"; parts.append(b.get("text", ""))
            elif bt == "thinking":
                kind = "thinking"; parts.append("(thinking) " + b.get("thinking", ""))
            elif bt == "tool_use":
                kind = "tool"
                parts.append(f"→ {b.get('name')}: {json.dumps(b.get('input', {}))}")
            elif bt == "tool_result":
                kind = "result"
                res = b.get("content")
                if isinstance(res, list):
                    res = " ".join(x.get("text", "") for x in res if isinstance(x, dict))
                parts.append("← " + str(res))
        if parts:
            return {"kind": kind, "ts": ts, "text": _short(" ".join(parts))}
    return None


def render_blocks(evt: dict) -> list[dict]:
    """Expand one event into one-or-more activities for the detail view."""
    msg = evt.get("message")
    if not isinstance(msg, dict):
        return []
    role = msg.get("role")
    content = msg.get("content")
    ts = evt.get("timestamp")
    out: list[dict] = []

    if isinstance(content, str):
        return [{"kind": role, "ts": ts, "text": content}]

    if isinstance(content, list):
        for b in content:
            bt = b.get("type")
            if bt == "text":
                out.append({"kind": "assistant", "ts": ts, "text": b.get("text", "")})
            elif bt == "thinking":
                out.append({"kind": "thinking", "ts": ts, "text": b.get("thinking", "")})
            elif bt == "tool_use":
                out.append({
                    "kind": "tool", "ts": ts,
                    "name": b.get("name"),
                    "text": json.dumps(b.get("input", {}), indent=2),
                })
            elif bt == "tool_result":
                res = b.get("content")
                if isinstance(res, list):
                    res = "\n".join(x.get("text", "") for x in res if isinstance(x, dict))
                out.append({
                    "kind": "result", "ts": ts,
                    "is_error": bool(b.get("is_error")),
                    "text": str(res),
                })
    return out


def compute_status(mtime: float, now: Optional[float] = None) -> str:
    """THINKING / WAITING / SITTING / SLEEPING / ENDED from idle time."""
    now = now if now is not None else time.time()
    age = now - mtime
    if age < THINKING_MAX_AGE:
        return "THINKING"
    if age < WAITING_MAX_AGE:
        return "WAITING"
    if age < SITTING_MAX_AGE:
        return "SITTING"
    if age < SLEEPING_MAX_AGE:
        return "SLEEPING"
    return "ENDED"


def _status_with_turn(mtime: float, turn_pending: bool,
                      now: Optional[float] = None) -> str:
    """Age-based status, overlaid with the turn rule: THINKING iff the latest
    message isn't the assistant's. Mid-turn stays THINKING (unless truly ended);
    once the assistant has replied it's never THINKING (a fresh reply is WAITING).
    """
    base = compute_status(mtime, now)
    if turn_pending:
        return base if base == "ENDED" else "THINKING"
    return "WAITING" if base == "THINKING" else base


# ---- Summaries ---------------------------------------------------------------

def _build_summary(path: str) -> dict:
    """Full single-pass parse of a transcript into a dashboard summary."""
    session_id = os.path.splitext(os.path.basename(path))[0]
    mtime = os.path.getmtime(path)

    title = None
    cwd = None
    model = None          # resolved model id from assistant messages (e.g. glm-5.2)
    full_by_base = {}     # base -> full /model name, e.g. "glm-5.2" -> "glm-5.2:cloud"
    last_full = None      # most recent /model full name (fallback when no messages)
    entrypoint = None
    created_at = None
    last_ts = None
    last_role = None      # role of the last main-thread message (turn state)
    tokens = Tokens()
    recent: list[dict] = []  # rolling list of rendered activities

    for evt in _iter_events(path):
        etype = evt.get("type")
        if etype == "ai-title" and evt.get("aiTitle"):
            title = evt["aiTitle"]

        if evt.get("cwd") and not cwd:
            cwd = evt["cwd"]

        if evt.get("entrypoint"):
            entrypoint = evt["entrypoint"]

        # `/model` writes a local-command line ("Set model to X" / "Kept model
        # as X") carrying the *full* routing name incl. suffix like `:cloud` —
        # the assistant `message.model` only records the provider-resolved id
        # (e.g. alias `glm-5.2:cloud` resolves to `glm-5.2`). We keep the last
        # one that is a real model id (has a digit and a '-' or ':'), so a
        # no-arg /model check that prints a display name like "Opus 4.8", or a
        # trailing "… and saved as your default", doesn't poison it.
        if evt.get("type") == "system" and evt.get("subtype") == "local_command":
            mm = _MODEL_CMD_RE.search(evt.get("content") or "")
            if mm:
                tok = _strip_ansi(mm.group(1)).strip()
                if re.search(r"\d", tok) and ("-" in tok or ":" in tok):
                    full_by_base[tok.split(":")[0].lower()] = tok
                    last_full = tok

        ts = evt.get("timestamp")
        if ts:
            if created_at is None:
                created_at = ts
            last_ts = ts

        msg = evt.get("message")
        if isinstance(msg, dict):
            # Track the real running model. Skip "<synthetic>" (subagent/system
            # synthetic turns) — it isn't a model and would mask the true one.
            mv = msg.get("model")
            if mv and mv != "<synthetic>":
                model = mv

            # Track the last MAIN-thread message role (ignore subagent
            # sidechains). THINKING is strictly "the latest message is not from
            # the assistant" — i.e. the agent still owes a reply.
            role = msg.get("role")
            if role in ("user", "assistant") and not evt.get("isSidechain"):
                last_role = role
            usage = msg.get("usage")
            if isinstance(usage, dict):
                tokens.input += usage.get("input_tokens", 0) or 0
                tokens.output += usage.get("output_tokens", 0) or 0
                tokens.cache_read += usage.get("cache_read_input_tokens", 0) or 0
                tokens.cache_creation += usage.get("cache_creation_input_tokens", 0) or 0

            act = render_activity(evt)
            if act and act.get("text"):
                recent.append(act)
                if len(recent) > 2:
                    recent.pop(0)

        # Title fallback: first user message text
        if title is None and isinstance(msg, dict) and msg.get("role") == "user":
            c = msg.get("content")
            if isinstance(c, str) and c.strip():
                title = _short(c)

    disp_model = _resolve_model(model, full_by_base, last_full)

    # THINKING strictly means the latest message is NOT from the assistant (the
    # agent still owes a reply) — this holds even when a long tool run keeps the
    # transcript quiet. Once the assistant has replied, it's never THINKING.
    turn_pending = last_role is not None and last_role != "assistant"
    status = _status_with_turn(mtime, turn_pending)

    return {
        "session_id": session_id,
        "title": title or "(untitled session)",
        "project": _project_label(cwd, path),
        "cwd": cwd,
        "model": disp_model,
        "entrypoint": entrypoint or "cli",
        "status": status,
        "turn_pending": turn_pending,
        "created_at": created_at,
        "updated_at": last_ts or _iso(mtime),
        "tokens": tokens.as_dict(),
        "last_activities": recent,
    }


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _summary_for(path: str) -> Optional[dict]:
    """Cached summary: re-parse only when mtime/size changes. Status is always
    recomputed (it depends on wall-clock age, not file content)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    cached = _summary_cache.get(path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        summary = dict(cached[2])
    else:
        summary = _build_summary(path)
        _summary_cache[path] = (st.st_mtime, st.st_size, summary)
    # Recompute against wall-clock age, applying the turn rule (content-derived
    # turn_pending survives the recompute): THINKING iff latest msg not assistant.
    summary["status"] = _status_with_turn(st.st_mtime, summary.get("turn_pending", False))
    summary["mtime"] = st.st_mtime
    return summary


def list_sessions(limit: Optional[int] = 10, offset: int = 0,
                  statuses: Optional[set] = None,
                  archived_ids: Optional[set] = None,
                  archived_mode: str = "all") -> dict:
    """Return summaries sorted by last activity, newest first.

    limit=None means 'all'. `statuses` (a set like {"WAITING","SITTING"}) filters
    to only those statuses. `archived_mode` controls archived sessions:
    'all' (no filtering), 'exclude' (drop archived), or 'only' (archived only),
    using `archived_ids`. Returns {sessions, total} after filtering.
    """
    paths = glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl"))
    summaries = [s for s in (_summary_for(p) for p in paths)
                 if s and s.get("cwd") != SUMMARIZER_CWD]

    if archived_ids is not None and archived_mode != "all":
        if archived_mode == "exclude":
            summaries = [s for s in summaries if s["session_id"] not in archived_ids]
        elif archived_mode == "only":
            summaries = [s for s in summaries if s["session_id"] in archived_ids]

    # Sort by last activity (file mtime) so THINKING/WAITING sessions surface first.
    summaries.sort(key=lambda s: s.get("mtime") or 0, reverse=True)
    if statuses:
        summaries = [s for s in summaries if s.get("status") in statuses]
    total = len(summaries)
    window = summaries[offset:] if limit is None else summaries[offset: offset + limit]
    return {"sessions": window, "total": total}


def session_cwd(session_id: str) -> Optional[str]:
    """Return the working directory recorded for a session (for resume cwd)."""
    s = _summary_for_id(session_id)
    return s.get("cwd") if s else None


def get_summary(session_id: str) -> Optional[dict]:
    """Lightweight summary (no activities) for a single session; uses the cache."""
    return _summary_for_id(session_id)


def session_path(session_id: str) -> Optional[str]:
    """Absolute path to a session's transcript, or None."""
    matches = glob.glob(os.path.join(PROJECTS_DIR, "*", f"{session_id}.jsonl"))
    return matches[0] if matches else None


def tail_activities(path: str, offset: int) -> tuple[list[dict], int]:
    """Read transcript events written after byte `offset`.

    Returns (activities, new_offset) where activities are in chronological order
    (oldest first) and new_offset is the byte position after the last COMPLETE
    line consumed. A partial trailing line (no newline yet) is left for next time.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset
    if offset > size:
        return [], size  # file shrank/rotated -> resync pointer, skip content
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read()
    if not data:
        return [], offset
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        return [], offset  # no complete line yet
    consumed = data[: last_nl + 1]
    new_offset = offset + len(consumed)

    activities: list[dict] = []
    for raw in consumed.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            evt = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for block in render_blocks(evt):
            if block.get("text", "").strip() or block.get("name"):
                activities.append(block)
    return activities, new_offset


def session_mtime(session_id: str) -> Optional[float]:
    """Current transcript mtime for a session, or None if not found."""
    matches = glob.glob(os.path.join(PROJECTS_DIR, "*", f"{session_id}.jsonl"))
    try:
        return os.path.getmtime(matches[0]) if matches else None
    except OSError:
        return None


def _summary_for_id(session_id: str) -> Optional[dict]:
    matches = glob.glob(os.path.join(PROJECTS_DIR, "*", f"{session_id}.jsonl"))
    return _summary_for(matches[0]) if matches else None


def _field_match(q: str, fields: list) -> bool:
    """Match query `q` (already lowercased) against any of `fields`.

    If `q` contains a wildcard (* or ?), each field is matched as a glob
    pattern (e.g. 'build*', '*docker*'). Otherwise it's a case-insensitive
    substring ('contains') match.
    """
    wild = "*" in q or "?" in q
    for f in fields:
        if not f:
            continue
        f = str(f).lower()
        if wild:
            if fnmatch.fnmatch(f, q):
                return True
        elif q in f:
            return True
    return False


def search_sessions(query: str, limit: int = 50, extra_titles: dict | None = None) -> dict:
    """Find sessions by id, cwd/project path, auto title, or renamed title.

    Case-insensitive. Supports glob wildcards (* and ?) — see _field_match.
    `extra_titles` maps session_id -> override title so renamed sessions are
    searchable by their new name. Returns {sessions, total}, newest-active first.
    Empty query returns no results.
    """
    q = (query or "").strip().lower()
    if not q:
        return {"sessions": [], "total": 0}
    extra_titles = extra_titles or {}

    paths = glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl"))
    matches = []
    for p in paths:
        s = _summary_for(p)
        if not s or s.get("cwd") == SUMMARIZER_CWD:
            continue
        sid = s.get("session_id", "")
        fields = [
            sid,
            s.get("cwd"),
            s.get("project"),
            s.get("title"),
            extra_titles.get(sid),  # renamed/override title
        ]
        if _field_match(q, fields):
            matches.append(s)

    matches.sort(key=lambda s: s.get("mtime") or 0, reverse=True)
    return {"sessions": matches[:limit], "total": len(matches)}


def get_session(session_id: str) -> Optional[dict]:
    """Full detail for one session: summary header + events in DESC order."""
    matches = glob.glob(os.path.join(PROJECTS_DIR, "*", f"{session_id}.jsonl"))
    if not matches:
        return None
    path = matches[0]
    summary = _summary_for(path)
    if not summary:
        return None

    activities: list[dict] = []
    for evt in _iter_events(path):
        for block in render_blocks(evt):
            if block.get("text", "").strip() or block.get("name"):
                activities.append(block)
    activities.reverse()  # newest first

    detail = dict(summary)
    detail["activities"] = activities
    try:
        detail["file_size"] = os.path.getsize(path)  # tail starting offset
    except OSError:
        detail["file_size"] = 0
    return detail
