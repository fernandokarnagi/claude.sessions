"""
grokparser.py — read xAI `grok` CLI sessions for the dashboard (read-only).

`grok` stores each session as a directory:
    ~/.grok/sessions/<url-encoded-cwd>/<session-id>/
        summary.json         id, cwd, title, model, timestamps, message counts
        signals.json         metrics incl. context token usage
        chat_history.jsonl   {"type": ..., "content": ...} turns
        prompt_history.jsonl the user's raw prompts

Everything readable is already JSON — no protobuf guessing like agy. We mirror
parser.py's summary shape so grok sessions render on the same board alongside
Claude and Antigravity sessions, tagged with origin "grok".

Override the data dir with GROK_CLI_DIR (default ~/.grok).
"""

from __future__ import annotations

import glob
import json
import os

from . import parser as claude_parser   # reuse status thresholds + iso helper

GROK_DIR = os.path.expanduser(os.environ.get("GROK_CLI_DIR", "~/.grok"))
SESS_ROOT = os.path.join(GROK_DIR, "sessions")

# summary_dir cache: session_id -> session directory. Rebuilt when the set of
# on-disk sessions changes (cheap: one glob).
_DIR_CACHE: dict[str, str] = {}
# _summarize cache: dir -> (summary_mtime, size, summary dict)
_SUMM_CACHE: dict[str, tuple] = {}

_MAX_ACT = 3          # recent activities on a board summary
_MAX_DETAIL_ACT = 400 # activities in a full detail view


def _session_dirs() -> dict[str, str]:
    """Map session_id -> its directory. One glob over sessions/*/*/summary.json."""
    out: dict[str, str] = {}
    for sp in glob.glob(os.path.join(SESS_ROOT, "*", "*", "summary.json")):
        d = os.path.dirname(sp)
        out[os.path.basename(d)] = d
    _DIR_CACHE.clear()
    _DIR_CACHE.update(out)
    return out


def _dir_for(sid: str) -> str | None:
    d = _DIR_CACHE.get(sid)
    if d and os.path.isfile(os.path.join(d, "summary.json")):
        return d
    return _session_dirs().get(sid)


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _project_label(cwd: str | None) -> str:
    if not cwd:
        return "grok"
    return os.path.basename(cwd.rstrip("/")) or cwd


def _text_of(content) -> str:
    """Flatten a grok chat_history `content` (str | list[{type,text}]) to text."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict):
                t = blk.get("text") or blk.get("content") or ""
                if isinstance(t, str) and t.strip():
                    parts.append(t.strip())
        return "\n".join(parts).strip()
    return ""


import re as _re

# The real prompt grok wraps in <user_query>…</user_query>; a <user_info>…
# </user_info> block (env/context) rides along on the first turn.
_USER_QUERY_RE = _re.compile(r"<user_query>(.*?)</user_query>", _re.DOTALL)


def _strip_user_info(text: str) -> str:
    """Return just the human's prompt from a grok user turn.

    Grok wraps the actual message in <user_query>…</user_query> and prepends a
    <user_info>…</user_info> context block. Prefer the query's inner content;
    otherwise drop the info block and return what's left."""
    m = _USER_QUERY_RE.search(text)
    if m:
        return m.group(1).strip()
    # No query wrapper → this "user" turn is injected context (a
    # <system-reminder> / <user_info> block), not a human prompt. Drop it.
    if "<system-reminder>" in text or "<user_info>" in text:
        return ""
    return text.strip()


def _prompts(session_dir: str) -> list[str]:
    """User prompts (newest last) from prompt_history.jsonl."""
    out = []
    path = os.path.join(session_dir, "prompt_history.jsonl")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                p = (o.get("prompt") or "").strip()
                if p:
                    out.append(p)
    except OSError:
        pass
    return out


# chat_history types worth surfacing as activities, mapped to a display kind.
_ACT_KINDS = {
    "user": "user",
    "assistant": "assistant",
    "reasoning": "thinking",
    "tool_result": "tool_result",
}


def _activities(session_dir: str, limit: int) -> list[dict]:
    """Recent readable turns (chronological). Reads only the file's tail lines."""
    path = os.path.join(session_dir, "chat_history.jsonl")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    acts: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        kind = _ACT_KINDS.get(o.get("type"))
        if kind is None:
            continue
        text = _text_of(o.get("content"))
        if kind == "user":
            text = _strip_user_info(text)
        if not text:
            continue
        # kind carries the role (user/assistant/thinking/tool_result) so the UI
        # labels each turn and styles assistant replies — not a flat "grok".
        acts.append({"kind": kind, "name": None,
                     "role": kind, "text": text[:2000]})
    return acts[-limit:] if limit else acts


def _tokens(signals: dict) -> dict:
    """Map grok signals to the dashboard token shape. Grok reports context usage,
    not an input/output split, so total carries the context tokens used."""
    total = int(signals.get("contextTokensUsed") or 0)
    return {"input": 0, "output": 0, "cache_read": 0,
            "cache_creation": 0, "total": total}


def _summarize(session_dir: str) -> dict | None:
    sp = os.path.join(session_dir, "summary.json")
    try:
        st = os.stat(sp)
    except OSError:
        return None
    cached = _SUMM_CACHE.get(session_dir)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        s = dict(cached[2])
        s["status"] = claude_parser.compute_status(s["mtime"])
        return s

    info = _load_json(sp)
    meta = info.get("info", {}) if isinstance(info.get("info"), dict) else {}
    sid = meta.get("id") or os.path.basename(session_dir)
    cwd = meta.get("cwd")
    signals = _load_json(os.path.join(session_dir, "signals.json"))

    title = (info.get("generated_title") or info.get("session_summary") or "").strip()
    if not title:
        prompts = _prompts(session_dir)
        title = prompts[0] if prompts else ""

    # Prefer the transcript's own updated_at; fall back to file mtime.
    upd = info.get("updated_at") or info.get("last_active_at")
    mtime = _iso_to_epoch(upd) or st.st_mtime
    created = info.get("created_at") or claude_parser._iso(st.st_ctime)

    summary = {
        "session_id": sid,
        "title": title or "(grok session)",
        "project": _project_label(cwd),
        "cwd": cwd,
        "model": info.get("current_model_id"),
        "entrypoint": "grok",
        "origin": "grok",
        "source": "grok",
        "status": claude_parser.compute_status(mtime),
        "turn_pending": False,
        "created_at": created,
        "updated_at": claude_parser._iso(mtime),
        "mtime": mtime,
        "tokens": _tokens(signals),
        "step_count": int(info.get("num_messages") or 0),
        "last_activities": _activities(session_dir, _MAX_ACT),
        "live_tmux": False,
        "live": False,
        "live_web": False,
        "pending_approval": False,
        "archived": False,
        "attention": False,
        "renamed": False,
        "autonomy": "manual",
    }
    _SUMM_CACHE[session_dir] = (st.st_mtime, st.st_size, summary)
    return dict(summary)


def _iso_to_epoch(s: str | None) -> float | None:
    """Parse grok's RFC3339 timestamps (e.g. 2026-07-23T07:27:40.155205Z)."""
    if not s:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


# ---- public API (mirrors agyparser) -----------------------------------------

def session_ids() -> set[str]:
    return set(_session_dirs().keys())


def has_session(sid: str) -> bool:
    return _dir_for(sid) is not None


def get_summary(sid: str) -> dict | None:
    """Cheap cached board summary (no full activity list) for one session."""
    d = _dir_for(sid)
    return _summarize(d) if d else None


def list_sessions() -> list[dict]:
    """All grok sessions as dashboard summaries, newest activity first."""
    out = []
    for sid, d in _session_dirs().items():
        s = _summarize(d)
        if s and s["step_count"] > 0:      # skip empty/never-used sessions
            out.append(s)
    out.sort(key=lambda x: x.get("mtime") or 0, reverse=True)
    return out


def usage_text(sid: str) -> dict:
    """A read-only usage panel built from grok's signals.json — no REPL scrape.

    grok records rich per-session stats on disk (token/context usage, turn and
    tool counts, latency, model), so we format them directly instead of driving
    a live /usage overlay. Returns {"ok": bool, "text"|"error": str}."""
    d = _dir_for(sid)
    if not d:
        return {"ok": False, "error": "grok session not found"}
    sig = _load_json(os.path.join(d, "signals.json"))
    info = _load_json(os.path.join(d, "summary.json"))
    if not sig:
        return {"ok": False, "error": "no signals.json (session hasn't started?)"}

    def _int(k):
        try:
            return int(sig.get(k) or 0)
        except (TypeError, ValueError):
            return 0

    used = _int("contextTokensUsed")
    win = _int("contextWindowTokens")
    pct = sig.get("contextWindowUsage")
    dur = _int("sessionDurationSeconds")
    mins, secs = divmod(dur, 60)
    tools = ", ".join(sig.get("toolsUsed") or []) or "—"
    models = ", ".join(sig.get("modelsUsed") or []) or (info.get("current_model_id") or "—")

    lines = [
        f"Model:            {models}",
        f"Reasoning:        {info.get('reasoning_effort') or '—'}",
        "",
        f"Context usage:    {used:,} / {win:,} tokens"
        + (f"  ({pct}%)" if pct is not None else ""),
        f"Turns:            {_int('turnCount')}",
        f"Messages:         {_int('userMessageCount')} user / {_int('assistantMessageCount')} assistant",
        f"Tool calls:       {_int('toolCallCount')}  (failures {_int('toolFailureCount')})",
        f"Tools used:       {tools}",
        f"Compactions:      {_int('compactionCount')}",
        f"Errors:           {_int('errorCount')}",
        "",
        f"Session duration: {mins}m {secs}s",
        f"Avg first token:  {_int('avgTimeToFirstTokenMs')} ms",
        f"Avg response:     {_int('avgResponseTimeMs')} ms",
        f"Lines added:      {_int('agentLinesAdded')}",
    ]
    return {"ok": True, "text": "\n".join(lines)}


def get_session(sid: str) -> dict | None:
    """Full detail: summary header + activity list (newest first)."""
    d = _dir_for(sid)
    if not d:
        return None
    s = _summarize(d)
    if not s:
        return None
    acts = _activities(d, _MAX_DETAIL_ACT)
    acts.reverse()   # newest first for the history view
    detail = dict(s)
    detail["activities"] = acts
    return detail
