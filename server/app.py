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

from . import (archives, autonomy, overrides, parser, registry, runner,
               slackbot, summaries, summarizer, tmuxio)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="Claude Sessions Dashboard")


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


def _decorate(summary: dict, web_mtimes: dict, running: set[str],
              titles: dict | None = None, archived: set | None = None) -> dict:
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

    archived = archived if archived is not None else archives.archived_ids()
    summary["archived"] = sid in archived

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
    return summary


# Sessions that are idle and waiting on the user (the "needs attention" set).
ATTENTION_STATUSES = {"WAITING", "SITTING", "SLEEPING"}


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
    levels = autonomy.all()
    for s in data["sessions"]:
        _decorate(s, web_mtimes, running, titles, arch_ids)
        sid = s["session_id"]
        s["pending_approval"] = sid in gated
        s["autonomy"] = levels.get(sid, autonomy.DEFAULT)
    return data


@app.get("/api/search")
def api_search(q: str = Query("")):
    web_mtimes, running, titles = registry.web_mtimes(), runner.running_ids(), overrides.all_titles()
    data = parser.search_sessions(q, extra_titles=titles)
    for s in data["sessions"]:
        _decorate(s, web_mtimes, running, titles)
    return data


@app.get("/api/sessions/{session_id}")
def api_session(session_id: str):
    detail = parser.get_session(session_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="session not found")
    _decorate(detail, registry.web_mtimes(), runner.running_ids())
    detail["autonomy"] = autonomy.get(session_id)
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
        raise HTTPException(status_code=404, detail="session not found")
    _decorate(s, registry.web_mtimes(), runner.running_ids())
    s["autonomy"] = autonomy.get(session_id)
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


@app.post("/api/sessions/{session_id}/archive")
def api_archive(session_id: str):
    if parser.session_path(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    archives.set_archived(session_id, True)
    return {"session_id": session_id, "archived": True}


@app.delete("/api/sessions/{session_id}/archive")
def api_unarchive(session_id: str):
    archives.set_archived(session_id, False)
    return {"session_id": session_id, "archived": False}


class TitleBody(BaseModel):
    title: str = ""


@app.put("/api/sessions/{session_id}/title")
def api_set_title(session_id: str, body: TitleBody):
    """Set a custom title override (empty title reverts to the original)."""
    if parser.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    overrides.set_title(session_id, body.title)
    return {"session_id": session_id, "title": overrides.get_title(session_id)}


@app.delete("/api/sessions/{session_id}/title")
def api_clear_title(session_id: str):
    """Remove the override, reverting to the transcript-derived title."""
    overrides.clear_title(session_id)
    return {"session_id": session_id, "title": None}


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
    screen = tmuxio.capture_pane(session_id)
    if screen is None:
        return {"session_id": session_id, "has_tmux": False, "prompt": None, "screen": None}
    return {
        "session_id": session_id,
        "has_tmux": True,
        "prompt": tmuxio.parse_prompt(screen),
        "screen": screen,
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


class SayBody(BaseModel):
    text: str


@app.post("/api/sessions/{session_id}/say")
def api_say(session_id: str, body: SayBody):
    """Type a message into the live tmux REPL (continuous conversation).

    Use this for sessions running in tmux instead of /send (which forks a
    separate headless `claude --resume`).
    """
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
    """
    result = tmuxio.answer(session_id, body.choice, body.text)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "answer failed"))
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
    web_mtimes, running, titles = registry.web_mtimes(), runner.running_ids(), overrides.all_titles()
    levels = autonomy.all()
    out = []
    for s in data["sessions"]:
        sid = s["session_id"]
        is_gated = sid in gated
        if not (is_gated or s.get("status") == "WAITING"):
            continue
        _decorate(s, web_mtimes, running, titles, arch_ids)
        s["pending_approval"] = is_gated
        s["autonomy"] = levels.get(sid, autonomy.DEFAULT)
        s["prompt"] = tmuxio.pending(sid) if is_gated else None
        out.append(s)
    out.sort(key=lambda x: x.get("mtime") or 0)   # oldest = longest waiting, on top
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


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
