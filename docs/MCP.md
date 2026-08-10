# Agent OS as an MCP server

The dashboard knows what all your sessions are doing. The sessions themselves
know nothing about each other — each one behaves as if it is the only process
on the machine, which is how two agents end up editing the same file.

This closes that gap. `server/mcp_server.py` exposes the fleet to the agents
inside it, over MCP.

## What's here

Read-only. Three query tools, no writes, no session identity.

| Tool | Answers |
|---|---|
| `fleet_status(live_only=true, limit=40)` | What else is running: title, status, model, cwd, whether it's blocked on a permission prompt |
| `who_owns(path, live_only=true)` | Which live sessions are working in or under a path |
| `session_info(session_id)` | One session in detail, **including the task list you maintain for it in the UI** |

`session_info` is the one with the surprising payoff: you curate a task board
per session in the dashboard that, until now, the agent could never read.

## Why this tier first

None of these three tools care *who* is asking. That matters, because
establishing caller identity is the hard part of this feature — session ids
change when you `/clear` (which is why every store in `server/` carries a
`rekey()`), and `$TMUX_SESSIONID` has already been observed going stale in a
shell and silently misattributing relay messages.

So this tier ships with none of that plumbing, and no write can go wrong
because there are no writes.

## Architecture

```
Claude Code session ──stdio/JSON-RPC──> server/mcp_server.py ──HTTP──> 127.0.0.1:8765
```

A thin client, not a second backend. Every answer comes from the FastAPI app
you already run, so no store is opened twice and no logic is duplicated.

Stdio means nothing binds a port, nothing is reachable off this machine, and
the server process dies with the session that launched it.

## Install

**Per repo** (already committed as `.mcp.json` here) — every session started in
this directory picks it up. Claude Code will ask you to approve the server the
first time.

**Everywhere** — this is the more useful install, because a session in
*another* repo is exactly the one that benefits from `who_owns`:

```
claude mcp add --scope user agent-os -- \
  python3 -m server.mcp_server
```

Run that with `PYTHONPATH` and `cwd` pointed at this repo, or wrap it in a
small launcher script with absolute paths.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `AGENTOS_URL` | `http://127.0.0.1:8765` | Where the dashboard is |
| `AGENTOS_MCP_TIMEOUT` | `4` (seconds) | Per-call HTTP timeout |

## Design constraints these tools hold to

**Payload caps are enforced in the tool, not left to the caller.** A
fleet-awareness tool that floods the caller's context window is spending the
exact resource it exists to protect. Max 40 sessions, 20 tasks, 400 chars of
description.

**No transcript content ever crosses the boundary.** `session_info` reads
`/api/sessions/{id}/status` (the cached summary) and never
`/api/sessions/{id}`, which ships activities. Transcripts are the largest
payload in the app and the most direct route for one agent's output to become
another agent's instructions.

**Cross-session text is data, not instruction.** Titles and descriptions are
written by other agents. The tool descriptions say so explicitly, because the
relay bus already carries this exposure and MCP widens it.

**Loopback bypasses the proxy.** This machine has a corporate proxy in the
environment and urllib honours `http_proxy` by default — which would route
`127.0.0.1` out to it and hang. `_opener()` passes an empty `ProxyHandler`,
which makes `build_opener` drop proxying entirely.

**Failure is a sentence, not a traceback.** If the dashboard is down, every
tool returns `{"error": "fleet unavailable", "hint": "..."}` telling the agent
to carry on without fleet information — rather than a stack trace it will try
to debug for ten minutes.

**Snapshots are stamped.** Every response carries `as_of`. The fleet moves; a
session can be dead by the time the agent acts on the answer.

## Not here, deliberately

Writes — `claim`/`release`, `send_to_session`, `add_task`,
`request_attention`, `dispatch` — are a later tier and need caller identity
first.

`answer` / `answer-multi` will **never** be exposed. A `manual` session could
approve its own permission gate through the MCP door, bypassing every autonomy
control in the app. Same for `kill`, `kill-all`, `autonomy` set, `pause`,
`reset` and `compact`.

## Tests

`tests/test_mcp_server.py` — 23 tests, no live server needed. Everything below
the HTTP call is pure dict-shaping over an injected fake opener.

For a real end-to-end check, start the dashboard and drive the server over
actual stdio with an MCP client — that path is verified working against a live
fleet.
