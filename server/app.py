"""
app.py — FastAPI layer over parser.py + runner.py.

Endpoints:
    GET  /api/sessions?limit=10&offset=0   -> {sessions, total}  (origin/live merged in)
    GET  /api/sessions/{id}                -> full detail
    POST /api/sessions/{id}/send           -> SSE stream of a resumed turn
    GET  /                                  -> dashboard
    GET  /session.html                      -> detail page
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (agyparser, archives, attention, autonomy, descriptions,
               grokparser, models, ollamausage, opencodeparser, overrides,
               parser, projects, registry, runner, slackbot, summaries,
               summarizer, tasks, tmuxio)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="Agent OS")


@app.on_event("startup")
def _start_background():
    # Auto-approver for sessions on auto-safe/yolo (always on; honours its own
    # kill switches). Slack is a no-op unless its tokens are set.
    autonomy.start_watcher()
    slackbot.start()


@app.middleware("http")
async def no_store(request, call_next):
    """Local dev tool — never let the browser cache HTML/JS/CSS, so updates to
    static assets always take effect on reload."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


# mtime slop (seconds) to absorb timestamp resolution between our recorded
# post-turn mtime and a subsequent stat of the same file.
_MTIME_EPS = 1.0


# A live tmux REPL that's merely idle must not age past WAITING — it's still
# alive and waiting on you. Only once its tmux is killed can it decay further.
_BEYOND_WAITING = {"SITTING", "SLEEPING", "ENDED"}


def _decorate(summary: dict, web_mtimes: dict, running: set[str],
              titles: dict | None = None, archived: set | None = None,
              live_ids: set | None = None, marked: set | None = None,
              working_ids: set | None = None, errors: dict | None = None,
              descs: dict | None = None) -> dict:
    """Attach origin (cli/vscode/web), live flags, title override, archived flag.

    A session is 'web' only if either (a) a web turn is generating right now,
    or (b) the web app wrote last — i.e. the file has NOT been written since
    our recorded post-turn mtime. If the CLI writes afterwards, it flips back.
    """
    sid = summary["session_id"]

    # Apply user title override (dashboard-only; transcript is untouched).
    titles = titles if titles is not None else overrides.all_titles()
    summary["default_title"] = summary["title"]
    if sid in titles:
        summary["title"] = titles[sid]
        summary["renamed"] = True
    else:
        summary["renamed"] = False

    # Your own note about what this session is for. Dashboard-only, like the
    # title override — absent means you haven't written one.
    descs = descs if descs is not None else descriptions.all_descriptions()
    summary["description"] = descs.get(sid)

    archived = archived if archived is not None else archives.archived_ids()
    summary["archived"] = sid in archived

    marked = marked if marked is not None else attention.marked_ids()
    summary["attention"] = sid in marked

    cur_mtime = summary.get("mtime") or 0
    web_mtime = web_mtimes.get(sid)

    if sid in running:
        summary["origin"], summary["live_web"] = "web", True
    elif web_mtime is not None and cur_mtime <= web_mtime + _MTIME_EPS:
        summary["origin"], summary["live_web"] = "web", False
    else:
        summary["origin"], summary["live_web"] = summary.get("entrypoint", "cli"), False

    is_web = summary["origin"] == "web"
    # CLI "live" = actively writing (THINKING) and not driven by us
    summary["live"] = summary["live_web"] or (not is_web and summary.get("status") == "THINKING")

    # Live tmux REPL drives the status. THINKING means the pane is actually
    # generating right now (a spinner is up) — ground truth, unlike the
    # transcript which can end on a queued tool_result / injected "no visible
    # output" nudge while the REPL has already gone idle. An idle-but-live REPL
    # is pinned at WAITING (never decays until its tmux is killed). With NO live
    # tmux the session isn't executing, so it's never THINKING.
    live_ids = live_ids if live_ids is not None else tmuxio.tmux_sessions()
    working = working_ids if working_ids is not None else tmuxio.working_ids()
    summary["live_tmux"] = sid in live_ids
    if summary["live_tmux"]:
        summary["status"] = "THINKING" if sid in working else "WAITING"
    elif summary.get("status") == "THINKING":
        summary["status"] = "WAITING"

    # The REPL's current error / retry banner, if it's showing one. Deliberately
    # a field of its own rather than a status value: the session may still be
    # THINKING (retrying) or WAITING, and it clears the moment the pane stops
    # showing the banner — nothing to reset by hand.
    if summary["live_tmux"]:
        errs = errors if errors is not None else tmuxio.error_lines()
        summary["error"] = errs.get(sid)
    else:
        summary["error"] = None
    return summary


# Sessions that are idle and waiting on the user (the "needs attention" set).
ATTENTION_STATUSES = {"WAITING", "SITTING", "SLEEPING"}

# Max activities shipped in a detail response (newest first). The UI's history
# selector tops out at 100; older history stays in the transcript. Big sessions
# would otherwise ship megabytes of activities the page never renders.
_DETAIL_ACT_CAP = 400


@app.get("/api/sessions")
def api_sessions(limit: str = Query("10"), offset: int = Query(0),
                 status: str | None = Query(None), archived: str | None = Query(None)):
    lim = None if limit == "all" else int(limit)
    statuses = None
    if status:
        if status.lower() == "attention":
            statuses = ATTENTION_STATUSES
        else:
            statuses = {x.strip().upper() for x in status.split(",") if x.strip()}

    arch_ids = archives.archived_ids()
    # default: hide archived from normal listings; board passes archived=all
    mode = {"all": "all", "only": "only"}.get((archived or "").lower(), "exclude")

    data = parser.list_sessions(limit=lim, offset=offset, statuses=statuses,
                                archived_ids=arch_ids, archived_mode=mode)
    web_mtimes, running, titles = registry.web_mtimes(), runner.running_ids(), overrides.all_titles()
    gated = tmuxio.pending_ids()
    live_tmux = tmuxio.tmux_sessions()
    levels = autonomy.all()
    proj_map = projects.tags_by_session()
    task_counts = tasks.counts_by_session()
    for s in data["sessions"]:
        _decorate(s, web_mtimes, running, titles, arch_ids, live_ids=live_tmux)
        sid = s["session_id"]
        s["pending_approval"] = sid in gated
        s["autonomy"] = levels.get(sid, autonomy.DEFAULT)
        s["projects"] = proj_map.get(sid, [])
        s["task_count"] = task_counts.get(sid, 0)

    # Merge in Antigravity (agy) + grok sessions — read-only, already summary-shaped.
    marked = attention.marked_ids()
    for s in _agy_summaries(titles, arch_ids, marked, mode, live_tmux):
        if statuses and s["status"] not in statuses:
            continue
        s["projects"] = proj_map.get(s["session_id"], [])
        s["task_count"] = task_counts.get(s["session_id"], 0)
        data["sessions"].append(s)
    for s in _grok_summaries(titles, arch_ids, marked, mode, live_tmux):
        if statuses and s["status"] not in statuses:
            continue
        s["projects"] = proj_map.get(s["session_id"], [])
        s["task_count"] = task_counts.get(s["session_id"], 0)
        data["sessions"].append(s)
    for s in _opencode_summaries(titles, arch_ids, marked, mode, live_tmux):
        if statuses and s["status"] not in statuses:
            continue
        s["projects"] = proj_map.get(s["session_id"], [])
        s["task_count"] = task_counts.get(s["session_id"], 0)
        data["sessions"].append(s)
    data["sessions"].sort(key=lambda x: x.get("mtime") or 0, reverse=True)
    data["total"] = len(data["sessions"])
    return data


def _agy_live_status(session_id: str, visible: str | None) -> tuple[str, bool]:
    """(status, pending_approval) for a live agy pane: gate → WAITING+gate,
    generating → THINKING, idle input box → WAITING."""
    if agyparser.parse_gate(visible) is not None or tmuxio.parse_prompt(visible) is not None:
        return "WAITING", True
    if agyparser.is_generating(visible):
        return "THINKING", False
    if agyparser.at_input_box(visible):
        return "WAITING", False
    return ("THINKING" if visible is not None else "WAITING"), False


def _agy_decorate_live(s: dict, session_id: str, full: str | None = "__cap__") -> dict:
    """Overlay a live agy session's summary/detail with pane-derived fields:
    status, pending_approval, model, and token totals. Single source so the
    board, status poll, and detail all agree.

    Pass `full` (a full-scrollback capture) to reuse one capture across
    status/model/tokens/events instead of shelling out to tmux several times —
    the agy detail poll is capture-bound, so this is the main latency win. The
    visible frame is sliced from the tail of `full` (gate/spinner/input live at
    the bottom), avoiding a second capture too.
    """
    if full == "__cap__":
        full = tmuxio.capture_pane(session_id, history=5000)
    visible = "\n".join((full or "").splitlines()[-60:]) or None
    s["live_tmux"] = s["live"] = True
    s["status"], s["pending_approval"] = _agy_live_status(session_id, visible)
    s["error"] = tmuxio.error_line(visible)
    s["model"] = agyparser.model_from_screen(visible) or s.get("model")
    tok = agyparser.tokens_from_screen(full)
    if tok:
        s["tokens"] = {"input": 0, "output": tok, "cache_read": 0,
                       "cache_creation": 0, "total": tok}
    return s


def _agy_summaries(titles, arch_ids, marked, mode="exclude", live_ids=None):
    """agy conversations decorated for the board (title override, archived,
    attention, live-tmux), honoring the archived visibility mode. A conversation
    is Live when a tmux session is named after its id (see runagy_default.sh)."""
    # Auto-link freshly-launched agy sessions whose tmux still has a temp name.
    if agyparser.reconcile_tmux_names():
        live_ids = None                       # refresh after any rename
    live_ids = live_ids if live_ids is not None else tmuxio.tmux_sessions()
    descs = descriptions.all_descriptions()
    out = []
    for s in agyparser.list_conversations():
        sid = s["session_id"]
        s["archived"] = sid in arch_ids
        if mode == "exclude" and s["archived"]:
            continue
        if mode == "only" and not s["archived"]:
            continue
        s["default_title"] = s["title"]
        if sid in titles:
            s["title"], s["renamed"] = titles[sid], True
        s["description"] = descs.get(sid)
        s["attention"] = sid in marked
        s["autonomy"] = autonomy.get(sid)
        if sid in live_ids:
            _agy_decorate_live(s, sid)       # pane is the truth (status/model/tokens)
        elif s["status"] == "THINKING":
            s["live_tmux"] = False
            s["status"] = "WAITING"          # not live → never THINKING
        else:
            s["live_tmux"] = False
        out.append(s)
    return out


def _grok_summaries(titles, arch_ids, marked, mode="exclude", live_ids=None):
    """grok sessions decorated for the board (title override, archived, attention,
    live-tmux), honoring the archived visibility mode. Read-only: no pane parsing,
    so a live tmux (named after the id) just pins the status at WAITING."""
    live_ids = live_ids if live_ids is not None else tmuxio.tmux_sessions()
    errs = tmuxio.error_lines()      # cached sweep — no extra tmux calls here
    descs = descriptions.all_descriptions()
    out = []
    for s in grokparser.list_sessions():
        sid = s["session_id"]
        s["archived"] = sid in arch_ids
        if mode == "exclude" and s["archived"]:
            continue
        if mode == "only" and not s["archived"]:
            continue
        s["default_title"] = s["title"]
        if sid in titles:
            s["title"], s["renamed"] = titles[sid], True
        s["description"] = descs.get(sid)
        s["attention"] = sid in marked
        s["autonomy"] = autonomy.get(sid)
        # grok is read-only (no send/gate/kill), but a tmux named after its id
        # means a live REPL is running — flag it live so the board shows it.
        if sid in live_ids:
            s["live_tmux"] = s["live"] = True
            # mid-turn (grok pane shows "Esc to stop") → THINKING, else WAITING.
            s["status"] = "THINKING" if tmuxio.grok_working(sid) else "WAITING"
            s["error"] = errs.get(sid)
        else:
            s["live_tmux"] = s["live"] = False
            s["error"] = None
            if s["status"] == "THINKING":
                s["status"] = "WAITING"      # not live → never surface THINKING
        out.append(s)
    return out


def _opencode_summaries(titles, arch_ids, marked, mode="exclude", live_ids=None):
    """opencode sessions decorated for the board (title override, archived,
    attention, live-tmux), honoring the archived visibility mode.

    Unlike grok, opencode's permission gate is readable from the pane, so a live
    session can report pending_approval — that's what puts it in the To-do inbox
    rather than leaving it silently blocked.
    """
    live_ids = live_ids if live_ids is not None else tmuxio.tmux_sessions()
    errs = tmuxio.error_lines()      # cached sweep — no extra tmux calls here
    descs = descriptions.all_descriptions()
    out = []
    for s in opencodeparser.list_sessions():
        sid = s["session_id"]
        s["archived"] = sid in arch_ids
        if mode == "exclude" and s["archived"]:
            continue
        if mode == "only" and not s["archived"]:
            continue
        s["default_title"] = s["title"]
        if sid in titles:
            s["title"], s["renamed"] = titles[sid], True
        s["description"] = descs.get(sid)
        s["attention"] = sid in marked
        s["autonomy"] = autonomy.get(sid)
        if sid in live_ids:
            s["live_tmux"] = s["live"] = True
            _apply_opencode_live(s, sid)
            s["error"] = errs.get(sid)
        else:
            s["live_tmux"] = s["live"] = False
            s["error"] = None
            if s["status"] == "THINKING":
                s["status"] = "WAITING"      # not live → never surface THINKING
        out.append(s)
    return out


def _apply_opencode_live(s: dict, session_id: str) -> None:
    """Status + gate flag for a live opencode pane: a permission gate pins
    WAITING and raises pending_approval, mid-turn is THINKING, else WAITING."""
    if tmuxio.opencode_pending(session_id) is not None:
        s["status"] = "WAITING"
        s["pending_approval"] = True
        return
    s["pending_approval"] = False
    s["status"] = "THINKING" if tmuxio.opencode_working(session_id) else "WAITING"


@app.get("/api/search")
def api_search(q: str = Query(""), archived: str | None = Query(None)):
    """Search by id/title/project. Archived sessions are excluded unless
    `archived=include`."""
    include_archived = (archived or "").lower() in ("1", "true", "include", "yes")
    web_mtimes, running, titles = registry.web_mtimes(), runner.running_ids(), overrides.all_titles()
    live_tmux = tmuxio.tmux_sessions()
    arch_ids = archives.archived_ids()
    data = parser.search_sessions(q, extra_titles=titles)
    proj_map = projects.tags_by_session()
    task_counts = tasks.counts_by_session()
    kept = []
    for s in data["sessions"]:
        _decorate(s, web_mtimes, running, titles, live_ids=live_tmux)
        if include_archived or not s.get("archived"):
            s["projects"] = proj_map.get(s["session_id"], [])
            s["task_count"] = task_counts.get(s["session_id"], 0)
            kept.append(s)
    data["sessions"] = kept
    # Include matching agy conversations (by id / title / project).
    ql = q.lower().strip()
    if ql:
        marked = attention.marked_ids()
        for s in (_agy_summaries(titles, arch_ids, marked, mode="all")
                  + _grok_summaries(titles, arch_ids, marked, mode="all")
                  + _opencode_summaries(titles, arch_ids, marked, mode="all")):
            if not include_archived and s.get("archived"):
                continue
            if (ql in s["session_id"].lower() or ql in (s["title"] or "").lower()
                    or ql in (s["project"] or "").lower() or ql in (s["cwd"] or "").lower()):
                s["projects"] = proj_map.get(s["session_id"], [])
                s["task_count"] = task_counts.get(s["session_id"], 0)
                data["sessions"].append(s)
    data["total"] = len(data["sessions"])
    return data


@app.get("/api/sessions/{session_id}")
def api_session(session_id: str):
    detail = parser.get_session(session_id)
    if detail is None:
        if grokparser.has_session(session_id):
            detail = grokparser.get_session(session_id)   # read-only, JSON store
            if detail is None:
                raise HTTPException(status_code=404, detail="session not found")
            # read-only, but a tmux named after its id = a live REPL is running.
            if session_id in tmuxio.tmux_sessions():
                detail["live_tmux"] = detail["live"] = True
                detail["status"] = ("THINKING" if tmuxio.grok_working(session_id)
                                    else "WAITING")
                detail["error"] = tmuxio.error_lines().get(session_id)
            else:
                detail["live_tmux"] = detail["live"] = False
                detail["error"] = None
                if detail.get("status") == "THINKING":
                    detail["status"] = "WAITING"
            titles = overrides.all_titles()
            if session_id in titles:
                detail["title"], detail["renamed"] = titles[session_id], True
            detail["description"] = descriptions.get(session_id)
            detail["archived"] = archives.is_archived(session_id)
            detail["attention"] = attention.is_marked(session_id)
            detail["autonomy"] = autonomy.get(session_id)
            detail["projects"] = projects.projects_for(session_id)
            detail["task_count"] = tasks.pending_count(session_id)
            return detail
        if opencodeparser.has_session(session_id):
            detail = opencodeparser.get_session(session_id)  # read-only, sqlite
            if detail is None:
                raise HTTPException(status_code=404, detail="session not found")
            # read-only, but a tmux named after its id = a live REPL is running.
            if session_id in tmuxio.tmux_sessions():
                detail["live_tmux"] = detail["live"] = True
                _apply_opencode_live(detail, session_id)
                detail["error"] = tmuxio.error_lines().get(session_id)
            else:
                detail["live_tmux"] = detail["live"] = False
                detail["error"] = None
                if detail.get("status") == "THINKING":
                    detail["status"] = "WAITING"
            titles = overrides.all_titles()
            if session_id in titles:
                detail["title"], detail["renamed"] = titles[session_id], True
            detail["description"] = descriptions.get(session_id)
            detail["archived"] = archives.is_archived(session_id)
            detail["attention"] = attention.is_marked(session_id)
            detail["autonomy"] = autonomy.get(session_id)
            detail["projects"] = projects.projects_for(session_id)
            detail["task_count"] = tasks.pending_count(session_id)
            return detail
        if not agyparser.has_conversation(session_id):
            raise HTTPException(status_code=404, detail="session not found")
        live = session_id in tmuxio.tmux_sessions()
        if live:
            # Live agy: the pane is the in-sync truth and the .db history parse
            # (~1.4s of protobuf extraction over every step) would be thrown
            # away — so use the cheap cached summary for the header and parse
            # the pane for activities. One capture reused for events + status.
            detail = agyparser.get_summary(session_id) or {}
            full = tmuxio.capture_pane(session_id, history=5000)
            detail["activities"] = agyparser.parse_console(full)
            _agy_decorate_live(detail, session_id, full=full)
        else:
            detail = agyparser.get_conversation(session_id)   # full .db history
            if detail is None:
                raise HTTPException(status_code=404, detail="session not found")
            if detail.get("status") == "THINKING":
                detail["status"] = "WAITING"
        titles = overrides.all_titles()
        if session_id in titles:
            detail["title"], detail["renamed"] = titles[session_id], True
        detail["description"] = descriptions.get(session_id)
        detail["archived"] = archives.is_archived(session_id)
        detail["attention"] = attention.is_marked(session_id)
        detail["autonomy"] = autonomy.get(session_id)
        detail["projects"] = projects.projects_for(session_id)
        return detail
    _decorate(detail, registry.web_mtimes(), runner.running_ids())
    detail["autonomy"] = autonomy.get(session_id)
    detail["projects"] = projects.projects_for(session_id)
    # Cap the activity payload to the most recent slice — a long transcript can
    # ship megabytes of history the UI never shows (the history-limit selector
    # tops out at 100; new events stream in via /tail). Keeps page load fast.
    acts = detail.get("activities") or []
    if len(acts) > _DETAIL_ACT_CAP:
        detail["activities"] = acts[:_DETAIL_ACT_CAP]   # newest-first → keep head
        detail["activities_total"] = len(acts)
    return detail


# Statuses for which a "what's expected from you" summary makes sense:
# idle and the assistant spoke last (waiting on the user).
_WAITING_STATUSES = {"WAITING", "SITTING", "SLEEPING"}


@app.get("/api/sessions/{session_id}/status")
def api_status(session_id: str):
    """Cheap, cached summary (no activities) — for live header refresh on the
    detail page without re-parsing the full transcript each poll."""
    s = parser.get_summary(session_id)
    if s is None:
        if grokparser.has_session(session_id):
            s = grokparser.get_summary(session_id)
            if s is None:
                raise HTTPException(status_code=404, detail="session not found")
            if session_id in tmuxio.tmux_sessions():
                s["live_tmux"] = s["live"] = True
                s["status"] = ("THINKING" if tmuxio.grok_working(session_id)
                               else "WAITING")
                s["error"] = tmuxio.error_lines().get(session_id)
            elif s.get("status") == "THINKING":
                s["status"] = "WAITING"
            s["autonomy"] = autonomy.get(session_id)
            s["projects"] = projects.projects_for(session_id)
            t = overrides.get_title(session_id)
            if t:
                s["title"], s["renamed"] = t, True
            s["description"] = descriptions.get(session_id)
            return s
        if opencodeparser.has_session(session_id):
            s = opencodeparser.get_summary(session_id)
            if s is None:
                raise HTTPException(status_code=404, detail="session not found")
            if session_id in tmuxio.tmux_sessions():
                s["live_tmux"] = s["live"] = True
                _apply_opencode_live(s, session_id)
                s["error"] = tmuxio.error_lines().get(session_id)
            elif s.get("status") == "THINKING":
                s["status"] = "WAITING"
            s["autonomy"] = autonomy.get(session_id)
            s["projects"] = projects.projects_for(session_id)
            t = overrides.get_title(session_id)
            if t:
                s["title"], s["renamed"] = t, True
            s["description"] = descriptions.get(session_id)
            return s
        s = agyparser._summarize(os.path.join(agyparser.CONV_DIR, f"{session_id}.db")) \
            if agyparser.has_conversation(session_id) else None
        if s is None:
            raise HTTPException(status_code=404, detail="session not found")
        # Live-aware: derive status from the pane so the header + poll cadence
        # (active → fast) track generation, not the stale db mtime.
        if session_id in tmuxio.tmux_sessions():
            _agy_decorate_live(s, session_id)
        elif s.get("status") == "THINKING":
            s["status"] = "WAITING"
        s["autonomy"] = autonomy.get(session_id)
        # The poll merges this over the loaded detail — send the real project
        # tags, otherwise an empty list wipes the header chips every tick.
        s["projects"] = projects.projects_for(session_id)
        t = overrides.get_title(session_id)   # keep the custom title on poll
        if t:
            s["title"], s["renamed"] = t, True
        s["description"] = descriptions.get(session_id)
        return s
    _decorate(s, registry.web_mtimes(), runner.running_ids())
    s["autonomy"] = autonomy.get(session_id)
    s["projects"] = projects.projects_for(session_id)
    return s


@app.get("/api/sessions/{session_id}/tail")
def api_tail(session_id: str, offset: int = Query(0)):
    """Incremental history: events written after byte `offset`. For live
    streaming on the detail page without re-parsing the whole transcript."""
    path = parser.session_path(session_id)
    if path is None:
        raise HTTPException(status_code=404, detail="session not found")
    activities, new_offset = parser.tail_activities(path, offset)
    return {"activities": activities, "offset": new_offset}


@app.get("/api/sessions/{session_id}/summary")
async def api_summary(session_id: str):
    """One-paragraph summary of what response the session is waiting for.

    Only generated when the session is idle-waiting and the assistant spoke
    last. Cached per waiting episode (keyed by transcript mtime).
    """
    detail = parser.get_session(session_id)
    if detail is None:
        # agy, grok + opencode stores are read-only — no LLM "what's expected" summary.
        if agyparser.has_conversation(session_id):
            return {"status": None, "summary": None, "reason": "agy (read-only)"}
        if grokparser.has_session(session_id):
            return {"status": None, "summary": None, "reason": "grok (read-only)"}
        if opencodeparser.has_session(session_id):
            return {"status": None, "summary": None, "reason": "opencode (read-only)"}
        raise HTTPException(status_code=404, detail="session not found")

    status = detail.get("status")
    if status == "THINKING":
        return {"status": status, "summary": None, "reason": "still working"}
    if status not in _WAITING_STATUSES:
        return {"status": status, "summary": None, "reason": "not waiting"}

    # last assistant message = the turn that ended before the pause
    last_assistant = next(
        (a["text"] for a in detail.get("activities", [])
         if a.get("kind") == "assistant" and a.get("text", "").strip()),
        None,
    )
    if not last_assistant:
        return {"status": status, "summary": None, "reason": "no assistant message"}

    mtime = detail.get("mtime") or 0
    cached = summaries.get(session_id, mtime)
    if cached:
        return {"status": status, "summary": cached, "cached": True}

    text = await summarizer.generate(last_assistant)
    if not text:
        return {"status": status, "summary": None, "reason": "generation failed"}
    summaries.set(session_id, mtime, text)
    return {"status": status, "summary": text, "cached": False}


def _session_exists(session_id: str) -> bool:
    """A claude transcript, agy conversation, grok or opencode session exists."""
    return (parser.session_path(session_id) is not None
            or agyparser.has_conversation(session_id)
            or grokparser.has_session(session_id)
            or opencodeparser.has_session(session_id))


@app.post("/api/sessions/{session_id}/archive")
def api_archive(session_id: str):
    if not _session_exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    archives.set_archived(session_id, True)
    return {"session_id": session_id, "archived": True}


@app.delete("/api/sessions/{session_id}/archive")
def api_unarchive(session_id: str):
    archives.set_archived(session_id, False)
    return {"session_id": session_id, "archived": False}


@app.post("/api/sessions/{session_id}/attention")
def api_mark_attention(session_id: str):
    """Manually pin this session to the Attention page (persists until unmarked)."""
    if not _session_exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    attention.set_marked(session_id, True)
    return {"session_id": session_id, "attention": True}


@app.delete("/api/sessions/{session_id}/attention")
def api_unmark_attention(session_id: str):
    attention.set_marked(session_id, False)
    return {"session_id": session_id, "attention": False}


def _summary_by_id(session_id: str, titles: dict, arch_ids: set, marked: set,
                   live_ids: set) -> dict | None:
    """Board-shaped, decorated summary for one session (claude or agy), or None
    if it no longer exists. Used to render a project's member widgets."""
    summary = parser.get_summary(session_id)
    if summary is not None:
        _decorate(summary, registry.web_mtimes(), runner.running_ids(),
                  titles=titles, archived=arch_ids, live_ids=live_ids, marked=marked)
        summary["pending_approval"] = session_id in tmuxio.pending_ids()
        summary["autonomy"] = autonomy.get(session_id)
        summary["task_count"] = tasks.pending_count(session_id)
        return summary
    if agyparser.has_conversation(session_id):
        s = agyparser.get_summary(session_id)
        if s is None:
            return None
        sid = s["session_id"]
        s["default_title"] = s["title"]
        if sid in titles:
            s["title"], s["renamed"] = titles[sid], True
        s["description"] = descriptions.get(sid)
        s["archived"] = sid in arch_ids
        s["attention"] = sid in marked
        s["autonomy"] = autonomy.get(sid)
        if sid in live_ids:
            _agy_decorate_live(s, sid)
        else:
            s["live_tmux"] = False
            if s.get("status") == "THINKING":
                s["status"] = "WAITING"
        s["task_count"] = tasks.pending_count(sid)
        return s
    if grokparser.has_session(session_id):
        s = grokparser.get_summary(session_id)
        if s is None:
            return None
        sid = s["session_id"]
        s["default_title"] = s["title"]
        if sid in titles:
            s["title"], s["renamed"] = titles[sid], True
        s["description"] = descriptions.get(sid)
        s["archived"] = sid in arch_ids
        s["attention"] = sid in marked
        s["autonomy"] = autonomy.get(sid)
        if sid in live_ids:
            s["live_tmux"] = True
            s["status"] = "WAITING"
            s["error"] = tmuxio.error_lines().get(sid)
        else:
            s["live_tmux"] = False
            s["error"] = None
            if s.get("status") == "THINKING":
                s["status"] = "WAITING"
        s["task_count"] = tasks.pending_count(sid)
        return s
    if opencodeparser.has_session(session_id):
        s = opencodeparser.get_summary(session_id)
        if s is None:
            return None
        sid = s["session_id"]
        s["default_title"] = s["title"]
        if sid in titles:
            s["title"], s["renamed"] = titles[sid], True
        s["description"] = descriptions.get(sid)
        s["archived"] = sid in arch_ids
        s["attention"] = sid in marked
        s["autonomy"] = autonomy.get(sid)
        if sid in live_ids:
            s["live_tmux"] = True
            _apply_opencode_live(s, sid)
            s["error"] = tmuxio.error_lines().get(sid)
        else:
            s["live_tmux"] = False
            s["error"] = None
            if s.get("status") == "THINKING":
                s["status"] = "WAITING"
        s["task_count"] = tasks.pending_count(sid)
        return s
    return None


class ProjectBody(BaseModel):
    title: str = ""
    description: str = ""


@app.get("/api/projects")
def api_projects():
    """All projects with member counts (for the Projects page + tag picker)."""
    return {"projects": projects.list_projects()}


@app.post("/api/projects")
def api_create_project(body: ProjectBody):
    if not body.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    return projects.create_project(body.title, body.description)


@app.get("/api/projects/{pid}")
def api_project(pid: str):
    """Project header + its member sessions decorated as board widgets."""
    proj = projects.get_project(pid)
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")
    titles = overrides.all_titles()
    arch_ids = archives.archived_ids()
    marked = attention.marked_ids()
    live_ids = tmuxio.tmux_sessions()
    sessions = []
    for sid in projects.sessions_for(pid):
        s = _summary_by_id(sid, titles, arch_ids, marked, live_ids)
        if s is not None:
            sessions.append(s)
    sessions.sort(key=lambda x: x.get("mtime") or 0, reverse=True)
    proj["sessions"] = sessions
    return proj


@app.patch("/api/projects/{pid}")
def api_update_project(pid: str, body: ProjectBody):
    if not projects.update_project(pid, title=body.title, description=body.description):
        raise HTTPException(status_code=404, detail="project not found")
    return projects.get_project(pid)


@app.delete("/api/projects/{pid}")
def api_delete_project(pid: str):
    if not projects.delete_project(pid):
        raise HTTPException(status_code=404, detail="project not found")
    return {"id": pid, "deleted": True}


@app.post("/api/projects/{pid}/sessions/{session_id}")
def api_tag_session(pid: str, session_id: str):
    if not _session_exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    if not projects.tag(session_id, pid):
        raise HTTPException(status_code=404, detail="project not found")
    return {"project_id": pid, "session_id": session_id, "tagged": True}


@app.delete("/api/projects/{pid}/sessions/{session_id}")
def api_untag_session(pid: str, session_id: str):
    projects.untag(session_id, pid)
    return {"project_id": pid, "session_id": session_id, "tagged": False}


class TaskBody(BaseModel):
    text: str = ""
    asked: bool = False


@app.get("/api/sessions/{session_id}/tasks")
def api_tasks(session_id: str, archived: bool = False):
    """Canned messages queued up for this session (nothing is sent yet).

    archived=true returns the archive instead of the active list."""
    return {"tasks": tasks.list_tasks(session_id, archived=archived)}


@app.post("/api/sessions/{session_id}/tasks")
def api_add_task(session_id: str, body: TaskBody):
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    return tasks.add_task(session_id, body.text, asked=body.asked)


def _last_assistant_text(session_id: str) -> str | None:
    """The newest assistant message of a claude, grok, opencode, or agy session.

    Activities come back newest-first from every parser, so the first assistant
    entry with text is the turn the session stopped on."""
    detail = parser.get_session(session_id)
    if detail is None and grokparser.has_session(session_id):
        detail = grokparser.get_session(session_id)
    if detail is None and opencodeparser.has_session(session_id):
        detail = opencodeparser.get_session(session_id)
    if detail is None and agyparser.has_conversation(session_id):
        detail = agyparser.get_conversation(session_id)
    if detail is None:
        return None
    return next(
        (a["text"] for a in detail.get("activities", [])
         if a.get("kind") == "assistant" and (a.get("text") or "").strip()),
        None,
    )


class TaskGenBody(BaseModel):
    text: str = ""          # the assistant message to work from; "" = the latest


@app.post("/api/sessions/{session_id}/tasks/summarize")
async def api_task_summarize(session_id: str, body: TaskGenBody | None = None):
    """Queue a task written from an assistant message.

    The 📋 Task button on a message in the history posts that message's text;
    with no text the session's latest assistant message is used. Runs the same
    throwaway `claude --print` as the waiting summary, but with a prompt that
    produces the reply to send back, so the new task is ready to Ask (or edit)
    instead of being a recap. Not cached: it is an explicit button press.
    """
    last = (body.text.strip() if body and body.text.strip()
            else _last_assistant_text(session_id))
    if last is None:
        if not _session_exists(session_id):
            raise HTTPException(status_code=404, detail="session not found")
        raise HTTPException(status_code=409, detail="no assistant message yet")
    text = await summarizer.as_task(last)
    if not text:
        raise HTTPException(status_code=502, detail="could not summarize the last message")
    return tasks.add_task(session_id, text)


@app.patch("/api/sessions/{session_id}/tasks/{tid}")
def api_update_task(session_id: str, tid: str, body: TaskBody):
    text = body.text if body.text.strip() else None
    if text is None and not body.asked:
        raise HTTPException(status_code=400, detail="text is required")
    rec = tasks.update_task(session_id, tid, text=text, asked=body.asked)
    if rec is None:
        raise HTTPException(status_code=404, detail="task not found")
    return rec


class TaskMoveBody(BaseModel):
    delta: int = 0          # -1 = one place earlier, +1 = one place later


@app.post("/api/sessions/{session_id}/tasks/{tid}/move")
def api_move_task(session_id: str, tid: str, body: TaskMoveBody):
    """Reorder a task — its position is the sequence you'll ask them in."""
    if not body.delta:
        raise HTTPException(status_code=400, detail="delta must be non-zero")
    items = tasks.move_task(session_id, tid, body.delta)
    if items is None:
        raise HTTPException(status_code=404, detail="task not found")
    return {"tasks": items}


class TaskArchiveBody(BaseModel):
    archived: bool = True


@app.post("/api/sessions/{session_id}/tasks/{tid}/archive")
def api_archive_task(session_id: str, tid: str, body: TaskArchiveBody):
    """Move a task to the archive, or restore it to the active list."""
    rec = tasks.set_archived(session_id, tid, body.archived)
    if rec is None:
        raise HTTPException(status_code=404, detail="task not found")
    return rec


# Declared before /tasks/{tid} — routes match in order, so the other way round
# "archived" would be swallowed as a task id.
@app.delete("/api/sessions/{session_id}/tasks/archived")
def api_delete_archived(session_id: str):
    """Empty this session's task archive; active tasks are left alone."""
    return {"deleted": tasks.delete_archived(session_id)}


@app.delete("/api/sessions/{session_id}/tasks/{tid}")
def api_delete_task(session_id: str, tid: str):
    if not tasks.delete_task(session_id, tid):
        raise HTTPException(status_code=404, detail="task not found")
    return {"id": tid, "deleted": True}


class TitleBody(BaseModel):
    title: str = ""


@app.put("/api/sessions/{session_id}/title")
def api_set_title(session_id: str, body: TitleBody):
    """Set a custom title override (empty title reverts to the original)."""
    if not _session_exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    overrides.set_title(session_id, body.title)
    return {"session_id": session_id, "title": overrides.get_title(session_id)}


@app.delete("/api/sessions/{session_id}/title")
def api_clear_title(session_id: str):
    """Remove the override, reverting to the transcript-derived title."""
    overrides.clear_title(session_id)
    return {"session_id": session_id, "title": None}


class DescriptionBody(BaseModel):
    description: str = ""


@app.put("/api/sessions/{session_id}/description")
def api_set_description(session_id: str, body: DescriptionBody):
    """Set your note about what this session is for (empty text clears it)."""
    if not _session_exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    descriptions.set_description(session_id, body.description)
    return {"session_id": session_id, "description": descriptions.get(session_id)}


@app.delete("/api/sessions/{session_id}/description")
def api_clear_description(session_id: str):
    descriptions.clear(session_id)
    return {"session_id": session_id, "description": None}


class SendBody(BaseModel):
    text: str
    permission_mode: str = "acceptEdits"


@app.post("/api/sessions/{session_id}/send")
async def api_send(session_id: str, body: SendBody):
    if parser.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    cwd = parser.session_cwd(session_id)

    async def event_stream():
        async for evt in runner.run_turn(
            session_id, body.text, cwd, body.permission_mode
        ):
            yield f"data: {json.dumps(evt)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/sessions/{session_id}/tmux")
def api_tmux(session_id: str):
    """Live tmux screen + any pending permission prompt for this session.

    `prompt` is non-null only when the live REPL is sitting at a Yes/No/... gate.
    """
    is_agy = agyparser.has_conversation(session_id)
    is_opencode = not is_agy and opencodeparser.has_session(session_id)
    # For agy, the console pane IS the conversation view (its .db store is lossy),
    # so capture full scrollback once and slice the visible frame from its tail
    # (gate/spinner/input live at the bottom) — one capture, not two.
    if is_agy:
        full = tmuxio.capture_pane(session_id, history=5000)
        if full is None:
            return {"session_id": session_id, "has_tmux": False, "prompt": None, "screen": None}
        screen = "\n".join(full.splitlines()[-60:])
    else:
        screen = tmuxio.capture_pane(session_id)
        if screen is None:
            return {"session_id": session_id, "has_tmux": False, "prompt": None, "screen": None}
        full = screen
    # Neither agy nor opencode gates are numbered menus — each has its own
    # parser. opencode's marks the selected option by colour alone, so its
    # parser re-captures the pane with ANSI kept (see tmuxio.opencode_pending).
    if is_agy:
        prompt = agyparser.parse_gate(screen)
    elif is_opencode:
        prompt = tmuxio.opencode_pending(session_id)
    else:
        prompt = None
    if prompt is None and not is_opencode:
        prompt = tmuxio.parse_prompt(screen)
    # opencode's spinner is bare animated dots — no "what it's thinking" text to
    # lift out of it, so it stays empty rather than showing noise.
    if is_agy:
        spinner = agyparser.spinner_line(screen)
    elif is_opencode:
        spinner = None
    else:
        spinner = tmuxio.spinner_line(screen)
    # Error/retry banner off the *visible* frame only, so it disappears from the
    # response as soon as the REPL recovers (see tmuxio.error_line).
    return {
        "session_id": session_id,
        "has_tmux": True,
        "prompt": prompt,
        "spinner": spinner,
        "error": tmuxio.error_line(screen),
        "screen": full or screen,
    }


PASTE_DIR = os.path.expanduser("~/.claude_dashboard_pastes")
_PASTE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
              "image/webp": "webp"}
_MAX_PASTE_BYTES = 20 * 1024 * 1024


class PasteBody(BaseModel):
    data: str           # base64 (with or without data: URI prefix)
    mime: str = "image/png"


@app.post("/api/sessions/{session_id}/paste")
def api_paste(session_id: str, body: PasteBody):
    """Save a pasted image to disk and return its path.

    The path is meant to be typed into the live REPL — Claude Code reads image
    files referenced by path in the prompt.
    """
    ext = _PASTE_EXT.get(body.mime)
    if ext is None:
        raise HTTPException(status_code=415, detail="unsupported image type")
    raw = body.data.split(",", 1)[-1]   # tolerate a data: URI prefix
    try:
        blob = base64.b64decode(raw, validate=True)
    except (ValueError, Exception):
        raise HTTPException(status_code=400, detail="bad base64")
    if not blob or len(blob) > _MAX_PASTE_BYTES:
        raise HTTPException(status_code=413, detail="image too large or empty")
    os.makedirs(PASTE_DIR, exist_ok=True)
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.{ext}"
    path = os.path.join(PASTE_DIR, name)
    with open(path, "wb") as f:
        f.write(blob)
    return {"path": path, "bytes": len(blob)}


@app.post("/api/sessions/{session_id}/spawn")
def api_spawn(session_id: str):
    """Start a live tmux session that resumes this Claude session, so /say and
    permission gates work against a live REPL."""
    if parser.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    cwd = parser.session_cwd(session_id)
    result = tmuxio.spawn(session_id, cwd)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "spawn failed"))
    return result


@app.get("/api/launchers")
def api_launchers():
    """Tmux launch scripts for the copy-paste popup. `claude` = runclaude_*.sh
    (per model); `agy` = runagy_*.sh (Antigravity, agy --conversation <id>)."""
    return {"launchers": models.launchers(), "agy": models.agy_launchers(),
            "grok": models.grok_launchers(),
            "opencode": models.opencode_launchers()}


@app.post("/api/sessions/{session_id}/usage")
def api_usage(session_id: str):
    """Run /usage in the live REPL and return its cost/limits panel. Routes to
    agy's Models & Quota screen or Claude Code's usage panel by session type.

    A session running an Ollama-hosted model (`…:cloud`) is the exception: the
    quota that matters is the ollama.com account's, not Claude Code's, so it
    comes from ollama.com and needs no live REPL."""
    summary = parser.get_summary(session_id)
    if summary and ollamausage.is_cloud_model(summary.get("model")):
        result = ollamausage.usage()
    elif grokparser.has_session(session_id):
        # Live → run /usage in the grok REPL and capture it; offline → the
        # on-disk signals.json snapshot.
        if session_id in tmuxio.tmux_sessions():
            result = tmuxio.grok_usage(session_id)
        else:
            result = grokparser.usage_text(session_id)
    elif opencodeparser.has_session(session_id):
        # opencode has no /usage command, but it records cost + tokens per
        # session in its db, so the panel is built from there. An Ollama-hosted
        # model defers to the ollama.com quota for the same reason as above.
        oc = opencodeparser.get_summary(session_id)
        if oc and ollamausage.is_cloud_model(oc.get("model")):
            result = ollamausage.usage()
        else:
            result = opencodeparser.usage_text(session_id)
    elif agyparser.has_conversation(session_id):
        result = tmuxio.agy_usage(session_id)
    else:
        result = tmuxio.usage(session_id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "usage failed"))
    return result


@app.get("/api/agy/models")
def api_agy_models():
    """The models the agy CLI offers (for the switch-model picker)."""
    return {"models": agyparser.model_options()}


class AgyModelBody(BaseModel):
    model: str


@app.post("/api/sessions/{session_id}/agy-model")
def api_agy_set_model(session_id: str, body: AgyModelBody):
    """Switch a live agy session's model via its /model picker (saved by agy)."""
    result = tmuxio.agy_set_model(session_id, body.model)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "set model failed"))
    return result


class AgyAnswerBody(BaseModel):
    action: str          # approve | manage | reject


@app.post("/api/sessions/{session_id}/agy-answer")
def api_agy_answer(session_id: str, body: AgyAnswerBody):
    """Answer an agy approval gate via its key chord (approve=C-k, manage=M-j,
    reject=Esc)."""
    result = tmuxio.agy_answer(session_id, body.action)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "agy answer failed"))
    return result


@app.post("/api/sessions/{session_id}/interrupt")
def api_interrupt(session_id: str):
    """Stop the current turn on the live REPL (sends Esc)."""
    result = tmuxio.interrupt(session_id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "interrupt failed"))
    return result


@app.post("/api/sessions/{session_id}/kill")
def api_kill(session_id: str):
    """Shut down the live tmux session (ends its Claude REPL). Irreversible."""
    result = tmuxio.kill(session_id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "kill failed"))
    return result


@app.post("/api/tmux/kill-all")
def api_kill_all():
    """Shut down every live tmux session (ends all their REPLs). Irreversible."""
    live = tmuxio.tmux_sessions()
    killed, failed = [], []
    for sid in live:
        (killed if tmuxio.kill(sid).get("ok") else failed).append(sid)
    return {"killed": killed, "failed": failed, "count": len(killed)}


class SayBody(BaseModel):
    text: str


@app.post("/api/sessions/{session_id}/say")
def api_say(session_id: str, body: SayBody):
    """Type a message into the live tmux REPL (continuous conversation).

    Use this for sessions running in tmux instead of /send (which forks a
    separate headless `claude --resume`).
    """
    # grok's editor debounces keystrokes — a plain type+Enter often lands the
    # Enter as a newline instead of a submit. grok_say settles + verifies.
    if grokparser.has_session(session_id):
        result = tmuxio.grok_say(session_id, body.text)
    elif opencodeparser.has_session(session_id):
        result = tmuxio.opencode_say(session_id, body.text)
    else:
        result = tmuxio.say(session_id, body.text)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "say failed"))
    return result


class AnswerBody(BaseModel):
    choice: int
    text: str = ""


@app.post("/api/sessions/{session_id}/answer")
def api_answer(session_id: str, body: AnswerBody):
    """Answer a live permission prompt by selecting a numbered option.

    For a "No, and tell Claude what to do differently" option, include `text`
    to type the follow-up guidance after selecting it.

    opencode's gate is a horizontal row of options; picking "Reject" there opens
    a box asking what to do instead, and `text` is typed into it.
    """
    if opencodeparser.has_session(session_id):
        result = tmuxio.opencode_answer(session_id, body.choice, body.text)
    else:
        result = tmuxio.answer(session_id, body.choice, body.text)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "answer failed"))
    return result


class AnswerMultiBody(BaseModel):
    nums: list[int] = []


@app.post("/api/sessions/{session_id}/answer-multi")
def api_answer_multi(session_id: str, body: AnswerMultiBody):
    """Answer a live multiSelect (checkbox) question: tick `nums`, then Submit.

    A digit only toggles a checkbox in that widget, so this can't go through
    /answer — it ticks each option, walks the cursor to Submit, and confirms.
    """
    result = tmuxio.answer_multi(session_id, body.nums)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "answer failed"))
    return result


class CompactBody(BaseModel):
    instructions: str = ""


@app.post("/api/sessions/{session_id}/compact")
def api_compact(session_id: str, body: CompactBody = CompactBody()):
    """Run /compact on the live tmux REPL to shrink its context window.

    Optional `instructions` focus what the summary keeps.

    grok has its own /compact and the same command shape, so it only needs the
    grok submit path — its editor debounces keystrokes and swallows an Enter
    sent too soon after the text (see tmuxio.grok_say).

    opencode 1.18 has no /compact — it isn't in the command palette — so there
    is nothing to run there.
    """
    if opencodeparser.has_session(session_id):
        raise HTTPException(status_code=400,
                            detail="opencode has no /compact command")
    if grokparser.has_session(session_id):
        cmd = "/compact"
        if body.instructions.strip():
            cmd += " " + body.instructions.strip()
        result = tmuxio.grok_say(session_id, cmd)
    else:
        result = tmuxio.compact(session_id, body.instructions)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "compact failed"))
    return result


@app.post("/api/sessions/{session_id}/reset")
def api_reset(session_id: str):
    """Start a fresh conversation on the live REPL, following it to its new id.

    /clear (Claude Code) and /new (grok) start that conversation under a *new*
    session id while the tmux session keeps the old name — which is what
    silently strands the dashboard (see tmuxio.reset / tmuxio.grok_reset). Here
    we run it, learn the new id, rename the tmux onto
    it, then carry the things that describe the *work* rather than the
    conversation — your title, your note, your still-pending to-dos, project
    tags, autonomy level, and its place in the To-do inbox — across to it.

    Then we retire the old id: unpinned by the same migration, any tmux still
    answering to that name killed, and archived. It's a frozen stub transcript
    from here on, and leaving it in the inbox and on the board alongside its own
    continuation is how you end up driving the dead half of a session. The
    caller gets the new id and should navigate there.

    Not available for agy: its conversations aren't identified by anything it
    creates on reset, so there'd be no new id to follow.

    Not available for opencode either, for the mirror-image reason: it does have
    /new, but it doesn't write the session row until the first turn of that new
    conversation lands, so there is no new id to follow at reset time — we'd
    rename nothing and strand the tmux under the old name.
    """
    if agyparser.has_conversation(session_id):
        raise HTTPException(status_code=400,
                            detail="reset is Claude Code and grok only")
    if opencodeparser.has_session(session_id):
        raise HTTPException(
            status_code=400,
            detail="reset is Claude Code and grok only — opencode does not "
                   "register a new session until its first turn")
    if grokparser.has_session(session_id):
        # grok keys sessions by directory, so the thing to watch is the
        # project's folder — the parent of this session's own.
        sdir = grokparser.session_dir(session_id)
        if not sdir:
            raise HTTPException(status_code=404, detail="session not found")
        result = tmuxio.grok_reset(session_id, os.path.dirname(sdir))
    else:
        path = parser.session_path(session_id)
        if not path:
            raise HTTPException(status_code=404, detail="session not found")
        result = tmuxio.reset(session_id, os.path.dirname(path))
    new_id = result.get("session_id")
    # Migrate on any run that produced a new id, even a failed rename — the
    # conversation moved regardless, and leaving the state on the dead id is
    # the exact loss this endpoint exists to prevent.
    if new_id and new_id != session_id:
        for store in (overrides, descriptions, tasks, projects, autonomy, attention):
            store.rekey(session_id, new_id)
    if not result.get("ok"):
        # Retiring the old id is deliberately *not* done here. A failed rename
        # means the live REPL still answers to the old name, and archiving or
        # killing that would take a working session out from under you.
        raise HTTPException(status_code=409, detail=result.get("error", "reset failed"))
    # The rename landed, so the REPL is under new_id and anything still called
    # old_id is a stale leftover — kill is a no-op in the normal case.
    tmuxio.kill(session_id)
    archives.set_archived(session_id, True)
    return result


# ---------------------------------------------------------------------------
# Triage — the single inbox of sessions that need you (gated or WAITING),
# longest-waiting first. Includes the live prompt so the view can answer inline.
# ---------------------------------------------------------------------------
@app.get("/api/triage")
def api_triage():
    arch_ids = archives.archived_ids()
    data = parser.list_sessions(limit=None, archived_ids=arch_ids,
                                archived_mode="exclude")
    gated = tmuxio.pending_ids()
    live_tmux = tmuxio.tmux_sessions()
    marked = attention.marked_ids()
    web_mtimes, running, titles = registry.web_mtimes(), runner.running_ids(), overrides.all_titles()
    levels = autonomy.all()
    proj_map = projects.tags_by_session()
    task_counts = tasks.counts_by_session()
    out = []
    for s in data["sessions"]:
        sid = s["session_id"]
        is_gated = sid in gated
        is_live = sid in live_tmux
        is_marked = sid in marked
        # Attention page = only live tmux sessions plus ones the user manually
        # pinned. (A gated session always has a live REPL, so it's covered.)
        if not (is_live or is_marked):
            continue
        _decorate(s, web_mtimes, running, titles, arch_ids,
                  live_ids=live_tmux, marked=marked)
        s["pending_approval"] = is_gated
        s["autonomy"] = levels.get(sid, autonomy.DEFAULT)
        s["prompt"] = tmuxio.pending(sid) if is_gated else None
        s["projects"] = proj_map.get(sid, [])
        s["task_count"] = task_counts.get(sid, 0)
        out.append(s)

    # Include agy conversations that are live or manually pinned.
    for s in _agy_summaries(titles, arch_ids, marked, "exclude", live_tmux):
        if not (s["live_tmux"] or s["attention"]):
            continue
        if s.get("pending_approval"):
            s["prompt"] = tmuxio.pending(s["session_id"])
        s["projects"] = proj_map.get(s["session_id"], [])
        s["task_count"] = task_counts.get(s["session_id"], 0)
        out.append(s)

    # Include grok sessions that are live (tmux running) or manually pinned.
    for s in _grok_summaries(titles, arch_ids, marked, "exclude", live_tmux):
        if not (s["live_tmux"] or s["attention"]):
            continue
        s["projects"] = proj_map.get(s["session_id"], [])
        s["task_count"] = task_counts.get(s["session_id"], 0)
        out.append(s)

    # Include opencode sessions that are live (tmux running) or manually pinned.
    # _opencode_summaries already set pending_approval off the live pane, so a
    # gated one only needs its prompt read out of opencode's own gate widget.
    for s in _opencode_summaries(titles, arch_ids, marked, "exclude", live_tmux):
        if not (s["live_tmux"] or s["attention"]):
            continue
        if s.get("pending_approval"):
            s["prompt"] = tmuxio.opencode_pending(s["session_id"])
        s["projects"] = proj_map.get(s["session_id"], [])
        s["task_count"] = task_counts.get(s["session_id"], 0)
        out.append(s)

    # Gated (needs-approval) always on top; otherwise A→Z by title.
    out.sort(key=lambda x: (not x.get("pending_approval", False),
                            (x.get("title") or "").lower()))
    return {"sessions": out, "total": len(out),
            "autonomy_paused": autonomy.is_paused()}


# ---------------------------------------------------------------------------
# Autonomy — per-session trust level + a global pause kill switch.
# ---------------------------------------------------------------------------
@app.get("/api/autonomy")
def api_autonomy():
    return {"levels": autonomy.all(), "paused": autonomy.is_paused(),
            "env_disabled": autonomy.env_disabled(), "options": list(autonomy.LEVELS)}


class AutonomyBody(BaseModel):
    level: str


@app.put("/api/sessions/{session_id}/autonomy")
def api_set_autonomy(session_id: str, body: AutonomyBody):
    if body.level not in autonomy.LEVELS:
        raise HTTPException(status_code=400,
                            detail=f"level must be one of {autonomy.LEVELS}")
    autonomy.set(session_id, body.level)
    return {"session_id": session_id, "autonomy": body.level}


class PauseBody(BaseModel):
    paused: bool


@app.post("/api/autonomy/pause")
def api_autonomy_pause(body: PauseBody):
    return {"paused": autonomy.set_paused(body.paused),
            "env_disabled": autonomy.env_disabled()}


# ---------------------------------------------------------------------------
# Dispatch — spawn a brand-new Claude session for a task, in tmux.
# ---------------------------------------------------------------------------
class DispatchBody(BaseModel):
    cwd: str
    prompt: str
    model: str = "opus"
    autonomy: str = "manual"


@app.post("/api/dispatch")
def api_dispatch(body: DispatchBody):
    if body.autonomy not in autonomy.LEVELS:
        raise HTTPException(status_code=400,
                            detail=f"autonomy must be one of {autonomy.LEVELS}")
    result = tmuxio.dispatch(body.cwd, body.prompt, model=body.model)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "dispatch failed"))
    sid = result["session_id"]
    if body.autonomy != autonomy.DEFAULT:
        autonomy.set(sid, body.autonomy)
    result["autonomy"] = body.autonomy
    return result


# ---------------------------------------------------------------------------
# Relay — structured session-to-session messaging over the file bus.
# ---------------------------------------------------------------------------
@app.get("/api/relay/sources")
def api_relay_sources():
    """Live tmux sessions usable as a relay sender, with best-effort titles."""
    live = tmuxio.tmux_sessions()
    titles = overrides.all_titles()
    data = parser.list_sessions(limit=None)
    known = {s["session_id"]: s for s in data["sessions"]}
    out = []
    for sid in sorted(live):
        s = known.get(sid, {})
        out.append({
            "session_id": sid,
            "title": titles.get(sid) or s.get("title") or s.get("cwd") or sid[:8],
        })
    return {"sources": out}


class RelayBody(BaseModel):
    from_id: str
    to_id: str
    message: str


@app.post("/api/relay")
def api_relay(body: RelayBody):
    result = tmuxio.relay(body.from_id, body.to_id, body.message)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "relay failed"))
    return result


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/session.html")
def session_page():
    return FileResponse(os.path.join(STATIC_DIR, "session.html"))


@app.get("/search.html")
def search_page():
    return FileResponse(os.path.join(STATIC_DIR, "search.html"))


@app.get("/archived.html")
def archived_page():
    return FileResponse(os.path.join(STATIC_DIR, "archived.html"))


@app.get("/world.html")
def world_page():
    return FileResponse(os.path.join(STATIC_DIR, "world.html"))


@app.get("/triage.html")
def triage_page():
    return FileResponse(os.path.join(STATIC_DIR, "triage.html"))


@app.get("/projects.html")
def projects_page():
    return FileResponse(os.path.join(STATIC_DIR, "projects.html"))


@app.get("/favicon.ico")
def favicon():
    # Browsers ask for /favicon.ico even when a page links an SVG icon.
    return FileResponse(
        os.path.join(STATIC_DIR, "favicon.svg"), media_type="image/svg+xml"
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
