# Changelog

All notable changes to Agent OS are recorded here. Newest first.

## Unreleased

### Added
- **Agent workflows** (`server/workflows.py`, `/workflows.html`) — a workflow is
  a reusable blueprint: an agent roster (name, model, role, system prompt) and
  ordered stages, each with a goal, exit criteria, and a coordination mode
  (`coordinator`, `handoff`, `parallel`, `solo`). Assigning one to a session
  stores a binding; it does not spawn agents or route hand-offs. The operator
  drives it by hand from a `#workflow` panel on the detail page — **▶ Send
  stage** composes that stage into one prompt and types it into the live REPL,
  **✓ Advance** moves on without sending. CRUD plus YAML import/export at
  `/api/workflows`; bind, send and advance at
  `/api/sessions/{id}/workflow[/send|/advance]`. State lives in
  `server/.workflows.json` and survives a `/clear` via `rekey`.
- **"＋ Task" button on every assistant message** in the session history: one
  click summarizes that reply into a queued task — the follow-up message to send
  back, ready to Ask or edit. `POST /api/sessions/{id}/tasks/summarize` (`app.py`)
  takes the message text, or falls back to the session's latest assistant message
  when none is posted; `summarizer.as_task()` reuses the throwaway
  `claude --print` run behind the waiting summary with a different prompt. Works
  for claude, grok and agy sessions; not cached, since it is an explicit button
  press.
- **MCP server** (`server/mcp_server.py`) exposing Agent OS over the Model
  Context Protocol, so another Claude session can query the fleet as a tool.
  Tools: `fleet_status`, `session_info`, `who_owns`. Registered for this repo
  in `.mcp.json`, pointed at the local dashboard (`http://127.0.0.1:8765`).
- `docs/MCP.md` — how to run and connect the MCP server.
- `docs/ARCHITECTURE.md` — module map and request-path walkthrough.
- `tests/test_mcp_server.py` — 23 tests covering the three tools.
- Dependencies: `mcp>=1.26`, `pytest-asyncio>=0.23`.

### Changed
- To-do rail status pills (THINKING / WAITING / SITTING / SLEEPING / ENDED) now
  render at 9.5px instead of 11px, matching the rail's other tags. The rail is
  174px wide, so the smaller pill leaves more room for the session title. Board,
  detail-header and watch-column pills are unaffected.
- Static asset cache-buster bumped to `v=165`.
