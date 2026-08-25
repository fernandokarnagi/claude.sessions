# Agent Workflows — design

Date: 2026-08-25
Status: approved design, pending implementation plan

## Problem

The dashboard can spawn and drive individual sessions, but every session starts
from a blank prompt. Repeatable multi-agent procedures — a research pass, then a
build pass, then a review pass, each with its own agent personas — live only in
the operator's head and get retyped every time.

This module stores those procedures as first-class, editable, portable objects
and binds one to a session so the operator can drive it stage by stage.

## Scope

A **blueprint** module. A workflow is a definition: a roster of agents and an
ordered list of stages. Assigning a workflow to a session stores a binding and
lets the operator send the current stage's composed prompt into that one live
REPL.

Explicitly out of scope for this project:

- Spawning one session per agent.
- Routing hand-offs between sessions at runtime.
- Auto-advancing stages.
- Writing agent files into the user's project directories.

The schema is designed so an orchestration runtime can be added later as a
separate project without reshaping stored data.

## Data model

New module `server/workflows.py`. Single JSON file `server/.workflows.json`,
guarded by an `RLock`, written atomically (tmp file + `os.replace`), gitignored
as per-user state — the same pattern as `server/projects.py`.

```json
{
  "workflows": {
    "<wid>": {
      "title": "Feature delivery",
      "description": "Research, build, review",
      "created_at": "<iso8601>",
      "updated_at": "<iso8601>",
      "agents": [
        {
          "id": "researcher",
          "name": "Researcher",
          "role": "Find prior art and constraints",
          "model": "opus",
          "prompt": "<system prompt / agent.md body>"
        }
      ],
      "stages": [
        {
          "id": "s1",
          "name": "Discovery",
          "goal": "Establish what already exists",
          "mode": "parallel",
          "agent_ids": ["researcher", "analyst"],
          "exit_criteria": "A written list of existing components to reuse"
        }
      ]
    }
  },
  "bindings": {
    "<session_id>": {
      "workflow_id": "<wid>",
      "stage_index": 0,
      "assigned_at": "<iso8601>",
      "sent": ["s1"]
    }
  }
}
```

Field rules:

- `wid` — `uuid4().hex[:12]`, as in `projects.py`.
- Agent `id` — a slug derived from the name, uniquified within the workflow.
  Stages reference agents by this id, so one agent's prompt is stored once and
  reused across stages.
- Stage `id` — `s<n>`, where `n` is one past the highest number already used in
  that workflow, so an id is never reused after a delete. Ids are assigned on
  insert and never rewritten, including on reorder, so a binding's `sent` list
  survives editing. A `PUT` may submit a stage with no `id`; the store mints one
  and preserves every id it is given.
- `mode` — one of `coordinator`, `handoff`, `parallel`, `solo`.
- `model` — free text, defaulting to `"opus"`; the dashboard does not validate
  model names, matching how `/api/dispatch` passes the model straight through.
- `sent` — stage ids already sent into the session, so the UI can mark a stage
  as re-sent rather than sent for the first time.

Cascades:

- Deleting an agent removes its id from every stage's `agent_ids`.
- Deleting a workflow removes every binding pointing at it.
- `rekey(old_id, new_id)` moves a binding onto a new session id, so a `/clear`
  (see `tmuxio.reset`) does not drop the session out of its workflow. Mirrors
  `projects.rekey`.

A session has at most one binding. `stage_index` clamps to
`[0, len(stages) - 1]`; a workflow with no stages clamps to `0` and sends
nothing.

## Prompt composition

`compose_stage(wid, stage_index) -> str` is a pure function over the store: no
tmux, no network. It is the substance of the module and gets its own test file.

Output is one markdown block:

1. Workflow title and description as framing context.
2. Stage name, goal, and exit criteria.
3. A mode sentence that states how the agents work together:
   - `coordinator` — the first agent in `agent_ids` leads and delegates to the
     rest; the lead owns the final answer.
   - `handoff` — agents run in the listed order, each taking the previous
     agent's output as input.
   - `parallel` — every agent works the same input independently, then the
     results are merged.
   - `solo` — the single agent runs the stage alone.
4. One `## <Name> — <role>` section per participating agent, containing that
   agent's prompt body.

Unknown agent ids in a stage raise; the API validates on write so this cannot
be reached from a saved workflow.

## HTTP API

Added to `server/app.py`, following the existing route style (Pydantic bodies,
`HTTPException` with 400 for validation and 409 for state conflicts).

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/workflows` | List with agent, stage, and bound-session counts |
| POST | `/api/workflows` | Create from title + description |
| GET | `/api/workflows/{wid}` | Full document |
| PUT | `/api/workflows/{wid}` | Replace header, agents, and stages |
| DELETE | `/api/workflows/{wid}` | Delete workflow and its bindings |
| GET | `/api/workflows/{wid}/export` | YAML text of the workflow |
| POST | `/api/workflows/import` | YAML body creates a new workflow |
| POST | `/api/workflows/{wid}/preview` | `{stage_index}` returns composed prompt |
| GET | `/api/sessions/{sid}/workflow` | Binding plus composed current stage |
| POST | `/api/sessions/{sid}/workflow` | `{workflow_id}` binds |
| DELETE | `/api/sessions/{sid}/workflow` | Unbinds |
| POST | `/api/sessions/{sid}/workflow/send` | `tmuxio.say(composed)` into the live pane |
| POST | `/api/sessions/{sid}/workflow/advance` | `{delta: +1 or -1}` moves the stage pointer |

Validation rejects, with 400: an empty title; a stage referencing an unknown
agent id; a mode outside the enum; duplicate agent ids; a `stage_index` out of
range on preview. `send` returns 409 when the session has no live tmux pane,
matching how `relay` reports a dead target.

Import parses YAML with `yaml.safe_load`, runs the same validation as `PUT`,
and always mints a fresh `wid` — import never overwrites an existing workflow.

New dependency: `pyyaml>=6.0` in `requirements.txt`. It is the only package
added.

## UI

New page `server/static/workflows.html` plus a `Workflows` controller in
`server/static/app.js`. A `🧩 Workflows` pill goes into `NAV_PILLS` after
Projects. Two views on one page, keyed off `?id=`, mirroring `Projects`.

**List view** — a grid of workflow cards showing title, description, and
`N agents · M stages · K sessions`, with Open, Edit, Export, and Delete
actions. The topbar carries `✨ New workflow` and `⬆ Import YAML`.

**Editor view** (`?id=<wid>`) — three parts:

- Header: title and description.
- Agents: a card per agent with name, role, model, and a prompt textarea;
  add and remove.
- Stages: an ordered list, each with name, goal, mode select, agent
  checkboxes, and exit criteria; add, remove, and reorder. Each stage has a
  `👁 Preview prompt` control that shows exactly what a send would type.

Save is an explicit button issuing one `PUT` of the whole document. There is no
autosave, so an in-progress edit cannot race a background refresh — the editor
view does not poll.

**Session detail** — a new `#workflow` panel. Bound sessions show the workflow
name, a `Stage 2/4 · Build` stepper, the composed prompt in a collapsed block,
and `▶ Send stage`, `✓ Advance`, and `✕ Unassign`. Unbound sessions show an
`Assign workflow` dropdown. A send with no live pane surfaces the 409 message
in the panel rather than an alert.

**Board and triage** — a small `🧩 <workflow>` badge on cards for bound
sessions, built from a single `bindings_by_session()` call the way
`projects.tags_by_session()` avoids per-session loads.

Static asset query strings bump to `?v=169` across all pages, per the existing
cache-busting convention.

## Testing

`tests/test_workflows.py` — store behaviour against a temp file:

- Create, read, update, delete, and persistence across a reload.
- Deleting an agent strips it from every stage.
- Deleting a workflow drops its bindings.
- Advance clamps at the first and last stage.
- `rekey` moves a binding to a new session id.
- YAML export then import round-trips to an equal document with a new id.
- Import validation rejects unknown agent ids, bad modes, and empty titles.

`tests/test_compose.py` — one composition test per mode asserting the mode
sentence and that every participating agent's prompt appears; an unknown agent
id raises.

The send path is tested with `tmuxio.say` monkeypatched: it asserts the composed
text reaches `say` and that a missing pane yields 409. No test starts a REPL or
spends tokens.

## Rejected alternatives

- **Extend `projects.py`.** Different cardinality (a session has many projects,
  one workflow), a different lifecycle, and it would roughly double that file.
- **Orchestrated fleet now.** Spawning a session per agent and routing hand-offs
  needs a scheduler, failure handling, and real token spend. The blueprint is
  useful on its own and is the input that runtime would need anyway.
- **Auto-advance on WAITING.** A session reaches WAITING for many reasons; a
  misfire would send the next stage into an unrelated pause.
- **JSON instead of YAML for export.** Zero new dependency, but long system
  prompts are unreadable as JSON strings and the point of export is that a human
  edits and version-controls the file.
