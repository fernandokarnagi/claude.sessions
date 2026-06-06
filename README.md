# 🪐 Claude Sessions Dashboard

A local web app to **view, monitor, and drive Claude Code sessions** from your browser.

Claude Code records every session (interactive CLI, VS Code, or SDK) as a JSONL
transcript under `~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`. This
project reads those transcripts and gives you:

- a **dashboard** of all sessions with live status, model, token usage, and CLI/WEB origin;
- a **detail page** with the full conversation, **live status + history streaming**, inline
  rename, a **"what's expected from you"** summary, and a **chat box to resume/awaken** a session;
- **search** by title / session ID / path (case-insensitive, wildcards);
- a **"Needs attention"** view and **desktop notifications** when a session starts waiting on you;
- a standalone **CLI transcript viewer** (`watch_session.py`).

Everything runs locally, reads from `~/.claude/projects`, and **never modifies Claude
Code's own transcripts** (renames and other state live in separate JSON files).

---

## Quick start

```bash
# 1. Point the app at your Claude Code transcripts (defaults to ~/.claude/projects).
#    Only needed if your transcripts live somewhere else.
export CLAUDE_PROJECTS_DIR="$HOME/.claude/projects"

# 2. Launch (from the project directory)
./serve.sh                  # → http://127.0.0.1:8765
PORT=9000 ./serve.sh        # custom port
```

`serve.sh` creates a local `.venv`, installs deps on first run (FastAPI, uvicorn,
pytest), and starts uvicorn. Open the URL in your browser. Stop with **Ctrl+C**.

> **Set `CLAUDE_PROJECTS_DIR` before running.** This env var tells the app where Claude
> Code writes its session JSONL files. It defaults to `~/.claude/projects`, so if you use the
> standard location you can skip it. Override it (and export it in the same shell that runs
> `serve.sh` / `watch.sh`) when your transcripts live elsewhere — e.g. a custom `CLAUDE_CONFIG_DIR`,
> a different user's home, or a mounted backup:
>
> ```bash
> export CLAUDE_PROJECTS_DIR="/path/to/your/.claude/projects"
> ```

> **Tip:** the server sends `Cache-Control: no-store` and versions its static assets,
> so a normal reload always picks up changes. If a page ever looks stale, hard-refresh once
> (Cmd+Shift+R).

---

## Features

### Dashboard (`/`)
- Responsive grid of **session cards**: title, project path, status pill, model, total
  tokens, last activity time, and the **last 2 activities**.
- **Origin badge** — **CLI** (or VSCODE) vs **WEB**; a pulsing dot shows a session that's
  currently live. WEB means the dashboard drove it last; it flips back to CLI the moment the
  CLI writes to that session again.
- **Adaptive polling** — refreshes every ~1 s while any session is active, ~5 s when idle;
  pauses while the tab is hidden (unless notifications are on).
- **Show 10 / 25 / 50 / All** selector, sorted by most-recent activity.
- **⚠ Needs attention** toggle — filter to only sessions waiting on you (WAITING / SITTING /
  SLEEPING) across all projects.
- **🔔 Notify** toggle — desktop notification when a session goes THINKING → WAITING; clicking
  it jumps to that session. A test notification fires on enable so you know it works.

### Detail page (`/session.html?id=…`)
- Full header: status, model, created/updated, full token breakdown (input / output /
  cache-read / cache-creation / total), origin.
- **Live status** — the pill/tokens update as the CLI works (no reload).
- **Live history streaming** — new transcript events append to the top of the history as
  they're written, briefly highlighted; reads only new bytes (cheap even on multi-MB files).
- **"📋 What's expected from you"** — a one-paragraph LLM summary of what response the session
  is waiting for, generated lazily for waiting sessions and cached per waiting episode.
- **Inline rename** — ✎ to set a custom title, ↺ to revert to the original. Persisted,
  dashboard-only, never touches the transcript.
- **Chat box** — resume/awaken the session via headless `claude --resume`, streaming the
  reply live, with a permission-mode dropdown and a "now using <model>" badge.

### Search (`/search.html`)
- Match by **title, session ID, cwd/project path, or renamed title**.
- Case-insensitive; supports **glob wildcards** (`*docker*`, `build*`, `report?`). Plain text
  is a "contains" match.

### CLI transcript viewer
- `watch_session.py` / `watch.sh` — tail a running session's transcript in the terminal with
  colorized output. Independent of the web app.
  ```bash
  ./watch.sh --list          # list recent sessions
  ./watch.sh                 # follow the most recently active session
  ./watch.sh --all           # replay history, then follow
  ./watch.sh --session 9fb37a4a
  ```

---

## Status model

Status is inferred from **time since the transcript was last written** (logs have no explicit
"ended" marker). Tunable constants live at the top of `server/parser.py`:

| Status     | Idle time        | Constant            |
|------------|------------------|---------------------|
| THINKING   | < 30 s           | `THINKING_MAX_AGE`  |
| WAITING    | 30 s – 30 min    | `WAITING_MAX_AGE`   |
| SITTING    | 30 min – 2 h     | `SITTING_MAX_AGE`   |
| SLEEPING   | 2 h – 24 h       | `SLEEPING_MAX_AGE`  |
| ENDED      | > 24 h           | —                   |

The THINKING grace window smooths over pauses between tool calls so the badge doesn't flicker.
This is a recency heuristic, **not semantic** — a session that printed "Session saved" still
shows by elapsed time, not by reading the message.

---

## Resuming sessions from the web

The chat box runs `claude --print "<msg>" --resume <id> --output-format stream-json` with the
subprocess `cwd` set to the session's project, streaming events back over SSE. Plain `--resume`
(no `--fork-session`) appends to the **same** transcript, so the conversation stays continuous.

- Resume a session that's **idle** in the CLI; resuming one that's **live in another terminal**
  would fork the conversation — the UI warns you when that's the case.
- The active model comes from your environment (`ANTHROPIC_BASE_URL` / model config), shown via
  the session init event. A session can switch backends across a resume.
- Tool permissions use a **mode dropdown** (acceptEdits / plan / bypassPermissions / default),
  since headless mode can't show the interactive TUI prompt.

---

## Architecture

```
claude.sessions/
├── serve.sh                 # venv launcher → uvicorn on 127.0.0.1:8765
├── requirements.txt         # fastapi, uvicorn, pytest
├── watch_session.py         # standalone CLI transcript viewer
├── watch.sh                 # venv launcher for the CLI viewer
├── server/
│   ├── app.py               # FastAPI: API endpoints + static serving + no-store middleware
│   ├── parser.py            # transcript parsing, status, search, tail (pure, cached by mtime)
│   ├── runner.py            # headless `claude --resume` turns, streamed (web chat)
│   ├── registry.py          # records last web-driven mtime per session (WEB/CLI origin)
│   ├── overrides.py         # persisted custom title overrides
│   ├── summaries.py         # persisted "what's expected" summaries (keyed by mtime)
│   ├── summarizer.py        # generates summaries via `claude --print` (isolated + cleaned up)
│   └── static/              # index.html, session.html, search.html, app.js, style.css
└── tests/                   # pytest suite (55 tests)
```

- **`parser.py`** is pure/file-based and independently testable. Summaries are cached per file
  and only re-parsed when mtime/size changes, so polling stays cheap (~8 ms warm over 250
  sessions).
- **`app.py`** is a thin HTTP layer; the frontend is plain static files (no build step).

### HTTP API
| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/sessions?limit=&offset=&status=` | Session summaries (status=`attention` or comma list filters) |
| GET | `/api/sessions/{id}` | Full detail (summary + activities desc + `file_size`) |
| GET | `/api/sessions/{id}/status` | Cheap cached summary (no activities) — live header refresh |
| GET | `/api/sessions/{id}/tail?offset=` | New events after byte `offset` — live history streaming |
| GET | `/api/sessions/{id}/summary` | One-paragraph "what's expected" summary (waiting sessions) |
| PUT | `/api/sessions/{id}/title` | Set custom title (empty body reverts) |
| DELETE | `/api/sessions/{id}/title` | Clear custom title |
| POST | `/api/sessions/{id}/send` | Resume a turn; **SSE** stream of events |
| GET | `/api/search?q=` | Search by title/id/path (wildcards) |

### Local state files (gitignored, never committed)
- `server/.web_sessions.json` — per-session mtime of the last web-driven turn (WEB/CLI origin).
- `server/.title_overrides.json` — your custom session titles.
- `server/.waiting_summaries.json` — cached "what's expected" summaries.

These hold per-user runtime data. Back them up if you want to preserve renames across machines.

---

## Configuration

| What | Where |
|------|-------|
| Transcripts location | `CLAUDE_PROJECTS_DIR` env var (default `~/.claude/projects`) |
| Port | `PORT` env var (default 8765) |
| Status thresholds | `THINKING/WAITING/SITTING/SLEEPING_MAX_AGE` in `server/parser.py` |
| Dashboard poll rates | `Dashboard.FAST_MS` / `SLOW_MS` in `server/static/app.js` |
| Detail poll rates | `Detail.FAST_MS` / `SLOW_MS` in `server/static/app.js` |
| Summary model | inherited from your environment (`ANTHROPIC_BASE_URL` / Claude config) |
| Summarizer isolation dir | `SUMMARIZER_CWD` in `server/parser.py` |

---

## Development

```bash
.venv/bin/python -m pytest tests/ -q     # run the test suite (55 tests)
```

Static assets are referenced with a `?v=N` query; bump it (and rely on the `no-store` header)
when changing `app.js` / `style.css` so browsers don't serve stale copies.

---

## Caveats

- **Status is heuristic** (idle-time based), not a real session lifecycle.
- **Token totals are cumulative** across all turns (cache-read tokens recur each turn), so a
  headline like "5M tok" reflects total accounting, not a single bill. Local/proxy backends may
  not report usage at all (totals can read 0).
- **Resuming a live CLI session forks it** — only resume idle/ended sessions from the web.
- **Notifications** are page-based (Notification API): keep a browser tab open, use
  `http://127.0.0.1`/`localhost` (a secure context), and allow notifications at the OS level
  (macOS: System Settings → Notifications; disable Focus/Do Not Disturb). Background tabs may be
  throttled to ~once/minute.
- **Summary generation** spends model quota (uses your configured backend); it's lazy
  (only when you open a waiting session's detail page) and cached per waiting episode.

---

## Ideas / backlog (not yet built)

- Full-text search across transcript message content
- Project grouping + model filter on the dashboard
- Changed-files-per-session view (from `file-history-snapshot` events)
- Audible alert + "waiting for Xm" on cards
- "Copy resume command" button; acknowledge/snooze a waiting session
- Per-tool Approve/Deny buttons in web chat (needs an MCP permission server)
- Stats / usage page (token & cost rollups)
- Live history streaming was completed; WebSocket push was evaluated but adaptive polling kept

---

_Repo: `https://github.com/fernandokarnagi/claude.sessions.git`_
