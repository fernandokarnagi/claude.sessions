"""
opencodeparser.py — read `opencode` CLI sessions for the dashboard (read-only).

Unlike claude (one .jsonl per session), agy (one .db per conversation) and grok
(one directory per session), opencode keeps *every* session in a single SQLite
database:

    ~/.local/share/opencode/opencode.db

    session   id, project_id, directory, title, model, agent, cost, tokens_*,
              time_created, time_updated, time_archived, parent_id
    message   id, session_id, data  — role + per-turn token/time envelope
    part      id, message_id, session_id, time_created, data  — the actual
              content: text / reasoning / tool / step-start / step-finish / patch

So "does this session exist" is a query, not an os.path.isfile, and there is no
per-session mtime to key a cache on — `session.time_updated` stands in for it.

The db is live: opencode holds it open in WAL mode while a session runs. Every
connection here is opened read-only (`mode=ro`) so the dashboard's poll loop can
never take a write lock on a session someone is actually using.

We mirror parser.py's summary shape so opencode sessions render on the same
board alongside Claude, Antigravity and grok sessions, tagged with origin
"opencode".

Override the data dir with OPENCODE_DATA_DIR (default ~/.local/share/opencode).
"""

from __future__ import annotations

import json
import os
import sqlite3

from . import parser as claude_parser   # reuse status thresholds + iso helper

DATA_DIR = os.path.expanduser(
    os.environ.get("OPENCODE_DATA_DIR", "~/.local/share/opencode"))

_MAX_ACT = 3           # recent activities on a board summary
_MAX_DETAIL_ACT = 400  # activities in a full detail view

# _summarize cache: session_id -> (time_updated, summary dict). opencode bumps
# time_updated on every write, so it invalidates exactly like an mtime would.
_SUMM_CACHE: dict[str, tuple] = {}

# part types that carry something worth showing, mapped to a display kind.
# step-start / step-finish are turn bookkeeping and patch is a diff blob — all
# noise in a transcript view.
_PART_KINDS = {
    "text": "text",            # resolved to user/assistant by the parent message
    "reasoning": "thinking",
    "tool": "tool",
}


def db_path() -> str:
    return os.path.join(DATA_DIR, "opencode.db")


def _connect() -> sqlite3.Connection | None:
    """A read-only connection to opencode's store, or None if there isn't one.

    Read-only matters: opencode may be running against this same file, and a
    dashboard poll must never contend for its write lock.
    """
    path = db_path()
    if not os.path.isfile(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _query(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    conn = _connect()
    if conn is None:
        return []
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _ms(epoch_ms) -> float:
    """opencode stamps milliseconds; the rest of the dashboard speaks seconds."""
    try:
        return float(epoch_ms) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def _loads(raw) -> dict:
    try:
        o = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return o if isinstance(o, dict) else {}


def _project_label(cwd: str | None) -> str:
    if not cwd:
        return "opencode"
    return os.path.basename(cwd.rstrip("/")) or cwd


def _model_label(raw: str | None) -> str | None:
    """session.model is JSON — {"id": "...", "providerID": "..."} — so render it
    the way opencode's own `--model` flag takes it: provider/model."""
    m = _loads(raw)
    mid = m.get("id") or m.get("modelID")
    if not mid:
        return None
    prov = m.get("providerID")
    return f"{prov}/{mid}" if prov else str(mid)


def _question_text(inp: dict) -> str:
    """The `question` tool's input as the question plus its numbered choices.

    opencode numbers the rows from 1 in the TUI, so this reads the same way as
    the dialog the user is looking at.
    """
    qs = inp.get("questions")
    if not isinstance(qs, list) or not qs:
        return ""
    out = []
    for q in qs:
        if not isinstance(q, dict):
            continue
        text = str(q.get("question") or q.get("header") or "").strip()
        opts = [str(o.get("label") or "").strip()
                for o in q.get("options") or [] if isinstance(o, dict)]
        opts = [o for o in opts if o]
        numbered = "  ".join(f"{i}. {o}" for i, o in enumerate(opts, 1))
        out.append(f"{text}\n{numbered}" if numbered else text)
    return "\n".join(o for o in out if o)


def _tool_text(data: dict) -> str:
    """A one-line-ish rendering of a tool part: its most identifying argument,
    or the error when the call failed."""
    state = data.get("state") or {}
    if state.get("status") == "error":
        return str(state.get("error") or "tool call failed")
    inp = state.get("input") or {}
    if not isinstance(inp, dict):
        return ""
    # The `question` tool asks the user something; dumping its JSON put the
    # whole option list, descriptions and all, on screen as one wall of quotes.
    asked = _question_text(inp)
    if asked:
        return asked
    # The argument that says what the call actually touched, by tool.
    for key in ("command", "filePath", "path", "pattern", "query", "description"):
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    try:
        return json.dumps(inp)[:400]
    except (TypeError, ValueError):
        return ""


def _is_noise(data: dict) -> bool:
    """True for a text part opencode injected rather than a human or the model
    writing it — editor context, IDE file-open notices and the like. They ride
    on the user's message and would otherwise render as the user's own words."""
    if data.get("synthetic") is True:
        return True
    meta = data.get("metadata") or {}
    return isinstance(meta, dict) and meta.get("kind") == "editor_context"


def _roles(conn: sqlite3.Connection, sid: str) -> dict[str, str]:
    """message id -> role ("user" / "assistant"). Part rows carry the content but
    not who said it; that lives on the parent message."""
    out: dict[str, str] = {}
    try:
        rows = conn.execute(
            "SELECT id, data FROM message WHERE session_id = ?", (sid,)).fetchall()
    except sqlite3.Error:
        return out
    for r in rows:
        out[r["id"]] = _loads(r["data"]).get("role") or "assistant"
    return out


def _activities(sid: str, limit: int) -> list[dict]:
    """Recent readable turns for a session, chronological.

    `limit` caps the *parts read*, newest-first, before the noise filter — so a
    board preview costs one small indexed read even on a session with tens of
    thousands of parts, rather than a full-transcript scan.
    """
    conn = _connect()
    if conn is None:
        return []
    try:
        roles = _roles(conn, sid)
        # Over-fetch: step-start/step-finish/patch parts get dropped below, and
        # they outnumber the parts we keep roughly two to one.
        fetch = limit * 6 if limit else -1
        try:
            rows = conn.execute(
                "SELECT message_id, time_created, data FROM part "
                "WHERE session_id = ? ORDER BY time_created DESC, id DESC "
                "LIMIT ?", (sid, fetch)).fetchall()
        except sqlite3.Error:
            return []
    finally:
        conn.close()

    acts: list[dict] = []
    for r in rows:                      # newest-first out of the query
        data = _loads(r["data"])
        kind = _PART_KINDS.get(data.get("type"))
        if kind is None:
            continue
        name = None
        if kind == "text":
            if _is_noise(data):
                continue
            text = (data.get("text") or "").strip()
            # kind carries the role so the UI labels each turn and styles
            # assistant replies — not a flat "opencode".
            kind = "user" if roles.get(r["message_id"]) == "user" else "assistant"
        elif kind == "thinking":
            text = (data.get("text") or "").strip()
        else:                            # tool
            name = data.get("tool")
            text = _tool_text(data)
        if not text:
            continue
        acts.append({"kind": kind, "name": name,
                     "ts": claude_parser._iso(_ms(r["time_created"])),
                     "role": kind, "text": text[:2000]})
        if limit and len(acts) >= limit:
            break
    acts.reverse()                       # back to chronological
    return acts


def _tokens(row: sqlite3.Row) -> dict:
    def _int(key):
        try:
            return int(row[key] or 0)
        except (TypeError, ValueError, IndexError, KeyError):
            return 0
    inp, out = _int("tokens_input"), _int("tokens_output")
    read, write = _int("tokens_cache_read"), _int("tokens_cache_write")
    return {"input": inp, "output": out, "cache_read": read,
            "cache_creation": write, "total": inp + out + read + write}


def _step_counts() -> dict[str, int]:
    """session id -> message count. One grouped query for the whole board."""
    return {r["session_id"]: int(r["n"] or 0) for r in
            _query("SELECT session_id, COUNT(*) AS n FROM message GROUP BY session_id")}


_SESSION_COLS = ("id, directory, title, model, agent, cost, tokens_input, "
                 "tokens_output, tokens_cache_read, tokens_cache_write, "
                 "time_created, time_updated, parent_id")


# ---- sub-agents --------------------------------------------------------------
#
# opencode runs a sub-agent as a real child session (`session.parent_id`) driven
# by a `task` tool part on the parent. Neither of those touches the parent row,
# so the parent's time_updated freezes for the whole run — which is why a
# delegating opencode session used to read WAITING on the board.
#
# Two bulk lookups answer it for every session at once: how many task parts are
# still open, and when each parent's children last wrote. Both scan the whole
# store (a json_extract over `part` is ~100ms and grows), so they are cached on
# a short TTL rather than run per session per poll.
_SUB_TTL = 2.0
_SUB_BULK: dict[str, object] = {"at": 0.0, "running": {}, "child_mtime": {}}

# A task part is finished once its state reaches one of these; anything else
# (pending, running, or a state opencode hasn't invented yet) counts as live.
_DONE_STATES = ("completed", "error")


def _bulk_subagents() -> tuple[dict, dict]:
    """({session_id: running task count}, {parent_id: newest child mtime})."""
    import time as _time
    now = _time.monotonic()
    if now - float(_SUB_BULK["at"]) < _SUB_TTL:
        return dict(_SUB_BULK["running"]), dict(_SUB_BULK["child_mtime"])  # type: ignore[arg-type]
    running = {
        r["session_id"]: int(r["n"])
        for r in _query(
            "SELECT session_id, COUNT(*) AS n FROM part "
            "WHERE json_extract(data,'$.tool') = 'task' "
            f"AND COALESCE(json_extract(data,'$.state.status'),'') NOT IN {_DONE_STATES} "
            "GROUP BY session_id")
    }
    child = {
        r["parent_id"]: _ms(r["m"])
        for r in _query("SELECT parent_id, MAX(time_updated) AS m FROM session "
                        "WHERE parent_id IS NOT NULL GROUP BY parent_id")
    }
    _SUB_BULK["at"], _SUB_BULK["running"], _SUB_BULK["child_mtime"] = now, running, child
    return dict(running), dict(child)


def _apply_subagents(s: dict, sid: str) -> None:
    """Attach the running sub-agent count and re-age the status around it."""
    running, child = _bulk_subagents()
    n = running.get(sid, 0)
    s["subagents_running"] = n
    age_from = s["mtime"]
    if n:
        age_from = max(age_from, child.get(sid) or 0)
    s["status"] = claude_parser.compute_status(age_from)


def subagents(sid: str) -> list[dict]:
    """Full sub-agent roster for one session, oldest first (detail view only).

    Joins each `task` part on the parent to the child session it spawned, so the
    row carries both what was asked (the part's input) and what the run actually
    cost (the child's tokens). Ordering is by the part's creation time.
    """
    children = {
        r["id"]: r for r in _query(
            "SELECT id, title, agent, model, time_created, time_updated, "
            "tokens_input, tokens_output FROM session WHERE parent_id = ?", (sid,))
    }
    # opencode does not record which child a task part spawned, so pair them up
    # in creation order — both lists are per-parent and chronological.
    kids = sorted(children.values(), key=lambda r: r["time_created"])
    out = []
    for i, r in enumerate(_query(
            "SELECT id, time_created, time_updated, data FROM part "
            "WHERE session_id = ? AND json_extract(data,'$.tool') = 'task' "
            "ORDER BY time_created", (sid,))):
        try:
            d = json.loads(r["data"])
        except (ValueError, TypeError):
            continue
        state = d.get("state") or {}
        inp = state.get("input") or {}
        kid = kids[i] if i < len(kids) else None
        status = state.get("status")
        out.append({
            "agent_id": d.get("callID") or r["id"],
            "agent_type": inp.get("subagent_type") or (kid["agent"] if kid else "") or "agent",
            "description": str(inp.get("description") or "")[:120],
            "running": status not in _DONE_STATES,
            "status": status,
            "started_at": _ms(r["time_created"]),
            "mtime": _ms(r["time_updated"]),
            "turns": None,
            "child_session_id": kid["id"] if kid else None,
        })
    return out


def _summarize(row: sqlite3.Row, step_count: int) -> dict:
    sid = row["id"]
    mtime = _ms(row["time_updated"])
    cached = _SUMM_CACHE.get(sid)
    if cached and cached[0] == row["time_updated"]:
        s = dict(cached[1])
        _apply_subagents(s, sid)
        return s

    cwd = row["directory"]
    summary = {
        "session_id": sid,
        "title": (row["title"] or "").strip() or "(opencode session)",
        "project": _project_label(cwd),
        "cwd": cwd,
        "model": _model_label(row["model"]),
        "entrypoint": "opencode",
        "origin": "opencode",
        "source": "opencode",
        "status": claude_parser.compute_status(mtime),
        "turn_pending": False,
        "created_at": claude_parser._iso(_ms(row["time_created"])),
        "updated_at": claude_parser._iso(mtime),
        "mtime": mtime,
        "tokens": _tokens(row),
        "cost": float(row["cost"] or 0),
        "agent": row["agent"],
        "step_count": step_count,
        "last_activities": _activities(sid, _MAX_ACT),
        "live_tmux": False,
        "live": False,
        "live_web": False,
        "pending_approval": False,
        "archived": False,
        "attention": False,
        "renamed": False,
        "autonomy": "manual",
    }
    _SUMM_CACHE[sid] = (row["time_updated"], summary)
    summary = dict(summary)
    _apply_subagents(summary, sid)
    return summary


# ---- public API (mirrors grokparser) ----------------------------------------

def session_ids() -> set[str]:
    return {r["id"] for r in _query("SELECT id FROM session")}


def has_session(sid: str) -> bool:
    return bool(_query("SELECT 1 FROM session WHERE id = ? LIMIT 1", (sid,)))


def get_summary(sid: str) -> dict | None:
    """Cheap cached board summary (3-turn preview only) for one session."""
    rows = _query(f"SELECT {_SESSION_COLS} FROM session WHERE id = ?", (sid,))
    if not rows:
        return None
    n = _query("SELECT COUNT(*) AS n FROM message WHERE session_id = ?", (sid,))
    return _summarize(rows[0], int(n[0]["n"]) if n else 0)


def list_sessions() -> list[dict]:
    """All opencode sessions as dashboard summaries, newest activity first.

    Child sessions (parent_id set) are opencode's subagent runs — they belong to
    their parent's transcript, not on the board as sessions of their own. They
    stay reachable by id, so a direct link to one still resolves.
    """
    counts = _step_counts()
    out = []
    for row in _query(f"SELECT {_SESSION_COLS} FROM session "
                      "WHERE parent_id IS NULL ORDER BY time_updated DESC"):
        n = counts.get(row["id"], 0)
        if n <= 0:
            continue                     # never-used session — nothing to show
        out.append(_summarize(row, n))
    return out


def get_session(sid: str) -> dict | None:
    """Full detail: summary header + activity list (newest first)."""
    s = get_summary(sid)
    if s is None:
        return None
    acts = _activities(sid, _MAX_DETAIL_ACT)
    acts.reverse()                       # newest first for the history view
    detail = dict(s)
    detail["activities"] = acts
    detail["subagents"] = subagents(sid)
    return detail


def usage_text(sid: str) -> dict:
    """A read-only usage panel built from the session row — no REPL scrape.

    opencode accounts tokens and cost per session in its own store, so we format
    those directly instead of driving a live overlay. Returns
    {"ok": bool, "text"|"error": str}."""
    rows = _query(f"SELECT {_SESSION_COLS} FROM session WHERE id = ?", (sid,))
    if not rows:
        return {"ok": False, "error": "opencode session not found"}
    row = rows[0]
    tok = _tokens(row)
    counts = _query(
        "SELECT COUNT(*) AS n, "
        "SUM(CASE WHEN json_extract(data,'$.role') = 'user' THEN 1 ELSE 0 END) AS u "
        "FROM message WHERE session_id = ?", (sid,))
    total_msgs = int(counts[0]["n"] or 0) if counts else 0
    user_msgs = int(counts[0]["u"] or 0) if counts else 0
    tools = _query(
        "SELECT COUNT(*) AS n FROM part WHERE session_id = ? "
        "AND json_extract(data,'$.type') = 'tool'", (sid,))
    tool_calls = int(tools[0]["n"] or 0) if tools else 0

    dur = max(0.0, _ms(row["time_updated"]) - _ms(row["time_created"]))
    mins, secs = divmod(int(dur), 60)
    lines = [
        f"Model:            {_model_label(row['model']) or '—'}",
        f"Agent:            {row['agent'] or '—'}",
        "",
        f"Tokens in:        {tok['input']:,}",
        f"Tokens out:       {tok['output']:,}",
        f"Cache read:       {tok['cache_read']:,}",
        f"Cache write:      {tok['cache_creation']:,}",
        f"Total:            {tok['total']:,}",
        "",
        f"Messages:         {user_msgs} user / {total_msgs - user_msgs} assistant",
        f"Tool calls:       {tool_calls}",
        f"Cost:             ${float(row['cost'] or 0):.4f}",
        "",
        f"Session duration: {mins}m {secs}s",
    ]
    return {"ok": True, "text": "\n".join(lines)}
