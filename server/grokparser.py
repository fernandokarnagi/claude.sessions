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
from . import subagents as claude_subagents   # reuse the current-task scope

GROK_DIR = os.path.expanduser(os.environ.get("GROK_CLI_DIR", "~/.grok"))
SESS_ROOT = os.path.join(GROK_DIR, "sessions")

# summary_dir cache: session_id -> session directory. Rebuilt when the set of
# on-disk sessions changes (cheap: one glob).
_DIR_CACHE: dict[str, str] = {}
# _summarize cache: dir -> (summary_mtime, size, summary dict)
_SUMM_CACHE: dict[str, tuple] = {}
# _update_times cache: dir -> (updates_mtime, size, prompt_ts, tool_ts)
_TS_CACHE: dict[str, tuple] = {}

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


def _update_times(session_dir: str) -> tuple[dict[int, str], dict[str, str]]:
    """Per-message wall-clock times, mined from updates.jsonl.

    chat_history.jsonl carries no timestamps, but the session's update stream
    does: a `user_message_chunk` stamps the prompt it belongs to (promptIndex),
    and every `tool_call`/`tool_call_update` stamps its toolCallId. Both keys
    also appear on chat_history turns, so those two maps pin exact times on
    user turns and tool results; the rest interpolate (see _activities).

    Returns (prompt_index -> iso, tool_call_id -> iso). Cached on file
    mtime+size — updates.jsonl is megabytes, and the board hits it per refresh.
    """
    path = os.path.join(session_dir, "updates.jsonl")
    try:
        st = os.stat(path)
    except OSError:
        return {}, {}
    cached = _TS_CACHE.get(session_dir)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2], cached[3]

    prompts: dict[int, str] = {}
    tools: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                # Cheap prefilter — most lines are hook/plan noise we'd only
                # parse to throw away.
                if "toolCallId" not in line and "user_message_chunk" not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                epoch = o.get("timestamp")
                if not isinstance(epoch, (int, float)):
                    continue
                upd = (o.get("params") or {}).get("update") or {}
                kind = upd.get("sessionUpdate")
                if kind == "user_message_chunk":
                    idx = (upd.get("_meta") or {}).get("promptIndex")
                    if isinstance(idx, int):
                        prompts.setdefault(idx, claude_parser._iso(epoch))
                elif kind in ("tool_call", "tool_call_update"):
                    cid = upd.get("toolCallId")
                    if cid:
                        tools.setdefault(cid, claude_parser._iso(epoch))
    except OSError:
        return {}, {}

    _TS_CACHE[session_dir] = (st.st_mtime, st.st_size, prompts, tools)
    return prompts, tools


def _fill_times(acts: list[dict], fallback: str | None) -> None:
    """Give every turn a ts. Turns pinned by _update_times keep theirs; the
    rest (assistant text, reasoning) take the next known time — they were
    written just before the tool call that follows them. Trailing turns with
    nothing after them take the previous known time, then the session's own."""
    nxt = None
    for a in reversed(acts):
        if a["ts"]:
            nxt = a["ts"]
        else:
            a["ts"] = nxt
    prev = fallback
    for a in acts:
        if a["ts"]:
            prev = a["ts"]
        else:
            a["ts"] = prev


def _activities(session_dir: str, limit: int, with_ts: bool = False) -> list[dict]:
    """Recent readable turns (chronological). Reads only the file's tail lines.

    with_ts also mines updates.jsonl for per-turn timestamps (detail view only —
    the board's 3-line preview doesn't show times and isn't worth the read)."""
    path = os.path.join(session_dir, "chat_history.jsonl")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    prompts, tools = _update_times(session_dir) if with_ts else ({}, {})
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
        ts = None
        if with_ts:
            if kind == "user":
                ts = prompts.get(o.get("prompt_index"))
            elif kind == "tool_result":
                ts = tools.get(o.get("tool_call_id"))
        # kind carries the role (user/assistant/thinking/tool_result) so the UI
        # labels each turn and styles assistant replies — not a flat "grok".
        acts.append({"kind": kind, "name": None, "ts": ts,
                     "role": kind, "text": text[:2000]})
    if with_ts:
        # Fill across the whole transcript, then trim — a kept turn's neighbour
        # may well be one that got sliced off.
        try:
            fallback = claude_parser._iso(os.path.getmtime(path))
        except OSError:
            fallback = None
        _fill_times(acts, fallback)
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
        _apply_subagents(s, session_dir)
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
    summary = dict(summary)
    _apply_subagents(summary, session_dir)
    return summary


def _apply_subagents(s: dict, session_dir: str) -> None:
    """Attach the running sub-agent count and re-age the status around it.

    summary.json does not move while a sub-agent runs — updates.jsonl does. Ageing
    off summary.json alone is what let a delegating session read WAITING.
    """
    live = [r for r in _subagent_map(session_dir).values() if r["running"]]
    running = len(live)
    s["subagents_running"] = running
    s["subagents_active"] = [r["description"] for r in live if r["description"]][:8]
    age_from = s["mtime"]
    if running:
        try:
            age_from = max(age_from, os.path.getmtime(_updates_path(session_dir)))
        except OSError:
            pass
    s["status"] = claude_parser.compute_status(age_from)


# ---- sub-agents --------------------------------------------------------------
#
# grok narrates its own delegation in updates.jsonl: `subagent_spawned` when a
# run starts, `subagent_finished` when it ends, both carrying the child session
# id. That is a far better signal than anything in chat_history.jsonl, which
# stays untouched for the whole run — the reason a delegating grok session used
# to sit on the board reading WAITING.
#
# updates.jsonl is append-only and can reach tens of thousands of lines, so the
# scan is incremental: parse only the bytes appended since the last look and
# fold them into the records already held.
# session_dir -> (bytes read, {id: record}, last prompt ts). The prompt time
# rides along because it comes off the same scan: updates.jsonl narrates the
# user's turns too, and it is what scopes the roster to the task in hand.
_SUB_CACHE: dict[str, tuple[int, dict, float | None]] = {}


def _updates_path(session_dir: str) -> str:
    return os.path.join(session_dir, "updates.jsonl")


def _subagents(session_dir: str) -> list[dict]:
    """Sub-agent runs of the session's current task, oldest first.

    Scoped to what was spawned since the last user prompt, plus anything still
    going: updates.jsonl holds every run the session ever made, and listing all
    of them turned a live-delegation panel into a log (see
    subagents.current_task).
    """
    runs = sorted(_subagent_map(session_dir).values(),
                  key=lambda r: r["started_at"] or 0)
    return claude_subagents.current_task(runs, since=_last_prompt(session_dir))


def _last_prompt(session_dir: str) -> float | None:
    """When this session's last user prompt landed, in seconds."""
    _subagent_map(session_dir)                # fills the cache this reads
    cached = _SUB_CACHE.get(session_dir)
    return cached[2] if cached else None


def _subagent_map(session_dir: str) -> dict:
    path = _updates_path(session_dir)
    try:
        size = os.path.getsize(path)
    except OSError:
        return {}
    offset, records, prompt_ts = _SUB_CACHE.get(session_dir, (0, {}, None))
    if offset == size:
        return records
    if offset > size:                    # truncated / rotated — start over
        offset, records, prompt_ts = 0, {}, None
    records = dict(records)
    # Binary mode: the cursor is a byte count, and text-mode seek() only accepts
    # opaque cookies from tell(), not a byte offset.
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            raw = fh.read()
    except OSError:
        return records
    # Leave a partial trailing line for the next pass rather than dropping it.
    cut = raw.rfind(b"\n")
    if cut == -1:
        return records
    raw = raw[: cut + 1]
    consumed = offset + len(raw)

    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except ValueError:
            continue
        upd = ((evt.get("params") or {}).get("update") or {})
        kind = upd.get("sessionUpdate")
        if kind == "user_message_chunk":
            ts = evt.get("timestamp")
            if isinstance(ts, (int, float)) and (prompt_ts is None or ts > prompt_ts):
                prompt_ts = float(ts)
            continue
        if kind not in ("subagent_spawned", "subagent_finished"):
            continue
        aid = upd.get("subagent_id") or upd.get("child_session_id")
        if not aid:
            continue
        ts = evt.get("timestamp")
        rec = records.setdefault(aid, {
            "agent_id": aid, "agent_type": "agent", "description": "",
            "running": True, "status": None, "started_at": None,
            "mtime": None, "turns": None, "duration_ms": None, "model": None,
        })
        if kind == "subagent_spawned":
            rec["agent_type"] = upd.get("subagent_type") or rec["agent_type"]
            rec["description"] = (upd.get("description") or "")[:120]
            rec["model"] = upd.get("model")
            rec["started_at"] = ts
            rec["mtime"] = ts
            rec["running"] = True
        else:
            rec["running"] = False
            rec["status"] = upd.get("status")
            rec["turns"] = upd.get("turns")
            rec["duration_ms"] = upd.get("duration_ms")
            rec["mtime"] = ts
    _SUB_CACHE[session_dir] = (consumed, records, prompt_ts)
    return records


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


def session_dir(sid: str) -> str | None:
    """Where grok stores this session — .../sessions/<enc-cwd>/<id>/.

    The parent of it is the project's folder, which is what a reset watches for
    the new session /new creates (see tmuxio.grok_reset).
    """
    return _dir_for(sid)


def get_summary(sid: str) -> dict | None:
    """Cheap cached board summary (no full activity list) for one session."""
    d = _dir_for(sid)
    return _summarize(d) if d else None


def list_sessions(keep_empty=frozenset()) -> list[dict]:
    """All grok sessions as dashboard summaries, newest activity first.

    Empty sessions are hidden — grok writes a session directory before the
    conversation, so an abandoned launch leaves one with no turns in it. Ids in
    `keep_empty` are shown anyway: the caller knows they are in use (a live
    REPL, a pin in the To-do inbox). That is the state /new leaves behind, and
    hiding it would take the session off the board along with the to-dos and
    pins reset just carried over to it.
    """
    out = []
    for sid, d in _session_dirs().items():
        s = _summarize(d)
        if s and (s["step_count"] > 0 or sid in keep_empty):
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
    acts = _activities(d, _MAX_DETAIL_ACT, with_ts=True)
    acts.reverse()   # newest first for the history view
    detail = dict(s)
    detail["activities"] = acts
    detail["subagents"] = _subagents(d)
    return detail
