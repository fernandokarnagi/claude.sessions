# Slack integration (Socket Mode)

Get a Slack ping when a session needs your approval or starts waiting, and
answer **Yes / No / …** straight from Slack. Uses **Socket Mode**, so the
dashboard needs no public URL — the bot dials out to Slack over a WebSocket.

## What you get

- **Auto-post** to a channel when a live tmux session hits a permission gate,
  showing the command/context + a button per option.
- **Answer from Slack** — click a button → it’s typed into the live tmux REPL.
  The “No, and tell Claude what to do differently” option opens a text modal.
- **`/pending`** slash command — lists every session currently at a gate.
- **Waiting notice** — a message when a session transitions into `WAITING`.

## 1. Create the Slack app

1. https://api.slack.com/apps → **Create New App** → **From scratch**. Pick a
   workspace.
2. **Socket Mode** (left nav) → toggle **Enable Socket Mode** on. When prompted,
   create an **App-Level Token** with the `connections:write` scope. Copy it —
   this is `SLACK_APP_TOKEN` (`xapp-…`).
3. **OAuth & Permissions** → **Bot Token Scopes** → add:
   - `chat:write`
   - `commands`
4. **Slash Commands** → **Create New Command**: command `/pending`, any short
   description. (Socket Mode → no Request URL needed.)
5. **Interactivity & Shortcuts** → toggle **on** (no URL needed under Socket
   Mode — this is what makes the buttons/modal work).
6. **Install App** (OAuth & Permissions → Install to Workspace). Copy the
   **Bot User OAuth Token** — this is `SLACK_BOT_TOKEN` (`xoxb-…`).
7. In Slack, create/choose a channel and **invite the bot**:
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
