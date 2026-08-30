# Agent Gateway — Sessions as A2A Services Behind Kong

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish every live session as its own addressable agent. Each session gets a stable handle derived from its title, a local HTTP listener on a managed port, and its own Kong Service + Upstream + Route. The listener speaks A2A (agent card, JSON-RPC, SSE) so any external system — CI, Slack, another agent — can call a tmux pane without knowing what it is.

**Architecture:** Four new modules, each following the existing store pattern (`server/projects.py`, `server/workflows.py`): one gitignored JSON file, a module-level `RLock`, atomic tmp+replace writes.

- `server/handles.py` — title → slug, collision rules, retirement. The identity layer.
- `server/ports.py` — port ledger and the per-session `SessionServer` listeners. The bind is the allocation.
- `server/agentapi.py` — the per-session ASGI app: agent card, `/health`, JSON-RPC.
- `server/kongreg.py` — Kong Admin API sync: publish, rename, teardown, reconcile.
- `server/extasks.py` — external task store, same shape as `server/tasks.py`.

Nothing in the dashboard changes shape. `app.py` gains a small publish/gateway surface; `tmuxio.spawn` and `archives.py` gain lifecycle hooks.

**Tech Stack:** Python 3.14, FastAPI + Starlette, uvicorn (already a dependency, now also used programmatically), Pydantic v2, pytest + `fastapi.testclient`, vanilla ES6, plain CSS.

**Scope of this plan:** Build-order items 1–4. Catalog publication (Konnect API products) and the AgentPortal are a separate plan.

---

## Global Constraints

- Python runs from the repo venv. The documented test command is `.venv/bin/python -m pytest` (README.md, docs/ARCHITECTURE.md) — `-m` is what puts the repo root on `sys.path` so `from server import ...` resolves. Never invoke the bare `.venv/bin/pytest` shim.
- New store files, all gitignored, all under `server/`: `.handles.json`, `.ports.json`, `.kong.json`, `.extasks.json`. Never commit one, never `rm` a real one during testing. Tests always `monkeypatch.setattr(mod, "_PATH", str(tmp_path / "x.json"))`.
- Every store module writes atomically: `json.dump` to `_PATH + ".tmp"`, then `os.replace`. Guard every read/write with a module-level `threading.RLock()`.
- Every timestamp is `datetime.now(timezone.utc).isoformat()`.
- **No test touches the network.** The Kong client is a single injectable callable; tests pass a recording fake. No `responses`, no `httpx` mock transport, no live Kong.
- **Never look up a Kong entity by name.** Names carry the handle and therefore change on rename. Store Kong's uuids in `.kong.json` and address entities by id. Names exist for humans and for the portal.
- Every Kong write is idempotent and tagged `agentos` plus `session:<session_id>`. The tag is the backstop when `.kong.json` is lost.
- Environment:

  | var | default | meaning |
  |---|---|---|
  | `KONG_ADMIN_URL` | `http://127.0.0.1:8001` | Admin API |
  | `AGENTOS_PUBLIC_BASE` | `http://127.0.0.1:8000` | what a caller dials (the DP) |
  | `AGENTOS_PORT_RANGE` | `8800-8899` | listener range |
  | `AGENTOS_NODE_HOST` | `127.0.0.1` | target host Kong dials |
  | `AGENTOS_NODE` | hostname | tag value, multi-host later |
  | `A2A_VERSION` | `0.3.0` | advertised in the card; verify before shipping |

- A session is published only when **both** are true: an operator set `publish` on it, and it has been alive past `PUBLISH_MIN_AGE` (300s). Scratch sessions never reach Kong.
- Published sessions must not run `bypassPermissions`. Enforce at publish time, refuse with 409.
- API errors: 400 validation, 404 unknown session, 409 conflict (ports exhausted, unsafe permission mode, no live pane), 503 Kong unreachable.
- Handle slug charset is `[a-z0-9-]`, max 48 chars — a subset of what Kong allows for entity names, so no Kong name can ever be invalid by construction.

---

### Task 1: Identity and ports

**Files:**
- Create: `server/handles.py`, `server/ports.py`
- Create: `tests/test_handles.py`, `tests/test_ports.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: nothing. Both modules are pure stores plus one asyncio listener manager.
- Produces:
  - `handles.slugify(title: str) -> str`
  - `handles.mint(session_id: str, title: str) -> str`
  - `handles.current(session_id: str) -> str | None`
  - `handles.owner(handle: str) -> str | None`
  - `handles.retire(handle: str) -> None`
  - `handles.aliases(session_id: str) -> list[dict]`
  - `handles.expire(grace_hours: int = 24) -> list[str]`
  - `ports.bind(session_id: str) -> tuple[int, SessionServer]` (async)
  - `ports.release(session_id: str) -> None` (async)
  - `ports.current(session_id: str) -> int | None`
  - `ports.reap() -> list[int]` (async)
  - `ports.PortsExhausted`

#### 1.1 `server/handles.py`

Store shape:

```json
{
  "claude-session-watcher-dvl": {"session_id": "5eafb603", "since": "2026-08-28T09:12:04Z", "retired_at": null},
  "old-title-slug":             {"session_id": "5eafb603", "since": "2026-08-27T18:02:11Z", "retired_at": "2026-08-28T09:12:04Z"}
}
```

Keyed by handle, not by session. A session owns one live handle and any number of retired aliases; the reverse lookup is a scan, and the fleet is small enough that this never matters.

Rules:

- `slugify` lowercases, replaces every run of non-`[a-z0-9]` with `-`, strips leading and trailing `-`, truncates to 48 then strips `-` again. Empty result becomes `"session"`.
- `"Claude SEssion Watcher Dvl"` → `"claude-session-watcher-dvl"`.
- Reserved prefixes — `api`, `static`, `agents`, `sessions`, `health`, `admin`, `well-known` — get `agent-` prepended. These are dashboard paths; a handle must never shadow one.
- Collision: if the slug is live and owned by another session, append `-` plus `session_id[:6]`. **Never a counter.** A counter reorders after a restart; an id prefix is stable forever.
- `mint` is idempotent. Called twice with the same title it returns the same handle and writes nothing.
- Rename: `mint` with a new title marks the old handle `retired_at` and inserts the new one. Retired handles keep resolving until `expire()` drops them past the grace window.
- A retired handle blocks a new session from taking that slug while the grace holds — otherwise a caller's in-flight job would silently land on a different agent.

#### 1.2 `server/ports.py`

Store shape:

```json
{
  "active": {"5eafb603": {"port": 8801, "bound_at": "..."}},
  "hints":  {"91c2d0f4": {"port": 8802, "freed_at": "..."}},
  "poison": {"8807": {"until": "..."}},
  "recycle": [8802, 8804]
}
```

- **active** — really bound right now. Blocks allocation.
- **hints** — advisory. A hint never reserves a port. If the session returns and its old port is free it gets it back; if not, it moves on without error. Reserving for dead sessions slowly strangles the range.
- **poison** — a bind here failed within the last 60s. Skipped.
- **recycle** — FIFO of freed ports, oldest first. Allocating lowest-free instead would hand a just-released port to the next spawn within seconds and destroy sticky reuse.

Allocation order: sticky hint → recycle queue (oldest first) → unused ports ascending → `0` (OS ephemeral).

**The bind is the allocation.** Checking "is this port free" and then binding is a race that cannot be won. Try the bind, catch `EADDRINUSE`/`EACCES`, poison, move on. `srv.port` read back from the socket is the only authoritative number — it is what matters when the candidate was `0`.

```python
async def bind(session_id: str) -> tuple[int, "SessionServer"]:
    for cand in _candidates(session_id):
        try:
            srv = await SessionServer.start(session_id, cand)
        except OSError as e:
            if e.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
            _poison(cand)
            continue
        _record(session_id, srv.port)
        return srv.port, srv
    raise PortsExhausted(_RANGE)
```

`SessionServer` runs a uvicorn `Server` as an asyncio task inside the existing process:

```python
class SessionServer:
    @classmethod
    async def start(cls, session_id: str, port: int) -> "SessionServer":
        self = cls()
        self.session_id = session_id
        cfg = uvicorn.Config(agentapi.build(session_id), host="127.0.0.1",
                             port=port, log_level="warning", lifespan="off")
        self.server = uvicorn.Server(cfg)
        self._task = asyncio.create_task(self.server.serve())
        await _await_started(self.server)          # raises the bind OSError
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        self.server.should_exit = True
        await self._task
```

`_await_started` polls `server.started` and re-raises whatever `serve()` died of, so a failed bind surfaces as `OSError` at the `bind()` call site instead of a silently dead task.

Release order is **socket → Kong → ledger**:

```python
async def release(session_id: str) -> None:
    srv = _servers.pop(session_id, None)
    if srv:
        await srv.stop()
    kongreg.teardown(session_id)        # no-op in Task 1, wired in Task 3
    _forget(session_id)                 # active -> hints, port -> recycle tail
```

Reverse that order and Kong keeps routing at a dead port.

`reap()` runs on startup and every 60s. Three independent checks, because a ledger entry can go wrong three ways:

1. session no longer exists → free
2. nothing is listening on the port → free
3. something answers, but `/health` reports a different `session_id` → free, and never register that target

Check 3 is what stops Kong routing a task at a stranger's socket. Without it the failure looks like a possessed agent.

`reap()` also expires hints older than 7 days and clears elapsed poison entries.

- [ ] **Step 1: Write the failing tests**

`tests/test_handles.py`:

```python
def test_slug_of_a_real_title():
    assert handles.slugify("Claude SEssion Watcher Dvl") == "claude-session-watcher-dvl"

def test_reserved_word_is_prefixed():
    assert handles.slugify("admin") == "admin"
    assert handles.mint("aaa", "admin") == "agent-admin"

def test_collision_uses_a_stable_id_suffix():
    assert handles.mint("aaaaaaaa", "Reviewer") == "reviewer"
    assert handles.mint("bbbbbbbb", "Reviewer") == "reviewer-bbbbbb"

def test_mint_is_idempotent():
    h1 = handles.mint("aaa", "Reviewer")
    h2 = handles.mint("aaa", "Reviewer")
    assert h1 == h2 and len(handles.aliases("aaa")) == 1

def test_rename_retires_the_old_handle_and_keeps_it_resolving():
    handles.mint("aaa", "Old Title")
    handles.mint("aaa", "New Title")
    assert handles.owner("old-title") == "aaa"
    assert handles.current("aaa") == "new-title"

def test_retired_handle_expires_after_grace():
    handles.mint("aaa", "Old Title"); handles.mint("aaa", "New Title")
    handles.expire(grace_hours=0)
    assert handles.owner("old-title") is None

def test_retired_handle_blocks_a_new_owner_during_grace():
    handles.mint("aaa", "Shared"); handles.mint("aaa", "Renamed")
    assert handles.mint("bbb", "Shared") == "shared-bbb"
```

`tests/test_ports.py`:

```python
async def test_port_returns_to_pool_and_is_reused()      # release then bind -> same number
async def test_sticky_port_wins_when_still_free()        # same session reclaims
async def test_hint_does_not_reserve()                   # another session may take it; owner falls through
async def test_bind_survives_a_stolen_port()             # pre-bind a decoy socket, land on the next
async def test_port_zero_fallback_when_range_full()      # range of size 1, two sessions
async def test_reap_frees_a_dead_listener()
async def test_reap_frees_on_identity_mismatch()         # /health returns another session_id
async def test_release_stops_the_socket_before_forgetting()
```

Run: `.venv/bin/python -m pytest tests/test_handles.py tests/test_ports.py` — all fail.

- [ ] **Step 2: Implement `handles.py`** until `tests/test_handles.py` passes. Pure functions plus the store; no asyncio.

- [ ] **Step 3: Implement `ports.py`** until `tests/test_ports.py` passes. `SessionServer` may serve a stub app at this stage — Task 2 replaces it with the real one.

- [ ] **Step 4: `.gitignore`** gains `server/.handles.json` and `server/.ports.json`.

**Done when:** both test files pass, and `AGENTOS_PORT_RANGE=8800-8801 .venv/bin/python -m pytest tests/test_ports.py` still passes — the range is honoured, exhaustion falls through to ephemeral.

---

### Task 2: The per-session listener — card, health, `tasks/get`

**Files:**
- Create: `server/agentapi.py`, `server/cards.py`, `server/extasks.py`
- Create: `tests/test_agentapi.py`, `tests/test_cards.py`
- Modify: `server/ports.py` (swap the stub app for `agentapi.build`)

**Interfaces:**
- Consumes: `handles.current`, `parser` status, `tmuxio.has_session/working_ids/pending_ids`, `agents.by_id`.
- Produces:
  - `agentapi.build(session_id: str) -> Starlette`
  - `cards.card(session_id: str) -> dict`
  - `extasks.create/get/update/list_for`

#### 2.1 Routes on the per-session app

Kong routes are declared with `strip_path: true`, so the listener sees clean A2A paths:

```
GET  /.well-known/agent-card.json    → the card
GET  /health                         → liveness + identity
POST /                               → JSON-RPC 2.0, all methods
```

#### 2.2 `/health`

```json
{"session_id": "5eafb603", "handle": "claude-session-watcher-dvl", "state": "ready"}
```

| condition | code | `state` |
|---|---|---|
| `tmuxio.has_session()` false | 503 | `dead` |
| session id in `tmuxio.working_ids()` | 200 | `working` |
| session id in `tmuxio.pending_ids()` | 200 | `input-required` |
| otherwise | 200 | `ready` |

**Busy returns 200.** An agent mid-task is not unhealthy, it is working. Only a dead pane is unhealthy. Inverting this makes Kong yank a perfectly good agent every time it starts thinking — write a test that pins it.

`session_id` in the body is what `ports.reap()` and `kongreg.publish()` use to prove the socket is ours.

#### 2.3 The agent card

```python
def card(session_id: str) -> dict:
    h = handles.current(session_id)
    s = _session(session_id)
    return {
        "protocolVersion": A2A_VERSION,
        "name": h,
        "description": _persona_description(s),
        "url": f"{PUBLIC_BASE}/agents/{h}",
        "preferredTransport": "JSONRPC",
        "version": "1.0.0",
        "capabilities": {"streaming": True, "pushNotifications": False,
                         "stateTransitionHistory": True},
        "securitySchemes": {"apiKey": {"type": "apiKey", "in": "header", "name": "apikey"}},
        "security": [{"apiKey": []}],
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": _skills(s),
        "x-agent": {"handle": h, "persona": s.persona, "model": s.model,
                    "repo": s.repo, "cli": s.cli, "node": NODE},
    }
```

Two rules that are easy to get wrong and expensive to debug:

- **`url` must be the Kong-facing URL**, never `127.0.0.1:<port>`. The card is what a remote client dials. A card advertising the listener leaks the topology and is unreachable off-box.
- **`securitySchemes` must match the Kong plugin exactly.** The listener itself has no auth; Kong does. A card claiming `apiKey` in front of an `oauth2` route fails every conformant client at handshake. Generate both from one config value.

`description` is the body of the persona's markdown file, read through `agents.py` — which already parses `~/.claude/agents/*.md`. Sessions with no persona fall back to the session description (`descriptions.py`), then to a one-liner naming the repo. **No hand-written API docs anywhere in this system.**

#### 2.4 `extasks.py` and the state machine

Store shape mirrors `server/tasks.py`:

```json
{"tasks": {"<session_id>": [
  {"id": "<tid>", "context_id": "<cid>", "state": "working",
   "text": "...", "history": [{"state": "submitted", "at": "..."}],
   "artifacts": [], "created_at": "...", "updated_at": "..."}
]}}
```

State is **derived**, never stored as truth — the pane is the truth:

| AgentOS status | A2A task state |
|---|---|
| THINKING | `working` |
| DELEGATING | `working` |
| WAITING + `tmuxio.pending()` non-null | `input-required` |
| WAITING, nothing pending | `completed` |
| SITTING / SLEEPING | `completed` (stale, flagged in history) |
| pane gone / ENDED | `failed` |
| cancelled by caller | `canceled` |

`history` is appended on every observed transition so `stateTransitionHistory: true` is honest.

#### 2.5 JSON-RPC skeleton

Task 2 implements the envelope plus `tasks/get`. `message/send` and `message/stream` land in Task 4.

```python
async def rpc(session_id: str, body: dict) -> dict:
    rid, method, params = body.get("id"), body.get("method"), body.get("params", {})
    if body.get("jsonrpc") != "2.0" or not method:
        return _err(rid, -32600, "Invalid Request")
    fn = _METHODS.get(method)
    if fn is None:
        return _err(rid, -32601, "Method not found")
    try:
        return _ok(rid, await fn(session_id, params))
    except _BadParams as e:
        return _err(rid, -32602, str(e))
    except Exception as e:                       # noqa: BLE001 - protocol boundary
        return _err(rid, -32603, "Internal error", data=str(e))
```

Error codes are part of the contract, not decoration. Conformance suites check them.

- [ ] **Step 1: Write the failing tests**

```python
def test_health_reports_our_own_session_id()
def test_health_is_200_while_working()          # busy != unhealthy
def test_health_is_503_when_the_pane_is_gone()
def test_card_url_points_at_the_gateway_not_the_listener()
def test_card_security_scheme_matches_the_configured_plugin()
def test_card_description_comes_from_the_persona_file()
def test_rpc_rejects_a_missing_jsonrpc_version()      # -32600
def test_rpc_unknown_method()                          # -32601
def test_tasks_get_maps_every_status_to_a_state()      # table-driven
def test_tasks_get_unknown_id_is_32602()
```

- [ ] **Step 2: Implement `extasks.py`** — store only, no protocol.
- [ ] **Step 3: Implement `cards.py`.**
- [ ] **Step 4: Implement `agentapi.build`** with `/health`, the card route, and the JSON-RPC envelope carrying `tasks/get`.
- [ ] **Step 5: Point `ports.SessionServer` at `agentapi.build`** and re-run `tests/test_ports.py`.

**Done when:**

```bash
.venv/bin/python -m pytest tests/test_agentapi.py tests/test_cards.py tests/test_ports.py
curl -s 127.0.0.1:8801/.well-known/agent-card.json | jq .name
curl -s 127.0.0.1:8801/health | jq .session_id
```

The card is reachable on the raw listener. Kong is still not involved.

---

### Task 3: Kong registration

**Files:**
- Create: `server/kongreg.py`
- Create: `tests/test_kongreg.py`
- Modify: `server/app.py` (publish endpoints, gateway status), `server/tmuxio.py` (spawn hook), `server/archives.py` (teardown hook), `server/overrides.py` (title-change hook)

**Interfaces:**
- Consumes: `handles`, `ports`, an injectable `_admin` callable.
- Produces:
  - `kongreg.set_published(session_id: str, on: bool) -> dict`
  - `kongreg.publish(session_id: str) -> dict`
  - `kongreg.rename(session_id: str, new_handle: str) -> None`
  - `kongreg.teardown(session_id: str) -> None`
  - `kongreg.reconcile() -> dict`
  - `kongreg.state(session_id: str) -> dict | None`

#### 3.1 Entity names and stored ids

```
Service   agent-claude-session-watcher-dvl
Upstream  up-agent-claude-session-watcher-dvl
Target    127.0.0.1:8801
Route     rt-claude-session-watcher-dvl      → /agents/claude-session-watcher-dvl
Route     rt-old-title-slug                  → retired alias, kept until grace expires
```

`.kong.json`:

```json
{"5eafb603": {
  "desired": true,
  "service_id": "9c1f…", "upstream_id": "44ab…", "target_id": "7d02…",
  "routes": {"claude-session-watcher-dvl": {"id": "b81e…", "retired_at": null},
             "old-title-slug":             {"id": "1f77…", "retired_at": "…"}},
  "port": 8801, "published_at": "…"
}}
```

The Service points at the Upstream by name (`host: up-agent-<handle>`), not at a bare URL. The one-target Upstream costs nothing and is the only way to get **active health checks** — a plain `url:` Service has no liveness at all, so a dead pane would keep receiving traffic.

Upstream health check config:

```json
{"active": {"http_path": "/health", "healthy": {"interval": 2, "successes": 1},
            "unhealthy": {"interval": 2, "http_failures": 1}}}
```

Service timeouts — agent turns run for minutes, and Kong's 60s default kills them mid-thought:

```json
{"connect_timeout": 5000, "read_timeout": 3600000, "write_timeout": 3600000}
```

Route flags — SSE must not be buffered into one blob at the end:

```json
{"strip_path": true, "response_buffering": false, "request_buffering": false}
```

#### 3.2 Publish

```python
def publish(session_id: str) -> dict:
    st = _state(session_id)
    if not st.get("desired"):                       return _skip("not published")
    if _age(session_id) < PUBLISH_MIN_AGE:          return _skip("too young")
    if _permission_mode(session_id) == "bypassPermissions":
        raise Unsafe("published sessions may not run bypassPermissions")
    port = ports.current(session_id)
    if port is None or not _verify(port, session_id):
        return _skip("listener not verified")
    h  = handles.current(session_id)
    up = _upsert_upstream(f"up-agent-{h}", session_id)
    tg = _upsert_target(up["id"], f"{NODE_HOST}:{port}")
    sv = _upsert_service(f"agent-{h}", host=up["name"], session_id=session_id)
    rt = _upsert_route(sv["id"], f"rt-{h}", [f"/agents/{h}"])
    return _record(session_id, up, tg, sv, rt)
```

Order is fixed: **upstream → target → service → route.** Any other order points a live route at something that does not exist yet.

`_verify(port, session_id)` hits `/health` and compares `session_id`. A target is never registered for a socket that has not identified itself.

Every `_upsert_*` is idempotent: look up by stored id, else by tag, else create.

#### 3.3 Rename, zero gap

`service.host` must match the upstream name, so renaming the upstream in place breaks routing until the service catches up. Build the new one first:

1. `POST /upstreams` — new upstream, tagged
2. `POST /upstreams/{new}/targets` — same `host:port`
3. `PATCH /services/{id}` — `name` **and** `host` in one call
4. `POST /services/{id}/routes` — new route for the new handle
5. `DELETE /upstreams/{old}` — now unreferenced

Service id survives, so plugins, ACLs and consumer bindings survive with it. Route ids survive, so retired aliases keep answering. No caller sees a gap.

Debounce: a title edited three times in ten seconds must produce one rename, not three. 5-second debounce on the handle-change event.

#### 3.4 Teardown

Delete in reverse: routes → service → target → upstream. Then drop the `.kong.json` entry. Retired routes go with the service.

#### 3.5 Reconcile

Kong config is derived state. Never assume it is right; recompute it. Runs on startup and every 60s.

```python
def reconcile() -> dict:
    live = {sid for sid in published_sessions()}
    seen = {}
    for svc in _admin("GET", "/services?tags=agentos")["data"]:
        sid = _tag_value(svc, "session")
        seen[sid] = svc
        if sid not in live:
            teardown(sid); continue
        want = f"agent-{handles.current(sid)}"
        if svc["name"] != want:
            rename(sid, handles.current(sid))       # same path as a live rename
        _verify_target(sid)                          # port moved? identity mismatch?
    for sid in live - set(seen):
        publish(sid)
    return _summary()
```

One code path for rename whether a human triggered it or reconcile discovered it. This single loop removes the entire "Kong was down when I spawned" bug class.

#### 3.6 Degradation

Kong unreachable is **not** a spawn failure. The session spawns, the listener binds, the work happens; only publication is deferred. `_admin` raises `KongUnavailable`, callers log and continue, reconcile retries. The dashboard shows an `unpublished` badge with the reason. The same holds for `PortsExhausted`.

#### 3.7 New dashboard surface

```
POST   /api/sessions/{id}/publish     → set_published(True) + publish()
DELETE /api/sessions/{id}/publish     → set_published(False) + teardown()
GET    /api/sessions/{id}/gateway     → {handle, port, published, service, routes[], health}
GET    /api/gateway/reconcile         → last run summary (read-only)
```

- [ ] **Step 1: Write the failing tests** with a recording fake for `_admin`:

```python
def test_publish_creates_entities_in_order()          # upstream, target, service, route
def test_publish_is_idempotent()                      # second call writes nothing
def test_publish_refuses_bypass_permissions()         # 409
def test_publish_skips_a_session_younger_than_the_gate()
def test_target_not_registered_when_identity_mismatches()
def test_rename_keeps_the_service_id()                # plugins survive
def test_rename_patches_the_service_after_the_new_upstream_exists()
def test_retired_route_still_resolves_after_rename()
def test_teardown_deletes_in_reverse_order()
def test_reconcile_deletes_orphans_and_restores_missing()
def test_reconcile_repairs_a_drifted_name()
def test_kong_unavailable_does_not_raise_into_spawn()
```

- [ ] **Step 2: Implement `kongreg.py`.**
- [ ] **Step 3: Wire the hooks** — `tmuxio.spawn` and the `/api/sessions/{id}/spawn` route bind a port and attempt publish; `archives.py` tears down; `overrides.py` title change fires the debounced rename.
- [ ] **Step 4: Add the four endpoints** to `app.py`.
- [ ] **Step 5: Start the reconcile loop** on FastAPI startup, 60s interval, cancelled on shutdown.

**Done when:** tests pass, and against a local DB-less Kong:

```bash
curl -s $KONG_ADMIN_URL/services?tags=agentos | jq '.data[].name'
curl -s $AGENTOS_PUBLIC_BASE/agents/claude-session-watcher-dvl/.well-known/agent-card.json | jq .url
```

The card is reachable **through the gateway**, and its `url` is the gateway's own address.

---

### Task 4: A2A `message/send` and streaming

**Files:**
- Modify: `server/agentapi.py`, `server/extasks.py`
- Create: `tests/test_a2a.py`

**Interfaces:**
- Consumes: `tmuxio.say`, `tmuxio.answer`, `tmuxio.interrupt`, `tmuxio.pending`, `runner.run_turn`, `parser` tail.
- Produces: `message/send`, `message/stream`, `tasks/cancel`, `tasks/resubscribe`.

#### 4.1 `message/send`

```python
async def message_send(session_id: str, params: dict) -> dict:
    msg  = params.get("message") or _bad("message is required")
    text = "".join(p["text"] for p in msg.get("parts", []) if p.get("kind") == "text")
    if not text.strip():
        _bad("message must contain a text part")
    cid  = msg.get("contextId") or session_id
    task = extasks.create(session_id, text=text, context_id=cid)
    res  = tmuxio.say(session_id, text)
    if not res.get("ok"):
        return extasks.fail(task.id, res.get("error", "delivery failed")).as_a2a()
    return extasks.advance(task.id, "working").as_a2a()
```

Returns immediately with a task in `working`. The caller polls `tasks/get` or subscribes. **The documented happy path is send + poll**, with streaming as the optimisation — portal try-it consoles cannot render a five-minute SSE stream, and if streaming is the documented path the API looks broken in the portal's own console.

`contextId` maps to the session: the pane is one continuous conversation, so every task in a session shares a context. Two callers sending to the same agent share that context — document it loudly, and offer `isolation: per-task` (spawn a fresh pane per task) as a later option.

#### 4.2 `message/stream`

SSE, `Content-Type: text/event-stream`, one `data:` frame per event. Source is `runner.run_turn`'s stream-json lines, which are already one JSON object per line, plus state transitions from the poller.

Frames: `status-update` on every state change, `artifact-update` when the turn produces output, terminal frame carrying the completed task. Heartbeat comment every 15s so intermediaries do not reap an idle stream.

End-to-end unbuffered: `response_buffering: false` on the Kong route (Task 3), and no buffering middleware on the listener.

#### 4.3 `input-required`

The most important state in this system, and the reason the repo exists. `tmuxio.pending()` non-null means a human gate is open — a permission prompt, a multiple-choice question. The task moves to `input-required` and the pending prompt is attached as a `DataPart` so the caller can render the actual choices.

Answering:

```
POST / {"method": "message/send", "params": {"taskId": "...", "message": {...}}}
```

Routed to `tmuxio.answer()` when the pending prompt is a choice, `tmuxio.say()` when it is free text. Never auto-answer a multiple-choice question from this path — that rule already exists in `autonomy.py` (commit d252dbb) and it applies with more force to an external caller.

#### 4.4 `tasks/cancel`

Calls `tmuxio.interrupt()`, moves the task to `canceled`. Cancelling a task that is already terminal returns `-32602` rather than pretending to succeed.

- [ ] **Step 1: Write the failing tests**

```python
def test_message_send_returns_a_working_task_immediately()
def test_message_send_rejects_a_message_with_no_text_part()   # -32602
def test_message_send_failure_marks_the_task_failed()
def test_context_id_defaults_to_the_session()
def test_pending_prompt_moves_the_task_to_input_required()
def test_pending_prompt_is_attached_as_a_data_part()
def test_answering_a_choice_routes_to_tmuxio_answer()
def test_free_text_answer_routes_to_tmuxio_say()
def test_cancel_interrupts_the_pane()
def test_cancel_on_a_terminal_task_is_32602()
def test_stream_emits_status_updates_then_a_terminal_frame()
def test_stream_sends_a_heartbeat()
```

- [ ] **Step 2: Implement `message/send`** plus the `extasks` transitions.
- [ ] **Step 3: Implement the SSE endpoint** and `tasks/resubscribe`.
- [ ] **Step 4: Implement `tasks/cancel`.**
- [ ] **Step 5: End-to-end check through Kong.**

```bash
H=claude-session-watcher-dvl
curl -s -H "apikey: $KEY" -X POST $AGENTOS_PUBLIC_BASE/agents/$H \
  -d '{"jsonrpc":"2.0","id":1,"method":"message/send",
       "params":{"message":{"role":"user","parts":[{"kind":"text","text":"summarise README.md"}]}}}'
# pane visibly starts typing; then:
curl -s -H "apikey: $KEY" -X POST $AGENTOS_PUBLIC_BASE/agents/$H \
  -d '{"jsonrpc":"2.0","id":2,"method":"tasks/get","params":{"id":"<tid>"}}'
```

**Done when:** all four test files pass, a task driven through Kong visibly types into the pane, and a permission prompt in that pane surfaces as `input-required` with its choices intact.

---

## Conformance checklist

- [ ] Card served at `/.well-known/agent-card.json`, reachable through Kong
- [ ] JSON-RPC 2.0 envelope with correct error codes (`-32600`, `-32601`, `-32602`, `-32603`)
- [ ] All task states emitted, with transition history
- [ ] SSE unbuffered end-to-end, heartbeats present
- [ ] `tasks/cancel` actually interrupts the pane
- [ ] `contextId` preserved across turns
- [ ] Card `securitySchemes` matches the Kong auth plugin exactly
- [ ] Card `url` is the gateway address, never the listener

**Verify method names, card fields and `A2A_VERSION` against the A2A spec revision you are targeting before shipping.** That spec has moved more than once; this plan encodes a shape, not a citation.

## Out of scope

Konnect API products, spec generation, the AgentPortal, per-caller consumers and ACLs, push-notification webhooks, multi-host targets, per-task pane isolation. Each is a follow-up plan.
