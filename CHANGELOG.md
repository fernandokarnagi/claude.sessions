# Changelog

All notable changes to Agent OS are recorded here. Newest first.

## Unreleased

### Added
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
- Static asset cache-buster bumped to `v=160`.
