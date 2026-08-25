# Agent OS (`claude.sessions`) — Architecture

> Generated: 2026-08-10
> Scope: repo
> Mode: standard
> Style: narrative
> Evidence quality: strong (graph: 1514 nodes / 7275 edges, rev 4; impact + smell analysis; 2 targeted source reads)
> Coverage: all four planes expanded; `parser.py`, `tmuxio.py`, `app.py`, `autonomy.py` and the frontend controllers covered in depth. Provider parsers, Slack, MCP and the persisted-state modules summarized. Per-method detail deliberately omitted (standard mode).

---

## Part 1 — Narrative

### 1. Overview

Agent OS is a **local control plane for a fleet of coding-agent sessions**. It started as a read-only dashboard over Claude Code's JSONL transcripts and has since grown a write side: it can answer permission gates, type into live REPLs, spawn new sessions, relay messages between them, and auto-approve on policy.

Two facts explain almost every design decision in the repo:

1. **The agents don't have an API.** Claude Code writes append-only JSONL transcripts to `~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl` and runs its REPL in a terminal. There is no status endpoint, no "session ended" marker, no way to answer a permission prompt programmatically. So the app **infers state from file mtime** and **drives the agent by reading and writing its terminal**.
2. **A tmux session is named exactly after the agent session id.** That single invariant is what makes the control plane possible — given a session id from a transcript, the app can find the live pane, screenshot it, and send keys to it. Every spawn path (`dispatch`) preserves the invariant deliberately.

Everything is local. Transcripts are never modified; renames, autonomy levels, task state and summaries live in separate gitignored JSON files beside the server.

The app is multi-provider. Claude Code is the primary target, with parallel parsers for **Antigravity** (`agyparser.py`) and **Grok** (`grokparser.py`), plus Ollama-cloud usage reporting.

### 2. Architecture

The `ix` auto-clustering splits the repo into 14 generic regions all labelled `Server (n)` — low signal, ignore it. The real boundaries are four planes, and they are easiest to hold in your head as a read side, a write side, a policy side, and edges.

```
                         ┌──────────────── EDGES ────────────────┐
   browser ──────────────►  app.py (FastAPI, 50 members)         │
   Slack   ──────────────►  slackbot.py                          │
   MCP client ───────────►  mcp_server.py                        │
   terminal ─────────────►  watch_session.py                     │
                         └───────────┬───────────────────────────┘
                                     │
            ┌────────────────────────┼────────────────────────┐
            ▼                        ▼                        ▼
   READ PLANE                 WRITE PLANE              POLICY / STATE
   parser.py       (high risk) tmuxio.py  (high risk)  autonomy.py
   agyparser.py                runner.py               registry.py
   grokparser.py                                       overrides.py, tasks.py,
   summaries/summarizer                                projects.py, attention.py,
   ollamausage.py                                      descriptions.py, archives.py
            │                        │                        │
            └──────────► ~/.claude/projects/*.jsonl  +  tmux panes
                                                     +  server/.*.json
```

**Read plane — `parser.py`.** Pure, file-based, cached by mtime+size. Turns a transcript into a summary, a status, a searchable record, and an incremental tail. Independently testable; nine modules import it. The provider parsers mirror its shape for non-Claude agents, so the rest of the system stays provider-agnostic.

**Write plane — `tmuxio.py`.** The terminal driver: capture a pane, parse a permission gate off the screen, send keys, spawn, relay, kill. This is the module the whole control plane rests on. `runner.py` is the alternative write path — headless `claude --print --resume`, streamed over SSE — used when there is no live pane.

**Policy / state plane.** Small, single-purpose modules, each owning one gitignored JSON file. `autonomy.py` is the only one with behavior rather than storage: it runs a background watcher that is the *single* authority for auto-answering gates.

**Edges.** `app.py` is the HTTP surface (session CRUD, tmux answer, dispatch, relay, tasks, projects, SSE) plus static serving with a `no-store` middleware. `slackbot.py` mirrors gates into Slack over Socket Mode. `mcp_server.py` (new, uncommitted) exposes fleet state to MCP clients. The frontend is plain static files — no build step.

### 3. How It Works

**The poll loop (the 95% path).** Every dashboard, To-do and detail view is a poll against `app.py`. A request lands on an `api_*` handler, which calls `parser.list_sessions` (13 callers — the busiest read entry point), then passes the result through `_decorate`. `_decorate` is where all the cross-cutting state converges: custom titles from `overrides.py`, WEB-vs-CLI origin from `registry.py`, autonomy level, attention marks, task counts, and the **live-tmux status clamp** — while a pane is alive, status can't decay past WAITING, because the idle-time heuristic would otherwise show a live-but-thinking agent as "Sleeping". Batch endpoints compute the live-tmux set once and pass it down, so N sessions cost one `tmux ls`, not N subprocesses.

**Status is a recency heuristic, not a lifecycle.** THINKING < 30s, WAITING < 30min, SITTING < 2h, SLEEPING < 24h, else ENDED. Thresholds live at the top of `parser.py`. A transcript that says "session saved" still ages by clock, not by content.

**The gate loop (what makes it a control plane).** `capture_pane` screenshots a live pane; a regex over numbered `❯ N.` rows detects a pending permission prompt and rejects idle/running screens. `pending_ids()` caches a full-fleet scan with a 1s TTL. Three consumers race for that result and they are deliberately not symmetric:

- the **autonomy watcher** applies policy first — `yolo` approves every permission gate, `auto-safe` approves read-only gates and escalates writes and shell commands, `manual` never acts. No level answers a multiple-choice question (a numbered menu with no yes/no in it): that is a decision about the work, not about risk, so it always waits for a human and is posted to Slack even on a `yolo` session;
- **Slack** posts a gate only for sessions still on `manual`, so a gate is never double-handled;
- the **browser** shows the gate in the To-do rail and the detail approval panel.

An answer is `tmux send-keys` of a digit plus Enter, with optional follow-up text for the "tell Claude what to do differently" option.

**Two write paths, and they are not interchangeable.** Sending to a session with a live pane types into that pane (continuous conversation). Sending to one without spawns a headless `claude --print --resume` (same transcript, no fork) and streams events back over SSE. The UI probes `/tmux` and routes automatically. Resuming a session that *is* live in another terminal would fork it — the UI warns instead.

**Frontend.** `app.js` is one file of controller objects — `Dashboard`, `Detail`, `AttnRail`, `WatchCols`, `Triage`, `Dispatch`, `Relay`, `World`. Each owns a page or a region, polls on its own timer, and repaints. Two helpers dominate: `esc` (35 callers) and `getJSON` (19 callers). The pattern is uniform enough that reading `Detail` teaches you all the others.

The watch-column view is worth calling out because it is structurally unusual: a column **is** the detail page, embedded via `<iframe src="/session.html?id=…&embed=1">`. That is what keeps every button, modal and provider path identical between a column and a full tab as those evolve. Column pins, count and rail-click target persist to `localStorage`; the iframe `src` is set once per pin and never reassigned on a poll, because reassigning it would wipe a half-typed message.

### 4. Key Components

**`server/tmuxio.py`** — 1142 lines, 44 members, imported by 7 modules, **high risk / shared**. The terminal driver. `capture_pane` alone has 27 direct callers and ~100 downstream dependents at depth 4; `_send_keys` (13), `error_line` (10), `pending` (8) follow. Everything the app *does* rather than *reads* goes through here.

**`server/parser.py`** — 554 lines, 22 members, imported by 9 modules, **high risk / shared**. Transcript truth. `list_sessions` (13 callers), `compute_status` (10), `search_sessions` (10), `session_path` (8), `tail_activities` (8). Pure and cached — the reason polling 250 sessions stays in single-digit milliseconds warm.

**`server/app.py`** — 1318 lines, 50 top-level members, fan-out 34. The HTTP surface and the assembly point: `api_sessions`, `api_triage`, `api_session`, `api_send`, `api_answer_multi`, `api_relay`, `api_tasks`, `api_projects`, `event_stream`, plus a Pydantic body class per POST. Flagged a god module, and it is one — but it is a *thin* god module: routing and orchestration, with the logic in the modules it calls.

**`server/autonomy.py`** — the only policy module with a background thread. Levels persist to `.autonomy.json`; absence means `manual`. Two independent kill switches: `AUTONOMY_DISABLED=1` at launch, `set_paused(True)` at runtime. It never imports `slackbot` — callers register a notify hook via `set_auto_answer_hook` instead, purely to avoid an import cycle.

**`server/registry.py`** — tiny, and a good example of the repo's style. WEB-vs-CLI origin is not a sticky flag: it records the transcript mtime at the end of each web-driven turn, and if the file is written after that, the session flips back to CLI. State inferred from the filesystem rather than tracked.

**`server/static/app.js`** — the entire frontend. Controller objects, `localStorage` for view preferences, no framework and no build step.

### 5. Dependencies & Relationships

The dependency shape is a **wide-but-shallow star**: `app.py` imports 34 things and almost nothing imports `app.py` (only two tests). Depth is low, fan-out is high. Practical consequence — you can change most modules without a ripple, but changing the two shared cores ripples everywhere.

The genuine coupling points:

- **`tmuxio.capture_pane`** — 27 callers spread across `app.py`, `agyparser.py`, `autonomy.py`, `slackbot.py`. Any change to its return shape or its idle/running detection reaches the whole fleet view. Run `ix impact capture_pane` before touching it.
- **`parser.list_sessions` / `compute_status`** — every list, search, triage and status endpoint funnels through these, so status semantics are effectively a public API.
- **`_decorate` in `app.py`** — 14 callers. The single place where transcript facts and app-side state are merged. New per-session state belongs here, not in individual endpoints.
- **`esc` and `getJSON` in `app.js`** — 35 and 19 callers. `esc` is the XSS boundary for the entire UI; every renderer depends on it being escape-first.
- **`autonomy.py` ↔ `slackbot.py`** — deliberately one-directional via a registered hook. Preserve that; a direct import reintroduces the cycle.

Provider parsers (`agyparser.py`, `grokparser.py`) both import `parser.py` and `tmuxio.py` and are imported by `app.py` and `autonomy.py`. They are peers of the Claude path, not wrappers around it.

### 6. Risk & Complexity

**Highest change sensitivity** — `tmuxio.py` and `parser.py`, both flagged high-risk shared dependencies. Between them they carry 173 member-level callers. Nothing else in the repo is close.

**God modules** — `app.py` (fan-out 34), `parser.py` (fan-in 9, fan-out 16), `slackbot.py` (fan-out 19, fan-in 1). `slackbot.py` is the odd one: high fan-out with a single importer means it reaches deep into the system while nothing depends on it — easy to break, cheap to disable.

**Screen-scraping is inherently fragile.** Gate detection is a regex over rendered terminal output. A prompt-format change in any upstream agent breaks approvals silently — the gate simply stops being detected. This is the single most likely source of a future production bug, and it has no compile-time protection. `tests/test_answer_verify.py` and `test_error_line.py` are the guard rails.

**`app.js` is treated as binary by git.** It contains 4 literal NUL bytes, used as sentinels in the markdown converter (`\0B<index>` placeholders around fenced code blocks). Valid UTF-8, but git's binary heuristic trips on the first NUL, so `git diff` reports `Bin 182738 -> 183876 bytes` — no line diffs, no blame, no reviewable history for the entire frontend. Fix is one line either way: add `*.js diff` to `.gitattributes`, or swap the sentinel for a private-use codepoint such as ``. Worth doing before the next frontend review.

**Weak component members** — every HTML page connects to the graph through exactly one neighbour (`app.js`). That is the intended shape for a no-build frontend, not a defect, but it does mean the graph can't tell you which page uses which controller. Read the inline `<script>` block at the bottom of each page; that is where wiring lives.

**Orphan files** — the markdown docs, `style.css` and `.mcp.json` have no graph edges. Expected for non-code assets.

**Cost and blast radius.** Dispatch spawns a real REPL and burns tokens; relay nudges a real session. Both are validated but neither is covered by an end-to-end test for exactly that reason. `yolo` autonomy auto-approves every gate including destructive ones — the two kill switches exist because that is a real operational risk, not a theoretical one.

### 7. How to Work With This Repo

**Run it.**

```bash
export CLAUDE_PROJECTS_DIR="$HOME/.claude/projects"   # default; override if elsewhere
./serve.sh                                            # → http://127.0.0.1:8765
.venv/bin/python -m pytest tests/ -q                  # test suite
```

`serve.sh` creates `.venv`, installs deps, sources a gitignored `.env.slack` if present, and starts uvicorn.

**Three rules that will bite you otherwise:**

1. **Bump `?v=N` on every HTML page when you touch `app.js` or `style.css`.** All pages share one version number. A stale `app.js` has previously made a working feature look broken.
2. **Never delete `server/.*.json`.** `.title_overrides.json`, `.web_sessions.json`, `.autonomy.json`, `.waiting_summaries.json` hold real user state. A test once wiped saved renames.
3. **Preserve `tmux session name == agent session id`.** Every control-plane capability depends on it. If you add a spawn path, it must set the name explicitly.

**Where changes usually go.** New per-session data → produce it in `parser.py` or a state module, merge it in `_decorate`, render it in the matching `app.js` controller. New terminal capability → `tmuxio.py`, then one `api_*` endpoint, then the UI. New agent provider → a parser mirroring `parser.py`'s surface, wired in `app.py` alongside `agyparser`/`grokparser`.

**Keep the graphs current.** `graphify update .` after code changes (AST-only, no API cost); `ix` re-indexes on its own commands. Both are configured in `CLAUDE.md` and worth using before grepping.

### 8. Where to Go Deeper

Read in this order — it follows the data, and each file explains the next:

1. `server/parser.py` — start at the `*_MAX_AGE` constants and `compute_status`. Everything downstream inherits these semantics.
2. `server/tmuxio.py` — `capture_pane`, then `parse_prompt`, then `answer`. That triple is the control plane.
3. `server/app.py` — `_decorate` first, then `api_triage`, then `api_send`. Skip the CRUD endpoints; they're mechanical.
4. `server/autonomy.py` — the module docstring is the spec; read it before changing any approval behavior.
5. `server/static/app.js` — `Detail`, then `AttnRail` and `WatchCols`. The remaining controllers repeat the pattern.
6. `server/agyparser.py` — read only when adding a provider; it shows what the parser contract actually requires.

Useful queries:

```bash
ix impact server/tmuxio.py        # before any terminal-driver change
ix callers _decorate              # who depends on decorated session shape
graphify query "how does a permission gate get answered"
graphify path "AttnRail" "WatchCols"
```

---

## Part 2 — Selective Reference

### Module Summary

**`server/app.py`** — HTTP surface. FastAPI app, static mounting, `no-store` middleware, background-thread startup, SSE streaming. Contains the Pydantic request bodies (`SendBody`, `ProjectBody`, `TaskBody`, `RelayBody`, `TitleBody`, …) and ~35 `api_*` handlers. Depends on nearly every other server module; almost nothing depends on it. *Change carefully:* `_decorate`, `api_triage`, `event_stream`.

**`server/parser.py`** — transcript reading. Session discovery, summary extraction, status computation, search with glob wildcards, byte-offset tail. Pure functions over files, cached by mtime+size. No dependencies on the rest of the server. *Change carefully:* status thresholds, `list_sessions` return shape.

**`server/tmuxio.py`** — terminal driver. Pane capture, gate parsing, key sending, error-line extraction, session listing, spawn (`dispatch`), inter-session `relay`, `kill`. Depends only on the tmux binary. *Change carefully:* `capture_pane`, `parse_prompt`, `_send_keys`.

**`server/runner.py`** — headless turns. Runs `claude --print --resume … --output-format stream-json` with cwd set to the session's project and streams events. Three members; the alternative to the live-pane write path.

**`server/autonomy.py`** — per-session approval policy plus the watcher thread that enforces it. Sole auto-answer authority. Notifies Slack through a registered hook rather than an import.

**`server/registry.py`** — WEB/CLI origin, derived from the last web-driven transcript mtime. Lock-guarded, atomic-replace JSON persistence — the template the other state modules follow.

**State modules — `overrides.py`, `attention.py`, `descriptions.py`, `tasks.py`, `projects.py`, `archives.py`, `summaries.py`.** One concern each, one gitignored JSON file each, same load/lock/atomic-save shape. Read one and you have read them all. `tasks.py` and `projects.py` are the largest because they carry real domain structure.

**`server/summarizer.py`** — generates the "what's expected from you" paragraph via `claude --print` in an isolated cwd, deleting its throwaway transcript so it never shows up in listings. Lazy and cached per waiting episode; spends model quota.

**`server/agyparser.py` / `server/grokparser.py`** — provider parsers for Antigravity and Grok. Mirror `parser.py`'s surface (`list_conversations`, `get_summary`, `get_conversation`, `parse_gate`) while reading each provider's own console format. `agyparser` additionally reconciles tmux names and extracts tokens from screen text.

**`server/ollamausage.py`** — usage reporting for Ollama-cloud models, signed against `ollama.com` with the local ed25519 key. Self-contained; no live REPL needed.

**`server/slackbot.py`** — Socket Mode integration. Watcher thread posts gates as button messages for `manual` sessions, updates them on resolution, handles replies and a `/pending` command. No-ops entirely when its env vars are absent.

**`server/mcp_server.py`** — MCP surface over fleet state (`fleet_status`, `session_info`, `who_owns`). New and uncommitted; not yet part of the graph's dependency structure.

**`server/static/app.js`** — the frontend, in full. Controller objects per view, shared helpers (`esc`, `getJSON`, `mdToHtml`, `relTime`), `localStorage` for view preferences.

**`watch_session.py` + `watch.sh`** — standalone terminal transcript viewer. Shares no code with the server; useful when the web app is down.

### Component Summary

| Component | Role | Manages | Used from |
|---|---|---|---|
| `parser.list_sessions` | boundary / read entry | transcript discovery + summaries | every list, search, triage endpoint |
| `parser.compute_status` | pure policy | the 5-tier idle heuristic | `_decorate`, all status paths |
| `tmuxio.capture_pane` | adapter | live pane text | 27 callers across app, providers, autonomy, Slack |
| `tmuxio.answer` | actuator | gate response via send-keys | approval panel, Slack buttons, autonomy watcher |
| `app._decorate` | orchestrator | merge of transcript facts + app state + live clamp | 14 callers |
| `autonomy.start_watcher` | background service | policy enforcement over live gates | app startup |
| `Detail` (`app.js`) | view controller | one session: header, chat, approval, history | detail page and every watch column |
| `AttnRail` (`app.js`) | view controller | the To-do queue; feeds column pickers from one poll | To-do view |
| `WatchCols` (`app.js`) | view controller | up to 3 embedded detail pages, pins persisted | To-do view |
| `Triage` (`app.js`) | view controller | inbox with inline answers, autonomy dial, fleet pause | triage view |

*Method-level summaries are omitted in standard mode. For those, rerun with `--full --style hybrid` against a specific module.*
