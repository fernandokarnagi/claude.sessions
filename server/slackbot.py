"""
slackbot.py — Slack Socket Mode integration for the Claude Sessions dashboard.

What it does (only when configured — see env vars below):
  * watches live tmux sessions; when one hits a permission gate (Yes/No/...),
    posts the command/context + answer buttons to a Slack channel
  * lets you answer from Slack — buttons call tmuxio.answer(); the
    "tell Claude what to do differently" option opens a modal for free text
  * notifies the channel when a session transitions into WAITING
  * /pending slash command lists sessions currently sitting at a gate

Socket Mode = no public URL; the bot dials out to Slack over a WebSocket.

Required env vars (all must be set, else this module is a no-op):
  SLACK_BOT_TOKEN   xoxb-...   bot token (chat:write, commands)
  SLACK_APP_TOKEN   xapp-...   app-level token with connections:write
  SLACK_CHANNEL     C012...     channel id for proactive posts
Optional:
  DASHBOARD_URL     http://127.0.0.1:8765   base for "open in dashboard" links
  SLACK_POLL_SECS   4           watcher poll interval
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Optional

from . import autonomy, overrides, parser, tmuxio

BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
CHANNEL = os.environ.get("SLACK_CHANNEL", "")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://127.0.0.1:8765").rstrip("/")
POLL_SECS = float(os.environ.get("SLACK_POLL_SECS", "4"))

_CTX_MAX = 1400          # chars of command context to include in a Slack post
_NEEDS_TEXT_RE = ("tell claude", "differently", "what to do")

# posted gates: sid -> {"sig", "ts", "channel"}; tracked so we update on resolve
_gates: dict[str, dict] = {}
# last seen status per session, for WAITING-transition notifications
_statuses: dict[str, str] = {}
# one Slack thread per session: sid -> {"channel", "ts"} (ts == thread root)
_threads: dict[str, dict] = {}
# transcript byte offset already mirrored to Slack, per session
_offsets: dict[str, int] = {}
THREADS_FILE = os.path.join(os.path.dirname(__file__), ".slack_threads.json")
_started = False
_bolt = None   # the slack_bolt App, built in start()


def enabled() -> bool:
    return bool(BOT_TOKEN and APP_TOKEN and CHANNEL)


def _load_threads() -> None:
    try:
        with open(THREADS_FILE) as f:
            data = json.load(f)
        _threads.update(data.get("threads", {}))
    except Exception:
        return
    # Don't replay history written before this run — start from current EOF.
    for sid in _threads:
        p = parser.session_path(sid)
        try:
            _offsets[sid] = os.path.getsize(p) if p and os.path.exists(p) else 0
        except Exception:
            _offsets[sid] = 0


def _save_threads() -> None:
    try:
        with open(THREADS_FILE, "w") as f:
            json.dump({"threads": _threads}, f)
    except Exception as e:
        print(f"[slack] save threads failed: {e}")


def _sid_for_thread(thread_ts: str) -> Optional[str]:
    for sid, t in _threads.items():
        if t.get("ts") == thread_ts:
            return sid
    return None


def _ensure_thread(sid: str) -> dict:
    """The Slack thread bound to a session — creating its root post if needed."""
    t = _threads.get(sid)
    if t:
        return t
    resp = _bolt.client.chat_postMessage(
        channel=CHANNEL,
        text=f":thread: *{_title(sid)}* · <{_link(sid)}|open in dashboard>\n"
             "_Reply in this thread to talk to the session._",
    )
    t = {"channel": resp["channel"], "ts": resp["ts"]}
    _threads[sid] = t
    p = parser.session_path(sid)
    try:
        _offsets[sid] = os.path.getsize(p) if p and os.path.exists(p) else 0
    except Exception:
        _offsets[sid] = 0
    _save_threads()
    return t


_BOT_UID: Optional[str] = None       # our bot's user id (to skip own msgs)
_chan_seen: Optional[str] = None     # last channel ts processed (mentions)
_thread_seen: dict[str, str] = {}    # thread root ts -> last reply ts processed
_inbound_seeded = False              # skip backlog on first poll


def _sessions_blocks(limit: int = 12) -> list:
    """Block Kit: one row per active session with an 'Open' button."""
    ss = [s for s in parser.list_sessions(limit=limit)["sessions"] if s.get("status") != "ENDED"]
    if not ss:
        return [{"type": "section", "text": {"type": "mrkdwn", "text": "No active sessions."}}]
    blocks: list = [{"type": "section", "text": {"type": "mrkdwn",
        "text": "*Active sessions* — tap *Open* to see the latest message and continue:"}}]
    for s in ss:
        sid = s["session_id"]
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                "text": f"*{_title(sid)}*  ·  `{s.get('status')}`"},
            "accessory": {
                "type": "button",
                "text": {"type": "plain_text", "text": "💬 Open"},
                "action_id": "open_session",
                "value": sid,
            },
        })
    return blocks


def _latest_assistant(sid: str) -> str:
    """The session's most recent assistant message text (from the transcript)."""
    p = parser.session_path(sid)
    if not p or not os.path.exists(p):
        return "(no transcript)"
    try:
        size = os.path.getsize(p)
        acts, _ = parser.tail_activities(p, max(0, size - 20000))
        texts = [a.get("text") for a in acts
                 if a.get("kind") == "assistant" and a.get("text")]
        return texts[-1] if texts else "(no recent assistant message)"
    except Exception:
        return "(could not read transcript)"


def _answer_query(text: str) -> str:
    """Plain-text reply for a simple query (pending / help)."""
    low = text.lower().strip()
    if not low or "pending" in low or "approv" in low:
        return _gated_text()
    return ("Try `pending` or `sessions`, or reply inside a session thread to "
            "talk to that session.")


def _poll_inbound() -> None:
    """Read new human messages via the Web API (no Events API needed).

    - replies inside a bound session thread -> typed into that session
    - @mentions in the channel -> answered (pending / sessions / help)
    """
    global _BOT_UID, _chan_seen, _inbound_seeded
    if _BOT_UID is None:
        try:
            _BOT_UID = _bolt.client.auth_test()["user_id"]
        except Exception:
            return

    # First pass: mark everything already in the channel/threads as seen, so we
    # don't reply to backlog from before the server started.
    if not _inbound_seeded:
        try:
            h = _bolt.client.conversations_history(channel=CHANNEL, limit=1)
            ms = h.get("messages", [])
            _chan_seen = ms[0]["ts"] if ms else None
        except Exception:
            pass
        for sid, t in list(_threads.items()):
            try:
                r = _bolt.client.conversations_replies(channel=t["channel"], ts=t["ts"], limit=50)
                ms = r.get("messages", [])
                _thread_seen[t["ts"]] = ms[-1]["ts"] if ms else t["ts"]
            except Exception:
                _thread_seen[t["ts"]] = t["ts"]
        _inbound_seeded = True
        return

    # 1. thread replies for each bound session
    for sid, t in list(_threads.items()):
        try:
            root = t["ts"]
            r = _bolt.client.conversations_replies(channel=t["channel"], ts=root, limit=50)
            msgs = r.get("messages", [])[1:]   # skip the root
            last = _thread_seen.get(root, root)
            newest = last
            for m in msgs:
                ts = m.get("ts", "")
                if ts <= last:
                    continue
                newest = max(newest, ts)
                if m.get("bot_id") or m.get("user") == _BOT_UID or m.get("subtype"):
                    continue
                text = re.sub(r"<@[^>]+>", "", m.get("text") or "").strip()
                if text:
                    res = tmuxio.say(sid, text)
                    print(f"[slack] inbound -> {sid[:8]}: {text[:60]!r} ok={res.get('ok')}")
            _thread_seen[root] = newest
        except Exception as e:
            print(f"[slack] poll thread {sid[:8]} failed: {e}")

    # 2. @mentions anywhere (top level OR inside a thread, bound or not)
    try:
        kw = {"channel": CHANNEL, "limit": 20}
        if _chan_seen:
            kw["oldest"] = _chan_seen
        h = _bolt.client.conversations_history(**kw)
        msgs = sorted(h.get("messages", []), key=lambda m: m.get("ts", ""))
        for m in msgs:
            ts = m.get("ts", "")
            if _chan_seen and ts <= _chan_seen:
                continue
            _chan_seen = ts if not _chan_seen else max(_chan_seen, ts)
            if m.get("bot_id") or m.get("user") == _BOT_UID:
                continue
            text = m.get("text") or ""
            tts = m.get("thread_ts")
            mentioned = _BOT_UID and f"<@{_BOT_UID}>" in text
            sid = _sid_for_thread(tts) if tts else None
            clean = re.sub(r"<@[^>]+>", "", text).strip()
            if sid and clean:
                # message in a bound session thread -> talk to the session
                res = tmuxio.say(sid, clean)
                print(f"[slack] inbound -> {sid[:8]}: {clean[:60]!r} ok={res.get('ok')}")
            elif mentioned:
                low = clean.lower()
                if "session" in low or "list" in low:
                    _bolt.client.chat_postMessage(
                        channel=CHANNEL, thread_ts=tts or ts,
                        text="Active sessions", blocks=_sessions_blocks())
                else:
                    _bolt.client.chat_postMessage(
                        channel=CHANNEL, thread_ts=tts or ts, text=_answer_query(clean))
                print(f"[slack] mention answered: {clean[:50]!r}")
    except Exception as e:
        print(f"[slack] poll mentions failed: {e}")


def _flush_replies(sid: str) -> None:
    """Mirror new assistant replies from the transcript into the Slack thread."""
    t = _threads.get(sid)
    if not t:
        return
    p = parser.session_path(sid)
    if not p:
        return
    try:
        acts, new_off = parser.tail_activities(p, _offsets.get(sid, 0))
    except Exception:
        return
    _offsets[sid] = new_off
    for a in acts:
        if a.get("kind") != "assistant":
            continue   # skip user echoes, tool calls, thinking — post replies only
        txt = (a.get("text") or "").strip()
        if not txt:
            continue
        try:
            _bolt.client.chat_postMessage(
                channel=t["channel"], thread_ts=t["ts"], text=txt[:3500])
        except Exception as e:
            print(f"[slack] reply post failed: {e}")


def _title(sid: str) -> str:
    t = overrides.all_titles().get(sid)
    if t:
        return t
    s = parser.get_summary(sid)
    return (s or {}).get("title") or sid[:8]


def _link(sid: str) -> str:
    return f"{DASHBOARD_URL}/session.html?id={sid}"


def _needs_text(label: str) -> bool:
    low = label.lower()
    return any(k in low for k in _NEEDS_TEXT_RE)


def _sig(prompt: dict) -> str:
    return prompt["question"] + "|" + "|".join(
        f"{o['num']}{o['label']}" for o in prompt["options"])


def gate_blocks(sid: str, prompt: dict, answered: Optional[str] = None) -> list:
    """Slack Block Kit blocks for one permission gate."""
    title = _title(sid)
    ctx = (prompt.get("context") or "").strip()
    if len(ctx) > _CTX_MAX:
        ctx = ctx[:_CTX_MAX] + "\n… (truncated)"
    blocks: list = [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f":warning: *Needs approval* — <{_link(sid)}|{title}>\n*{prompt['question']}*"}},
    ]
    if ctx:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"```{ctx}```"}})
    if answered is not None:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f":white_check_mark: answered: *{answered}*"}]})
        return blocks
    btns = []
    for o in prompt["options"]:
        style = "primary" if o["num"] == 1 else ("danger" if o["label"].lower().startswith("no") else None)
        btn = {
            "type": "button",
            "text": {"type": "plain_text", "text": f"{o['num']}. {o['label']}"[:75]},
            "action_id": f"appr_{o['num']}",
            "value": json.dumps({"sid": sid, "choice": o["num"], "needs_text": _needs_text(o["label"])}),
        }
        if style:
            btn["style"] = style
        btns.append(btn)
    blocks.append({"type": "actions", "elements": btns})
    return blocks


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------

def _post_gate(sid: str, prompt: dict) -> None:
    try:
        t = _ensure_thread(sid)
        resp = _bolt.client.chat_postMessage(
            channel=t["channel"], thread_ts=t["ts"],
            text=f"Needs approval: {prompt['question']}",
            blocks=gate_blocks(sid, prompt),
        )
        _gates[sid] = {"sig": _sig(prompt), "ts": resp["ts"], "channel": resp["channel"]}
    except Exception as e:
        print(f"[slack] post_gate failed: {e}")


def _resolve_gate(sid: str, answered: str = "closed") -> None:
    g = _gates.pop(sid, None)
    if not g:
        return
    try:
        _bolt.client.chat_update(
            channel=g["channel"], ts=g["ts"],
            text="Approval resolved",
            blocks=[{"type": "context", "elements": [
                {"type": "mrkdwn", "text": f":white_check_mark: <{_link(sid)}|{_title(sid)}> — {answered}"}]}],
        )
    except Exception as e:
        print(f"[slack] resolve_gate failed: {e}")


def _auto_answer_note(sid: str, level: str, choice: int, prompt: dict) -> None:
    """Mirror an autonomy auto-answer into Slack — only for sessions the user
    has already opened a thread for, so autonomous sessions don't spam new
    threads into the channel."""
    if sid not in _threads:
        return
    try:
        t = _threads[sid]
        q = (prompt.get("question") or "").strip()
        _bolt.client.chat_postMessage(
            channel=t["channel"], thread_ts=t["ts"],
            text=f":robot_face: auto-approved (*{level}*): option {choice}"
                 + (f" — {q[:200]}" if q else ""))
    except Exception as e:
        print(f"[slack] auto_answer_note failed: {e}")


def _notify_waiting(sid: str) -> None:
    try:
        t = _ensure_thread(sid)
        _bolt.client.chat_postMessage(
            channel=t["channel"], thread_ts=t["ts"],
            text=":bell: now *waiting* for your reply — reply in this thread.",
        )
    except Exception as e:
        print(f"[slack] notify_waiting failed: {e}")


def _watch() -> None:
    seeded = False
    beat = 0
    while True:
        try:
            beat += 1
            if beat % 15 == 1:
                print(f"[slack] watch alive (threads={len(_threads)}, seen_chan={_chan_seen})")
            sessions = parser.list_sessions(limit=None)["sessions"]
            for s in sessions:
                sid, st = s["session_id"], s.get("status")
                prev = _statuses.get(sid)
                if seeded and st == "WAITING" and prev != "WAITING":
                    _notify_waiting(sid)
                _statuses[sid] = st

            gated = tmuxio.pending_ids()
            for sid in gated - set(_gates):
                # Sessions on auto-safe/yolo are handled by the autonomy
                # watcher; only post a manual gate for the human to answer.
                if autonomy.get(sid) != "manual":
                    continue
                p = tmuxio.pending(sid)
                if p:
                    _post_gate(sid, p)
            for sid in set(_gates) - gated:
                _resolve_gate(sid, "answered / dismissed in terminal")

            # Mirror new assistant replies for any session bound to a thread.
            for sid in list(_threads):
                _flush_replies(sid)
            # Read inbound human messages via Web API (no Events API needed).
            _poll_inbound()
            seeded = True
        except Exception as e:
            print(f"[slack] watch loop error: {e}")
        time.sleep(POLL_SECS)


# ---------------------------------------------------------------------------
# Bolt app (commands + interactivity)
# ---------------------------------------------------------------------------

def _ssl_context():
    """SSL context backed by certifi — python.org builds ship without CA certs,
    so default verification fails against Slack otherwise."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _gated_text() -> str:
    gated = tmuxio.pending_ids()
    items = [(sid, tmuxio.pending(sid)) for sid in gated]
    items = [(sid, p) for sid, p in items if p]
    if not items:
        return ":tada: Nothing pending approval right now."
    lines = [f"*{len(items)} pending approval:*"]
    for sid, p in items:
        lines.append(f"• <{_link(sid)}|{_title(sid)}> — {p['question']}")
    return "\n".join(lines)


def _build_app():
    from slack_bolt import App
    from slack_sdk import WebClient
    client = WebClient(token=BOT_TOKEN, ssl=_ssl_context())
    # Skip the build-time auth.test network call; we connect via Socket Mode.
    app = App(client=client, token_verification_enabled=False)
    # NOTE: inbound messages/mentions are handled by _poll_inbound (Web API
    # polling), NOT by Events API handlers — registering @app.event here too
    # would double-answer when the Events API is also enabled.

    def _do_answer(sid: str, choice: int, text: str = "") -> dict:
        return tmuxio.answer(sid, choice, text)

    # One handler covers all numbered option buttons (action_id appr_1, appr_2…).
    def on_choice(ack, body, action, client):
        ack()
        v = json.loads(action["value"])
        sid, choice, needs = v["sid"], v["choice"], v.get("needs_text")
        if needs:
            # Open a modal to collect the "what to do differently" text.
            try:
                client.views_open(
                    trigger_id=body["trigger_id"],
                    view={
                        "type": "modal",
                        "callback_id": "appr_text",
                        "private_metadata": json.dumps({
                            "sid": sid, "choice": choice,
                            "channel": body["channel"]["id"], "ts": body["message"]["ts"]}),
                        "title": {"type": "plain_text", "text": "Tell Claude"},
                        "submit": {"type": "plain_text", "text": "Send"},
                        "blocks": [{
                            "type": "input", "block_id": "t",
                            "label": {"type": "plain_text", "text": "What to do differently"},
                            "element": {"type": "plain_text_input", "action_id": "v", "multiline": True},
                        }],
                    },
                )
            except Exception as e:
                print(f"[slack] views_open failed: {e}")
            return
        _do_answer(sid, choice)
        try:
            p = tmuxio.pending(sid)
            client.chat_update(
                channel=body["channel"]["id"], ts=body["message"]["ts"],
                text="Approval answered",
                blocks=gate_blocks(sid, p or {"question": "", "context": "", "options": []},
                                   answered=f"option {choice}"))
        except Exception as e:
            print(f"[slack] chat_update failed: {e}")
        _gates.pop(sid, None)

    for n in range(1, 10):
        app.action(f"appr_{n}")(on_choice)

    @app.view("appr_text")
    def on_text_submit(ack, body, view, client):
        ack()
        meta = json.loads(view["private_metadata"])
        text = view["state"]["values"]["t"]["v"]["value"] or ""
        _do_answer(meta["sid"], meta["choice"], text)
        try:
            client.chat_update(
                channel=meta["channel"], ts=meta["ts"], text="Approval answered",
                blocks=gate_blocks(meta["sid"],
                                   tmuxio.pending(meta["sid"]) or {"question": "", "context": "", "options": []},
                                   answered=f"option {meta['choice']}: {text[:60]}"))
        except Exception as e:
            print(f"[slack] chat_update (modal) failed: {e}")
        _gates.pop(meta["sid"], None)

    @app.command("/pending")
    def pending_cmd(ack, respond):
        ack()
        gated = tmuxio.pending_ids()
        prompts = [(sid, tmuxio.pending(sid)) for sid in gated]
        prompts = [(sid, p) for sid, p in prompts if p]
        if not prompts:
            respond("No sessions are waiting for approval. :tada:")
            return
        blocks: list = [{"type": "section", "text": {"type": "mrkdwn",
            "text": f"*{len(prompts)} session(s) need approval:*"}}]
        for sid, p in prompts:
            blocks.append({"type": "divider"})
            blocks.extend(gate_blocks(sid, p))
        respond(blocks=blocks, text=f"{len(prompts)} session(s) need approval")

    @app.command("/sessions")
    def sessions_cmd(ack, respond):
        ack()
        respond(blocks=_sessions_blocks(), text="Active sessions")

    @app.action("open_session")
    def open_session(ack, body, action, client):
        ack()
        sid = action["value"]
        t = _ensure_thread(sid)
        # Resume the session in tmux (best-effort, in background — can take secs).
        cwd = parser.session_cwd(sid)
        threading.Thread(target=tmuxio.spawn, args=(sid, cwd), daemon=True,
                         name=f"spawn-{sid[:8]}").start()
        latest = _latest_assistant(sid)
        try:
            client.chat_postMessage(
                channel=t["channel"], thread_ts=t["ts"],
                text=f":speech_balloon: *{_title(sid)}* — latest message:\n\n{latest[:3400]}"
                     "\n\n_Resuming live session… reply in this thread to continue._")
        except Exception as e:
            print(f"[slack] open_session post failed: {e}")

    return app


def start() -> None:
    """Start the Socket Mode handler + watcher in background threads.

    No-op if Slack is not configured or slack_bolt isn't installed.
    """
    global _started, _bolt
    if _started or not enabled():
        if not enabled():
            print("[slack] not configured (SLACK_BOT_TOKEN/APP_TOKEN/CHANNEL) — disabled")
        return
    # Point the whole process's TLS at certifi (covers the Socket Mode
    # websocket too) when the interpreter has no usable CA bundle.
    try:
        import certifi
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    except Exception:
        pass
    _load_threads()
    autonomy.set_auto_answer_hook(_auto_answer_note)
    try:
        from slack_bolt.adapter.socket_mode import SocketModeHandler
        _bolt = _build_app()
        handler = SocketModeHandler(_bolt, APP_TOKEN)
    except Exception as e:
        print(f"[slack] failed to start (is slack_bolt installed?): {e}")
        return
    _started = True
    threading.Thread(target=handler.start, daemon=True, name="slack-socket").start()
    threading.Thread(target=_watch, daemon=True, name="slack-watch").start()
    print(f"[slack] started — posting to {CHANNEL}, polling every {POLL_SECS}s")
