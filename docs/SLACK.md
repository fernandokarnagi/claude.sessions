# Slack integration (Socket Mode)

Get a Slack ping when a session needs your approval or starts waiting, and
answer **Yes / No / …** straight from Slack. Uses **Socket Mode**, so the
dashboard needs no public URL — the bot dials out to Slack over a WebSocket.

## What you get

- **Auto-post** to a channel when a live tmux session hits a permission gate,
  showing the command/context + a button per option.
- **Answer from Slack** — click a button → it’s typed into the live tmux REPL.
  The “No, and tell Claude what to do differently” option opens a text modal.
- **Two-way chat** — each session gets its own **Slack thread**. Reply in the
  thread and it’s typed into the live session; the session’s assistant replies
  are mirrored back into the same thread.
- **`/pending`** — lists every session currently at a gate.
- **`/sessions`** — lists active sessions, each with a **💬 Talk** button that
  opens a thread you can converse in.
- **Waiting notice** — a message in the thread when a session enters `WAITING`.

## 1. Create the Slack app

1. https://api.slack.com/apps → **Create New App** → **From scratch**. Pick a
   workspace.
2. **Socket Mode** (left nav) → toggle **Enable Socket Mode** on. When prompted,
   create an **App-Level Token** with the `connections:write` scope. Copy it —
   this is `SLACK_APP_TOKEN` (`xapp-…`).
3. **OAuth & Permissions** → **Bot Token Scopes** → add:
   - `chat:write`
   - `commands`
   - `channels:history`  ← needed for two-way chat (read your thread replies)
   - `groups:history`    ← only if you’ll use it in a private channel
   - `reactions:write`   ← optional, for the ✓/✗ ack on your replies
4. **Slash Commands** → **Create New Command** twice: `/pending` and
   `/sessions` (Socket Mode → no Request URL needed).
5. **Interactivity & Shortcuts** → toggle **on** (no URL under Socket Mode —
   powers the buttons/modal).
6. **Event Subscriptions** → toggle **on**. Under **Subscribe to bot events**
   add `message.channels` (and `message.groups` for private channels). This is
   what delivers your thread replies. (Socket Mode → no Request URL.)
   After adding scopes/events you must **Reinstall** the app.
7. **Install App** (OAuth & Permissions → Install to Workspace). Copy the
   **Bot User OAuth Token** — this is `SLACK_BOT_TOKEN` (`xoxb-…`).
8. In Slack, create/choose a channel and **invite the bot**:
   `/invite @YourApp`. Get the channel id (channel → View details → bottom, or
   right-click → Copy link → the `C…` id) — this is `SLACK_CHANNEL`.

## 2. Configure + run

```bash
export SLACK_BOT_TOKEN=xoxb-...      # bot token
export SLACK_APP_TOKEN=xapp-...      # app-level token (connections:write)
export SLACK_CHANNEL=C0123456789     # channel id the bot was invited to
export DASHBOARD_URL=http://127.0.0.1:8765   # optional, for message links
./serve.sh
```

On an existing venv, install the new dep once:

```bash
.venv/bin/pip install -r requirements.txt   # adds slack_bolt
```

If all three tokens are set you’ll see `[slack] started — posting to C…` in the
log. If not, the app logs `[slack] … disabled` and runs normally without Slack.

## Notes

- python.org Python builds ship without CA certs; the integration points TLS at
  `certifi` automatically (web API + the Socket Mode websocket).
- Answers go into the **live** tmux pane (same as the dashboard’s approval
  panel) — they do **not** spawn a separate headless `claude --resume`.
- Polling interval: `SLACK_POLL_SECS` (default 4s).
