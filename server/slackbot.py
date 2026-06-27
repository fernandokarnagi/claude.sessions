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
import threading
import time
from typing import Optional

from . import overrides, parser, tmuxio

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
_started = False
_bolt = None   # the slack_bolt App, built in start()


def enabled() -> bool:
    return bool(BOT_TOKEN and APP_TOKEN and CHANNEL)


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
        resp = _bolt.client.chat_postMessage(
            channel=CHANNEL,
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


def _notify_waiting(sid: str) -> None:
    try:
        _bolt.client.chat_postMessage(
            channel=CHANNEL,
            text=f":bell: <{_link(sid)}|{_title(sid)}> is now *waiting* for your reply.",
        )
    except Exception as e:
        print(f"[slack] notify_waiting failed: {e}")


def _watch() -> None:
    seeded = False
    while True:
        try:
            sessions = parser.list_sessions(limit=None)["sessions"]
            for s in sessions:
                sid, st = s["session_id"], s.get("status")
                prev = _statuses.get(sid)
                if seeded and st == "WAITING" and prev != "WAITING":
                    _notify_waiting(sid)
                _statuses[sid] = st

            gated = tmuxio.pending_ids()
            for sid in gated - set(_gates):
                p = tmuxio.pending(sid)
                if p:
                    _post_gate(sid, p)
            for sid in set(_gates) - gated:
                _resolve_gate(sid, "answered / dismissed in terminal")
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


def _build_app():
    from slack_bolt import App
    from slack_sdk import WebClient
    client = WebClient(token=BOT_TOKEN, ssl=_ssl_context())
    # Skip the build-time auth.test network call; we connect via Socket Mode.
    app = App(client=client, token_verification_enabled=False)

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
