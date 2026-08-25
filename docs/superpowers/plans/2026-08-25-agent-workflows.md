# Agent Workflows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Workflows module to the Agent OS dashboard that stores reusable multi-agent blueprints (agent roster + ordered stages) and binds one to a session so the operator can send each stage's composed prompt into that session's live REPL.

**Architecture:** A new `server/workflows.py` store follows the exact pattern of `server/projects.py` — one gitignored JSON file, an `RLock`, atomic tmp+replace writes. A pure `compose_stage()` renders a stage into one markdown prompt. `server/app.py` gains CRUD, YAML import/export, preview, and per-session bind/send/advance routes. The front end adds `workflows.html` plus a `Workflows` controller (list + editor) and a `Workflow` panel on the session detail page. No orchestration, no spawning, no auto-advance.

**Tech Stack:** Python 3.14, FastAPI, Pydantic v2, pytest + `fastapi.testclient` (needs httpx), PyYAML, vanilla ES6 (no build step), plain CSS.

**Spec:** `docs/superpowers/specs/2026-08-25-agent-workflows-design.md`

## Global Constraints

- Python runs from the repo venv. The repo's documented test command is `.venv/bin/python -m pytest` (README.md:196, docs/ARCHITECTURE.md:131) — `-m` is what puts the repo root on `sys.path` so `from server import ...` resolves. Never invoke the bare `.venv/bin/pytest` shim, and never add a `conftest.py` to work around it.
- Store file is `server/.workflows.json`, gitignored like `.projects.json` — never commit it, never `rm` a real one during testing. Tests always `monkeypatch.setattr(workflows, "_PATH", str(tmp_path / "workflows.json"))`.
- Store module writes atomically: `json.dump` to `_PATH + ".tmp"`, then `os.replace`. Guard every read/write with a module-level `threading.RLock()`.
- New dependencies: `pyyaml>=6.0` (the feature) and `httpx>=0.27` (what `fastapi.testclient` imports; absent from the venv today, which is why `tests/test_pins.py` currently fails to collect). Both added in Task 4.
- Coordination modes are exactly `("coordinator", "handoff", "parallel", "solo")`.
- Workflow ids are `uuid.uuid4().hex[:12]`. Stage ids are `s<n>` where `n` is one past the highest number already used in that workflow; ids are never reused and never rewritten.
- Agent `model` defaults to `"opus"` and is never validated against a model list.
- Every timestamp is `datetime.now(timezone.utc).isoformat()`.
- API errors: 400 for validation, 404 for unknown workflow/session, 409 for "no live tmux session".
- Front-end cache busting: every `?v=168` in `server/static/*.html` becomes `?v=169` in the task that first ships UI.
- HTML is always escaped with the existing `esc()` helper before interpolation.

---

### Task 1: Workflow store — CRUD and validation

**Files:**
- Create: `server/workflows.py`
- Create: `tests/test_workflows.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: nothing.
- Produces: `MODES: tuple[str, ...]`; `validate(agents: list[dict], stages: list[dict], previous: dict | None = None) -> tuple[list[dict], list[dict]]` (raises `ValueError`); `list_workflows() -> list[dict]`; `get_workflow(wid: str) -> dict | None`; `create_workflow(title: str, description: str = "") -> dict`; `update_workflow(wid, title=None, description=None, agents=None, stages=None) -> dict | None`; `delete_workflow(wid: str) -> bool`; module globals `_PATH`, `_lock`, `_load()`, `_save(data)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_workflows.py`:

```python
"""
Workflows — the blueprint store.

A workflow is a roster of agents plus an ordered list of stages that reference
them. Nothing here talks to tmux: this file is the store's contract only.

Every test points the store at a tmp file, so the real state under server/ is
never touched.
"""

import pytest

from server import workflows

AGENTS = [
    {"name": "Researcher", "role": "Find prior art", "prompt": "You research."},
    {"name": "Builder", "role": "Write the code", "model": "sonnet", "prompt": "You build."},
]


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "_PATH", str(tmp_path / "workflows.json"))


def test_create_defaults():
    wf = workflows.create_workflow("  Feature delivery  ", "  research then build  ")
    assert wf["title"] == "Feature delivery"          # stored trimmed
    assert wf["description"] == "research then build"
    assert wf["agents"] == [] and wf["stages"] == []
    assert len(wf["id"]) == 12
    assert wf["created_at"] and wf["updated_at"]


def test_create_blank_title_falls_back():
    assert workflows.create_workflow("   ")["title"] == "Untitled workflow"


def test_update_assigns_agent_and_stage_ids():
    wid = workflows.create_workflow("W")["id"]
    wf = workflows.update_workflow(wid, agents=AGENTS, stages=[
        {"name": "Discovery", "goal": "look around", "mode": "parallel",
         "agent_ids": ["researcher", "builder"], "exit_criteria": "a list"},
    ])
    assert [a["id"] for a in wf["agents"]] == ["researcher", "builder"]
    assert wf["agents"][0]["model"] == "opus"          # default
    assert wf["agents"][1]["model"] == "sonnet"
    assert [s["id"] for s in wf["stages"]] == ["s1"]


def test_duplicate_agent_names_get_distinct_ids():
    wid = workflows.create_workflow("W")["id"]
    wf = workflows.update_workflow(wid, agents=[
        {"name": "Reviewer", "prompt": "a"},
        {"name": "Reviewer", "prompt": "b"},
    ], stages=[])
    assert [a["id"] for a in wf["agents"]] == ["reviewer", "reviewer-2"]


def test_stage_ids_are_never_reused():
    wid = workflows.create_workflow("W")["id"]
    workflows.update_workflow(wid, agents=AGENTS, stages=[
        {"name": "One", "mode": "solo", "agent_ids": ["researcher"]},
        {"name": "Two", "mode": "solo", "agent_ids": ["builder"]},
    ])
    # Drop the first stage, then add another: the new one must not be s1.
    wf = workflows.update_workflow(wid, stages=[
        {"id": "s2", "name": "Two", "mode": "solo", "agent_ids": ["builder"]},
        {"name": "Three", "mode": "solo", "agent_ids": ["builder"]},
    ])
    assert [s["id"] for s in wf["stages"]] == ["s2", "s3"]


def test_stage_ids_survive_deleting_the_last_stage():
    """The high-water mark comes from what is stored, not from what the
    caller sent — otherwise deleting the newest stage and adding one in the
    same save would hand the new stage the id that was just freed."""
    wid = workflows.create_workflow("W")["id"]
    workflows.update_workflow(wid, agents=AGENTS, stages=[
        {"name": "One", "mode": "solo", "agent_ids": ["builder"]},
        {"name": "Two", "mode": "solo", "agent_ids": ["builder"]},
    ])
    wf = workflows.update_workflow(wid, stages=[
        {"id": "s1", "name": "One", "mode": "solo", "agent_ids": ["builder"]},
        {"name": "Fresh", "mode": "solo", "agent_ids": ["builder"]},
    ])
    assert [s["id"] for s in wf["stages"]] == ["s1", "s3"]


def test_deleting_an_agent_strips_it_from_stages():
    wid = workflows.create_workflow("W")["id"]
    workflows.update_workflow(wid, agents=AGENTS, stages=[
        {"name": "Both", "mode": "parallel", "agent_ids": ["researcher", "builder"]},
    ])
    wf = workflows.update_workflow(wid, agents=[AGENTS[0]])
    assert [a["id"] for a in wf["agents"]] == ["researcher"]
    assert wf["stages"][0]["agent_ids"] == ["researcher"]


def test_a_stage_left_with_no_agents_is_still_kept():
    """Editing is iterative — losing the stage as a side effect of removing an
    agent would delete work the operator never asked to delete."""
    wid = workflows.create_workflow("W")["id"]
    workflows.update_workflow(wid, agents=AGENTS, stages=[
        {"name": "Solo", "mode": "solo", "agent_ids": ["builder"]},
    ])
    wf = workflows.update_workflow(wid, agents=[AGENTS[0]])
    assert wf["stages"][0]["agent_ids"] == []


def test_unknown_agent_id_rejected():
    wid = workflows.create_workflow("W")["id"]
    with pytest.raises(ValueError, match="unknown agent"):
        workflows.update_workflow(wid, agents=AGENTS, stages=[
            {"name": "X", "mode": "solo", "agent_ids": ["nobody"]},
        ])


def test_bad_mode_rejected():
    wid = workflows.create_workflow("W")["id"]
    with pytest.raises(ValueError, match="mode"):
        workflows.update_workflow(wid, agents=AGENTS, stages=[
            {"name": "X", "mode": "swarm", "agent_ids": ["builder"]},
        ])


def test_solo_stage_needs_exactly_one_agent():
    wid = workflows.create_workflow("W")["id"]
    with pytest.raises(ValueError, match="solo"):
        workflows.update_workflow(wid, agents=AGENTS, stages=[
            {"name": "X", "mode": "solo", "agent_ids": ["researcher", "builder"]},
        ])


def test_nameless_agent_rejected():
    wid = workflows.create_workflow("W")["id"]
    with pytest.raises(ValueError, match="name"):
        workflows.update_workflow(wid, agents=[{"name": "  ", "prompt": "x"}], stages=[])


def test_list_counts_and_persistence():
    a = workflows.create_workflow("A")
    workflows.update_workflow(a["id"], agents=AGENTS, stages=[
        {"name": "One", "mode": "solo", "agent_ids": ["builder"]},
    ])
    workflows.create_workflow("B")
    rows = workflows.list_workflows()
    assert [r["title"] for r in rows] == ["B", "A"]        # newest first
    row = next(r for r in rows if r["id"] == a["id"])
    assert row["agent_count"] == 2 and row["stage_count"] == 1
    assert row["session_count"] == 0
    assert "agents" not in row                             # list stays light


def test_update_unknown_workflow_returns_none():
    assert workflows.update_workflow("deadbeef0000", title="x") is None


def test_delete():
    wid = workflows.create_workflow("W")["id"]
    assert workflows.delete_workflow(wid) is True
    assert workflows.get_workflow(wid) is None
    assert workflows.delete_workflow(wid) is False


def test_corrupt_file_reads_as_empty(tmp_path, monkeypatch):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(workflows, "_PATH", str(path))
    assert workflows.list_workflows() == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_workflows.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.workflows'`

- [ ] **Step 3: Write the store**

Create `server/workflows.py`:

```python
"""
workflows.py — reusable multi-agent blueprints.

A workflow is a roster of agents (name, role, model, system prompt) plus an
ordered list of stages. A stage names which agents take part, how they work
together (mode), what the stage is for, and how you know it is done. Stages
reference agents by id, so one agent's prompt is stored once however many
stages use it.

This module is a blueprint store only: nothing here spawns a session, routes a
hand-off, or talks to tmux. Composing a stage into a prompt lives in the same
module (compose_stage) because it is a pure read of this shape and nothing
else. Binding a workflow to a session lives here too — the binding is one
pointer into a workflow, and keeping it beside the workflow is what makes a
delete able to clean up after itself.

Shape of .workflows.json:
    {
      "workflows": {
        "<wid>": {
          "title": "...", "description": "...",
          "created_at": "<iso>", "updated_at": "<iso>",
          "agents": [{"id": "researcher", "name": "Researcher",
                      "role": "...", "model": "opus", "prompt": "..."}],
          "stages": [{"id": "s1", "name": "Discovery", "goal": "...",
                      "mode": "parallel", "agent_ids": ["researcher"],
                      "exit_criteria": "..."}]
        }
      },
      "bindings": {
        "<session_id>": {"workflow_id": "<wid>", "stage_index": 0,
                         "assigned_at": "<iso>", "sent": ["s1"]}
      }
    }
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone

_PATH = os.path.join(os.path.dirname(__file__), ".workflows.json")
_lock = threading.RLock()

# How the agents in one stage work together. Per stage, not per workflow: a
# real procedure researches in parallel and then writes solo.
MODES = ("coordinator", "handoff", "parallel", "solo")
DEFAULT_MODE = "solo"
DEFAULT_MODEL = "opus"

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_STAGE_ID_RE = re.compile(r"^s(\d+)$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    try:
        with open(_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"workflows": {}, "bindings": {}}
    data.setdefault("workflows", {})
    data.setdefault("bindings", {})
    return data


def _save(data: dict) -> None:
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, _PATH)


def _slug(name: str) -> str:
    return _SLUG_RE.sub("-", name.strip().lower()).strip("-") or "agent"


def _next_stage_n(stages: list[dict]) -> int:
    """One past the highest stage number ever used here.

    Ids are never recycled: a binding remembers which stages it has already
    sent by id, and reusing s1 for a brand-new stage would mark it sent.
    """
    used = [int(m.group(1)) for s in stages
            if (m := _STAGE_ID_RE.match(str(s.get("id") or "")))]
    return max(used, default=0) + 1


def validate(agents: list[dict], stages: list[dict],
             previous: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Normalise a roster + stage list, or raise ValueError.

    Returns cleaned copies: agent ids filled in and made unique, stage ids
    minted where missing, agent_ids filtered down to agents that still exist.

    `previous` is the stored record being replaced, when there is one. It
    answers two questions this call cannot answer on its own:

      - An agent id in a stage that is not in the new roster: was it just
        deleted, or did the caller invent it? Deleting an agent is a normal
        edit, so its stages are quietly cleaned up; an id that never existed
        is a mistake and raises.
      - Which stage numbers have been used before, so deleting the newest
        stage and adding one in the same save cannot recycle its id.
    """
    prev_agent_ids = {str(a.get("id") or "")
                      for a in (previous or {}).get("agents", [])}
    prev_stages = (previous or {}).get("stages", [])
    clean_agents: list[dict] = []
    seen: set[str] = set()
    for a in agents:
        name = str(a.get("name") or "").strip()
        if not name:
            raise ValueError("every agent needs a name")
        aid = str(a.get("id") or "").strip() or _slug(name)
        if aid in seen:
            n = 2
            while f"{aid}-{n}" in seen:
                n += 1
            aid = f"{aid}-{n}"
        seen.add(aid)
        clean_agents.append({
            "id": aid,
            "name": name,
            "role": str(a.get("role") or "").strip(),
            "model": str(a.get("model") or "").strip() or DEFAULT_MODEL,
            "prompt": str(a.get("prompt") or "").strip(),
        })

    known = {a["id"] for a in clean_agents}
    clean_stages: list[dict] = []
    next_n = max(_next_stage_n(stages), _next_stage_n(prev_stages))
    for s in stages:
        name = str(s.get("name") or "").strip()
        if not name:
            raise ValueError("every stage needs a name")
        mode = str(s.get("mode") or DEFAULT_MODE).strip()
        if mode not in MODES:
            raise ValueError(f"stage {name!r}: mode must be one of {MODES}")
        ids = [str(x) for x in (s.get("agent_ids") or [])]
        unknown = [i for i in ids if i not in known and i not in prev_agent_ids]
        if unknown:
            raise ValueError(f"stage {name!r}: unknown agent id {unknown[0]!r}")
        ids = [i for i in ids if i in known]
        if mode == "solo" and len(ids) > 1:
            raise ValueError(f"stage {name!r}: a solo stage takes one agent")
        sid = str(s.get("id") or "").strip()
        if not _STAGE_ID_RE.match(sid):
            sid = f"s{next_n}"
            next_n += 1
        clean_stages.append({
            "id": sid,
            "name": name,
            "goal": str(s.get("goal") or "").strip(),
            "mode": mode,
            "agent_ids": ids,
            "exit_criteria": str(s.get("exit_criteria") or "").strip(),
        })
    return clean_agents, clean_stages


def _counts(data: dict) -> dict[str, int]:
    counts = {wid: 0 for wid in data["workflows"]}
    for b in data["bindings"].values():
        wid = b.get("workflow_id")
        if wid in counts:
            counts[wid] += 1
    return counts


def _row(wid: str, rec: dict, sessions: int) -> dict:
    """List shape — counts only. The full agents/stages payload is big enough
    that the list page has no business carrying it."""
    return {
        "id": wid,
        "title": rec.get("title", ""),
        "description": rec.get("description", ""),
        "created_at": rec.get("created_at", ""),
        "updated_at": rec.get("updated_at", ""),
        "agent_count": len(rec.get("agents", [])),
        "stage_count": len(rec.get("stages", [])),
        "session_count": sessions,
    }


def _full(wid: str, rec: dict) -> dict:
    out = dict(rec)
    out["id"] = wid
    return out


def list_workflows() -> list[dict]:
    with _lock:
        data = _load()
        counts = _counts(data)
        rows = [_row(wid, rec, counts.get(wid, 0))
                for wid, rec in data["workflows"].items()]
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return rows


def get_workflow(wid: str) -> dict | None:
    with _lock:
        rec = _load()["workflows"].get(wid)
        return _full(wid, rec) if rec else None


def create_workflow(title: str, description: str = "") -> dict:
    wid = uuid.uuid4().hex[:12]
    now = _now()
    rec = {
        "title": title.strip() or "Untitled workflow",
        "description": description.strip(),
        "created_at": now,
        "updated_at": now,
        "agents": [],
        "stages": [],
    }
    with _lock:
        data = _load()
        data["workflows"][wid] = rec
        _save(data)
    return _full(wid, rec)


def update_workflow(wid: str, title: str | None = None,
                    description: str | None = None,
                    agents: list[dict] | None = None,
                    stages: list[dict] | None = None) -> dict | None:
    """Replace whatever is passed. Agents and stages validate together, because
    a stage is only valid against the roster it is saved with."""
    with _lock:
        data = _load()
        rec = data["workflows"].get(wid)
        if rec is None:
            return None
        if agents is not None or stages is not None:
            next_agents = agents if agents is not None else rec.get("agents", [])
            next_stages = stages if stages is not None else rec.get("stages", [])
            rec["agents"], rec["stages"] = validate(next_agents, next_stages,
                                                    previous=rec)
        if title is not None:
            rec["title"] = title.strip() or rec.get("title", "Untitled workflow")
        if description is not None:
            rec["description"] = description.strip()
        rec["updated_at"] = _now()
        _save(data)
        return _full(wid, rec)


def delete_workflow(wid: str) -> bool:
    with _lock:
        data = _load()
        if wid not in data["workflows"]:
            return False
        del data["workflows"][wid]
        for sid in [s for s, b in data["bindings"].items()
                    if b.get("workflow_id") == wid]:
            del data["bindings"][sid]
        _save(data)
    return True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflows.py -v`
Expected: PASS (17 tests)

- [ ] **Step 5: Gitignore the store file**

`.gitignore` already has a "Local runtime state (per-user data, not code)" block. Add the new file inside it, after `server/.pins.json`:

```
server/.pins.json
server/.workflows.json
```

The `server/.*.tmp` pattern already covers the atomic-write temp file.

Verify with: `git check-ignore -v server/.workflows.json`
Expected: a line naming `.gitignore` and the new pattern.

- [ ] **Step 6: Commit**

```bash
git add server/workflows.py tests/test_workflows.py .gitignore
git commit -m "feat(workflows): blueprint store for multi-agent workflows"
```

---

### Task 2: Stage prompt composition

**Files:**
- Modify: `server/workflows.py` (append after `delete_workflow`)
- Create: `tests/test_compose.py`

**Interfaces:**
- Consumes: `get_workflow(wid)`, `MODES` from Task 1.
- Produces: `compose_stage(wid: str, stage_index: int) -> str` (raises `ValueError` on an unknown workflow or an out-of-range index); `mode_sentence(mode: str, names: list[str]) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_compose.py`:

```python
"""
Stage composition — turning a stage into the prompt that gets typed.

This is the substance of the module: the stored blueprint only matters
because of what it renders to. Pure function, no I/O beyond the store.
"""

import pytest

from server import workflows

AGENTS = [
    {"name": "Researcher", "role": "Find prior art", "prompt": "You research thoroughly."},
    {"name": "Builder", "role": "Write the code", "prompt": "You write the code."},
    {"name": "Reviewer", "role": "Check the work", "prompt": "You review."},
]


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "_PATH", str(tmp_path / "workflows.json"))


def build(mode, agent_ids, **stage):
    wid = workflows.create_workflow("Feature delivery", "Research then build")["id"]
    workflows.update_workflow(wid, agents=AGENTS, stages=[
        dict({"name": "Discovery", "goal": "Establish what exists",
              "mode": mode, "agent_ids": agent_ids,
              "exit_criteria": "A written list"}, **stage),
    ])
    return wid


def test_header_carries_workflow_and_stage():
    text = workflows.compose_stage(build("solo", ["builder"]), 0)
    assert "# Workflow: Feature delivery" in text
    assert "Research then build" in text
    assert "## Stage 1/1: Discovery" in text
    assert "Goal: Establish what exists" in text
    assert "Exit criteria: A written list" in text


def test_solo_sentence_and_body():
    text = workflows.compose_stage(build("solo", ["builder"]), 0)
    assert "Coordination: solo. Builder runs this stage alone." in text
    assert "### Builder — Write the code" in text
    assert "You write the code." in text
    assert "Researcher" not in text          # non-participants stay out


def test_coordinator_names_the_lead():
    text = workflows.compose_stage(build("coordinator", ["builder", "researcher", "reviewer"]), 0)
    assert ("Coordination: coordinator. Builder leads this stage and delegates "
            "to Researcher and Reviewer. Builder owns the final answer.") in text
    for name in ("Builder", "Researcher", "Reviewer"):
        assert f"### {name} — " in text


def test_handoff_shows_the_chain():
    text = workflows.compose_stage(build("handoff", ["researcher", "builder", "reviewer"]), 0)
    assert ("Coordination: hand-off. Run in order: Researcher → Builder → Reviewer. "
            "Each agent takes the previous agent's output as its input.") in text


def test_parallel_sentence():
    text = workflows.compose_stage(build("parallel", ["researcher", "reviewer"]), 0)
    assert ("Coordination: parallel. Researcher and Reviewer each work the same "
            "input independently; merge the results at the end.") in text


def test_empty_optional_fields_are_omitted():
    wid = build("solo", ["builder"], goal="", exit_criteria="")
    workflows.update_workflow(wid, description="")
    text = workflows.compose_stage(wid, 0)
    assert "Goal:" not in text
    assert "Exit criteria:" not in text
    assert text.startswith("# Workflow: Feature delivery\n\n## Stage 1/1")


def test_stage_with_no_agents_says_so():
    wid = build("parallel", [])
    text = workflows.compose_stage(wid, 0)
    assert "No agents are assigned to this stage." in text


def test_index_out_of_range():
    wid = build("solo", ["builder"])
    with pytest.raises(ValueError, match="stage index"):
        workflows.compose_stage(wid, 1)


def test_unknown_workflow():
    with pytest.raises(ValueError, match="workflow not found"):
        workflows.compose_stage("deadbeef0000", 0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_compose.py -v`
Expected: FAIL — `AttributeError: module 'server.workflows' has no attribute 'compose_stage'`

- [ ] **Step 3: Implement composition**

Append to `server/workflows.py`:

```python
# ---------------------------------------------------------------------------
# Composition — a stage rendered as the prompt that actually gets typed.
# ---------------------------------------------------------------------------

def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " and " + names[-1]


def mode_sentence(mode: str, names: list[str]) -> str:
    """One line telling the session how these agents work together.

    This sentence is the whole difference between a pile of personas and a
    procedure, so it is spelled out rather than left for the model to infer.
    """
    if not names:
        return "No agents are assigned to this stage."
    if mode == "solo":
        return f"Coordination: solo. {names[0]} runs this stage alone."
    if mode == "coordinator":
        lead, rest = names[0], names[1:]
        if not rest:
            return f"Coordination: coordinator. {lead} runs this stage alone."
        return (f"Coordination: coordinator. {lead} leads this stage and "
                f"delegates to {_join(rest)}. {lead} owns the final answer.")
    if mode == "handoff":
        return (f"Coordination: hand-off. Run in order: {' → '.join(names)}. "
                "Each agent takes the previous agent's output as its input.")
    return (f"Coordination: parallel. {_join(names)} each work the same input "
            "independently; merge the results at the end.")


def compose_stage(wid: str, stage_index: int) -> str:
    """Render one stage as a single markdown prompt."""
    wf = get_workflow(wid)
    if wf is None:
        raise ValueError("workflow not found")
    stages = wf.get("stages", [])
    if not 0 <= stage_index < len(stages):
        raise ValueError(f"stage index {stage_index} out of range")
    stage = stages[stage_index]
    by_id = {a["id"]: a for a in wf.get("agents", [])}
    taking_part = [by_id[i] for i in stage.get("agent_ids", []) if i in by_id]

    lines = [f"# Workflow: {wf['title']}"]
    if wf.get("description"):
        lines.append(wf["description"])
    lines.append("")
    lines.append(f"## Stage {stage_index + 1}/{len(stages)}: {stage['name']}")
    if stage.get("goal"):
        lines.append(f"Goal: {stage['goal']}")
    if stage.get("exit_criteria"):
        lines.append(f"Exit criteria: {stage['exit_criteria']}")
    lines.append("")
    lines.append(mode_sentence(stage.get("mode", DEFAULT_MODE),
                               [a["name"] for a in taking_part]))
    for a in taking_part:
        lines.append("")
        head = f"### {a['name']}"
        if a.get("role"):
            head += f" — {a['role']}"
        lines.append(head)
        if a.get("model"):
            lines.append(f"Model: {a['model']}")
        if a.get("prompt"):
            lines.append(a["prompt"])
    return "\n".join(lines).strip() + "\n"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_compose.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add server/workflows.py tests/test_compose.py
git commit -m "feat(workflows): compose a stage into one prompt per coordination mode"
```

---

### Task 3: Session bindings

**Files:**
- Modify: `server/workflows.py` (append after `compose_stage`)
- Create: `tests/test_workflow_bindings.py`

**Interfaces:**
- Consumes: `_load`, `_save`, `_lock`, `_now`, `get_workflow` from Task 1.
- Produces: `bind(session_id: str, wid: str) -> dict | None`; `unbind(session_id: str) -> bool`; `get_binding(session_id: str) -> dict | None`; `advance(session_id: str, delta: int) -> dict | None`; `mark_sent(session_id: str, stage_id: str) -> None`; `bindings_by_session() -> dict[str, dict]`; `rekey(old_id: str, new_id: str) -> None`.

Binding record shape returned by `get_binding`: `{"workflow_id", "title", "stage_index", "stage_count", "assigned_at", "sent": [stage ids]}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_workflow_bindings.py`:

```python
"""
Bindings — one workflow pinned to one session, plus a stage pointer.

The operator drives the pointer by hand; nothing here advances on its own.
"""

import pytest

from server import workflows

SID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

AGENTS = [{"name": "Builder", "prompt": "build"}]
STAGES = [
    {"name": "One", "mode": "solo", "agent_ids": ["builder"]},
    {"name": "Two", "mode": "solo", "agent_ids": ["builder"]},
    {"name": "Three", "mode": "solo", "agent_ids": ["builder"]},
]


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "_PATH", str(tmp_path / "workflows.json"))


@pytest.fixture
def wid():
    w = workflows.create_workflow("Feature delivery")["id"]
    workflows.update_workflow(w, agents=AGENTS, stages=STAGES)
    return w


def test_bind_starts_at_stage_zero(wid):
    b = workflows.bind(SID, wid)
    assert b["workflow_id"] == wid
    assert b["title"] == "Feature delivery"
    assert b["stage_index"] == 0
    assert b["stage_count"] == 3
    assert b["sent"] == []
    assert workflows.get_binding(SID) == b


def test_bind_unknown_workflow(wid):
    assert workflows.bind(SID, "deadbeef0000") is None


def test_rebinding_replaces_and_resets(wid):
    other = workflows.create_workflow("Other")["id"]
    workflows.bind(SID, wid)
    workflows.advance(SID, 1)
    b = workflows.bind(SID, other)
    assert b["workflow_id"] == other
    assert b["stage_index"] == 0


def test_advance_and_clamp(wid):
    workflows.bind(SID, wid)
    assert workflows.advance(SID, 1)["stage_index"] == 1
    assert workflows.advance(SID, 1)["stage_index"] == 2
    assert workflows.advance(SID, 1)["stage_index"] == 2       # clamped at the end
    assert workflows.advance(SID, -1)["stage_index"] == 1
    assert workflows.advance(SID, -5)["stage_index"] == 0      # clamped at the start


def test_advance_without_a_binding():
    assert workflows.advance(SID, 1) is None


def test_mark_sent_is_idempotent(wid):
    workflows.bind(SID, wid)
    workflows.mark_sent(SID, "s1")
    workflows.mark_sent(SID, "s1")
    assert workflows.get_binding(SID)["sent"] == ["s1"]


def test_unbind(wid):
    workflows.bind(SID, wid)
    assert workflows.unbind(SID) is True
    assert workflows.get_binding(SID) is None
    assert workflows.unbind(SID) is False


def test_deleting_the_workflow_drops_the_binding(wid):
    workflows.bind(SID, wid)
    workflows.delete_workflow(wid)
    assert workflows.get_binding(SID) is None


def test_binding_survives_stages_shrinking(wid):
    """Editing a workflow down to fewer stages must not leave a binding
    pointing past the end — the pointer is clamped on read."""
    workflows.bind(SID, wid)
    workflows.advance(SID, 2)
    workflows.update_workflow(wid, stages=[STAGES[0]])
    b = workflows.get_binding(SID)
    assert b["stage_index"] == 0 and b["stage_count"] == 1


def test_bindings_by_session_is_one_read(wid):
    workflows.bind(SID, wid)
    workflows.bind(OTHER, wid)
    workflows.advance(OTHER, 1)
    rows = workflows.bindings_by_session()
    assert rows[SID]["title"] == "Feature delivery"
    assert rows[SID]["stage_name"] == "One"
    assert rows[OTHER]["stage_name"] == "Two"


def test_rekey_moves_the_binding(wid):
    workflows.bind(SID, wid)
    workflows.advance(SID, 1)
    workflows.rekey(SID, OTHER)
    assert workflows.get_binding(SID) is None
    assert workflows.get_binding(OTHER)["stage_index"] == 1


def test_rekey_noop_cases(wid):
    workflows.rekey(SID, SID)
    workflows.rekey("nothing-here", OTHER)
    assert workflows.get_binding(OTHER) is None


def test_rekey_does_not_clobber_an_existing_binding(wid):
    """A session that already carries a workflow keeps it — the /clear target
    is the newer conversation, and its own assignment wins."""
    other_wf = workflows.create_workflow("Other")["id"]
    workflows.bind(SID, wid)
    workflows.bind(OTHER, other_wf)
    workflows.rekey(SID, OTHER)
    assert workflows.get_binding(OTHER)["workflow_id"] == other_wf
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_workflow_bindings.py -v`
Expected: FAIL — `AttributeError: module 'server.workflows' has no attribute 'bind'`

- [ ] **Step 3: Implement bindings**

Append to `server/workflows.py`:

```python
# ---------------------------------------------------------------------------
# Bindings — which workflow a session is running, and where it has got to.
#
# One workflow per session. The stage pointer only ever moves because a human
# pressed Advance; nothing in this module watches a session.
# ---------------------------------------------------------------------------

def _binding_pub(data: dict, sid: str, rec: dict) -> dict | None:
    wf = data["workflows"].get(rec.get("workflow_id"))
    if wf is None:
        return None
    stages = wf.get("stages", [])
    # Clamped on read, not on write: the workflow can be edited down to fewer
    # stages long after the binding was made, and a stored pointer past the
    # end would 500 the session page rather than just showing the last stage.
    idx = max(0, min(int(rec.get("stage_index", 0)), max(len(stages) - 1, 0)))
    return {
        "session_id": sid,
        "workflow_id": rec["workflow_id"],
        "title": wf.get("title", ""),
        "stage_index": idx,
        "stage_count": len(stages),
        "stage_name": stages[idx]["name"] if stages else "",
        "assigned_at": rec.get("assigned_at", ""),
        "sent": list(rec.get("sent", [])),
    }


def bind(session_id: str, wid: str) -> dict | None:
    """Assign a workflow to a session, starting at its first stage."""
    with _lock:
        data = _load()
        if wid not in data["workflows"]:
            return None
        data["bindings"][session_id] = {
            "workflow_id": wid,
            "stage_index": 0,
            "assigned_at": _now(),
            "sent": [],
        }
        _save(data)
        return _binding_pub(data, session_id, data["bindings"][session_id])


def unbind(session_id: str) -> bool:
    with _lock:
        data = _load()
        if data["bindings"].pop(session_id, None) is None:
            return False
        _save(data)
    return True


def get_binding(session_id: str) -> dict | None:
    with _lock:
        data = _load()
        rec = data["bindings"].get(session_id)
        return _binding_pub(data, session_id, rec) if rec else None


def advance(session_id: str, delta: int) -> dict | None:
    """Move the stage pointer, clamped to the workflow's stages."""
    with _lock:
        data = _load()
        rec = data["bindings"].get(session_id)
        if rec is None:
            return None
        wf = data["workflows"].get(rec.get("workflow_id"))
        if wf is None:
            return None
        last = max(len(wf.get("stages", [])) - 1, 0)
        rec["stage_index"] = max(0, min(int(rec.get("stage_index", 0)) + delta, last))
        _save(data)
        return _binding_pub(data, session_id, rec)


def mark_sent(session_id: str, stage_id: str) -> None:
    """Remember that this stage has been typed into the session at least once,
    so the UI can offer "Re-send" instead of "Send"."""
    with _lock:
        data = _load()
        rec = data["bindings"].get(session_id)
        if rec is None:
            return
        sent = rec.setdefault("sent", [])
        if stage_id not in sent:
            sent.append(stage_id)
            _save(data)


def bindings_by_session() -> dict[str, dict]:
    """{session_id: binding} in one file read — for the board and triage
    badges, which decorate many sessions at once."""
    with _lock:
        data = _load()
        out = {}
        for sid, rec in data["bindings"].items():
            pub = _binding_pub(data, sid, rec)
            if pub:
                out[sid] = pub
        return out


def rekey(old_id: str, new_id: str) -> None:
    """Carry a binding onto a new session id (see tmuxio.reset).

    Which procedure you are running is about the work, not the conversation,
    so a /clear shouldn't drop it. A binding already on the new id wins.
    """
    if old_id == new_id:
        return
    with _lock:
        data = _load()
        rec = data["bindings"].pop(old_id, None)
        if rec is None:
            return
        data["bindings"].setdefault(new_id, rec)
        _save(data)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_bindings.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add server/workflows.py tests/test_workflow_bindings.py
git commit -m "feat(workflows): bind a workflow to a session with a stage pointer"
```

---

### Task 4: YAML export and import

**Files:**
- Modify: `server/workflows.py` (append after `rekey`)
- Modify: `requirements.txt`
- Create: `tests/test_workflow_yaml.py`

**Interfaces:**
- Consumes: `get_workflow`, `create_workflow`, `update_workflow`, `validate` from Tasks 1–3.
- Produces: `to_yaml(wid: str) -> str | None`; `from_yaml(text: str) -> dict` (raises `ValueError`).

- [ ] **Step 1: Add the dependencies**

Append to `requirements.txt`:

```
pyyaml>=6.0
httpx>=0.27
```

`pyyaml` is what this feature needs. `httpx` is what `fastapi.testclient.TestClient` needs: it is missing from both `requirements.txt` and the venv today, so every TestClient test in the repo — the existing `tests/test_pins.py` included — fails at collection with `RuntimeError: The starlette.testclient module requires the httpx package`. Tasks 5 and 6 are TestClient tests, so the suite has to be able to collect them.

Install both: `.venv/bin/pip install "pyyaml>=6.0" "httpx>=0.27"`
Verify:

```bash
.venv/bin/python -c "import yaml, httpx; print(yaml.__version__, httpx.__version__)"
```

Expected: yaml at or above `6.0`, httpx at or above `0.27`.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_workflow_yaml.py`:

```python
"""
YAML round-trip — a workflow you can diff, review, and keep in git.

Import always mints a new id: bringing a file in must never silently
overwrite a workflow you are already running.
"""

import pytest
import yaml

from server import workflows

AGENTS = [
    {"name": "Researcher", "role": "Find prior art", "prompt": "You research."},
    {"name": "Builder", "role": "Write it", "model": "sonnet", "prompt": "You build."},
]
STAGES = [
    {"name": "Discovery", "goal": "look", "mode": "parallel",
     "agent_ids": ["researcher", "builder"], "exit_criteria": "a list"},
    {"name": "Build", "goal": "write", "mode": "solo",
     "agent_ids": ["builder"], "exit_criteria": "green tests"},
]


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "_PATH", str(tmp_path / "workflows.json"))


@pytest.fixture
def wid():
    w = workflows.create_workflow("Feature delivery", "Research then build")["id"]
    workflows.update_workflow(w, agents=AGENTS, stages=STAGES)
    return w


def test_export_shape(wid):
    doc = yaml.safe_load(workflows.to_yaml(wid))
    assert doc["title"] == "Feature delivery"
    assert [a["name"] for a in doc["agents"]] == ["Researcher", "Builder"]
    assert [s["name"] for s in doc["stages"]] == ["Discovery", "Build"]
    # Identity and timestamps belong to this machine, not to the document.
    assert "id" not in doc and "created_at" not in doc and "updated_at" not in doc


def test_export_unknown_workflow():
    assert workflows.to_yaml("deadbeef0000") is None


def test_round_trip_makes_an_equal_copy_with_a_new_id(wid):
    copy = workflows.from_yaml(workflows.to_yaml(wid))
    assert copy["id"] != wid
    original = workflows.get_workflow(wid)
    for field in ("title", "description", "agents", "stages"):
        assert copy[field] == original[field]


def test_import_rejects_non_mapping():
    with pytest.raises(ValueError, match="mapping"):
        workflows.from_yaml("- just\n- a list\n")


def test_import_rejects_broken_yaml():
    with pytest.raises(ValueError, match="YAML"):
        workflows.from_yaml("title: [unclosed\n")


def test_import_rejects_missing_title():
    with pytest.raises(ValueError, match="title"):
        workflows.from_yaml("agents: []\nstages: []\n")


def test_import_rejects_unknown_agent_id():
    text = ("title: W\nagents:\n  - name: Builder\n    prompt: b\n"
            "stages:\n  - name: X\n    mode: solo\n    agent_ids: [ghost]\n")
    with pytest.raises(ValueError, match="unknown agent"):
        workflows.from_yaml(text)


def test_import_rejects_bad_mode():
    text = ("title: W\nagents:\n  - name: Builder\n    prompt: b\n"
            "stages:\n  - name: X\n    mode: swarm\n    agent_ids: [builder]\n")
    with pytest.raises(ValueError, match="mode"):
        workflows.from_yaml(text)


def test_import_without_ids_mints_them():
    text = ("title: W\nagents:\n  - name: Builder\n    prompt: b\n"
            "stages:\n  - name: X\n    mode: solo\n    agent_ids: [builder]\n")
    wf = workflows.from_yaml(text)
    assert wf["agents"][0]["id"] == "builder"
    assert wf["stages"][0]["id"] == "s1"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_workflow_yaml.py -v`
Expected: FAIL — `AttributeError: module 'server.workflows' has no attribute 'to_yaml'`

- [ ] **Step 4: Implement export and import**

Add `import yaml` to the imports at the top of `server/workflows.py`, then append:

```python
# ---------------------------------------------------------------------------
# YAML — a workflow as a file you can diff, review, and keep in git.
#
# Long system prompts are the bulk of a workflow, and JSON string escaping
# makes them unreadable; YAML block scalars keep them legible.
# ---------------------------------------------------------------------------

# Fields that describe *this install's* copy, not the procedure itself. They
# are stripped on export so two exports of the same procedure diff clean.
_LOCAL_FIELDS = ("id", "created_at", "updated_at")


def to_yaml(wid: str) -> str | None:
    wf = get_workflow(wid)
    if wf is None:
        return None
    doc = {k: v for k, v in wf.items() if k not in _LOCAL_FIELDS}
    ordered = {
        "title": doc.get("title", ""),
        "description": doc.get("description", ""),
        "agents": doc.get("agents", []),
        "stages": doc.get("stages", []),
    }
    return yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True,
                          default_flow_style=False, width=100)


def from_yaml(text: str) -> dict:
    """Create a NEW workflow from YAML. Never overwrites an existing one."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"could not parse YAML: {e}") from e
    if not isinstance(doc, dict):
        raise ValueError("a workflow file must be a mapping with a title")
    title = str(doc.get("title") or "").strip()
    if not title:
        raise ValueError("a workflow file needs a title")
    agents = doc.get("agents") or []
    stages = doc.get("stages") or []
    if not isinstance(agents, list) or not isinstance(stages, list):
        raise ValueError("agents and stages must be lists")
    # Validate before creating, so a bad file leaves nothing behind.
    validate(agents, stages)
    wf = create_workflow(title, str(doc.get("description") or ""))
    return update_workflow(wf["id"], agents=agents, stages=stages)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_yaml.py -v`
Expected: PASS (9 tests)

- [ ] **Step 6: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: everything passes, no new failures.

- [ ] **Step 7: Commit**

```bash
git add server/workflows.py tests/test_workflow_yaml.py requirements.txt
git commit -m "feat(workflows): YAML export and import"
```

---

### Task 5: Workflow CRUD endpoints

**Files:**
- Modify: `server/app.py` (imports at line 26-28; new route block before the `@app.get("/")` page routes near line 1548)
- Create: `tests/test_workflow_api.py`

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces: routes `GET/POST /api/workflows`, `GET/PUT/DELETE /api/workflows/{wid}`, `GET /api/workflows/{wid}/export`, `POST /api/workflows/import`, `POST /api/workflows/{wid}/preview`; Pydantic models `WorkflowBody`, `WorkflowDocBody`, `ImportBody`, `PreviewBody`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_workflow_api.py`:

```python
"""
Workflow CRUD over HTTP. Validation is the store's; this file checks that the
right ValueError becomes the right status code.
"""

import pytest
from fastapi.testclient import TestClient

from server import workflows
from server.app import app

AGENTS = [{"name": "Builder", "role": "Write it", "prompt": "You build."}]
STAGES = [{"name": "Build", "mode": "solo", "agent_ids": ["builder"], "goal": "ship"}]


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "_PATH", str(tmp_path / "workflows.json"))


@pytest.fixture
def client():
    return TestClient(app)


def test_create_and_list(client):
    r = client.post("/api/workflows", json={"title": "Feature delivery"})
    assert r.status_code == 200
    wid = r.json()["id"]
    rows = client.get("/api/workflows").json()["workflows"]
    assert [w["id"] for w in rows] == [wid]
    assert rows[0]["agent_count"] == 0


def test_create_requires_a_title(client):
    assert client.post("/api/workflows", json={"title": "  "}).status_code == 400


def test_get_full_document(client):
    wid = client.post("/api/workflows", json={"title": "W"}).json()["id"]
    body = {"title": "W", "description": "d", "agents": AGENTS, "stages": STAGES}
    assert client.put(f"/api/workflows/{wid}", json=body).status_code == 200
    doc = client.get(f"/api/workflows/{wid}").json()
    assert doc["agents"][0]["id"] == "builder"
    assert doc["stages"][0]["id"] == "s1"


def test_get_unknown_is_404(client):
    assert client.get("/api/workflows/deadbeef0000").status_code == 404


def test_put_unknown_is_404(client):
    r = client.put("/api/workflows/deadbeef0000",
                   json={"title": "W", "agents": [], "stages": []})
    assert r.status_code == 404


def test_put_validation_is_400(client):
    wid = client.post("/api/workflows", json={"title": "W"}).json()["id"]
    bad = {"title": "W", "agents": AGENTS,
           "stages": [{"name": "X", "mode": "swarm", "agent_ids": ["builder"]}]}
    r = client.put(f"/api/workflows/{wid}", json=bad)
    assert r.status_code == 400 and "mode" in r.json()["detail"]


def test_delete(client):
    wid = client.post("/api/workflows", json={"title": "W"}).json()["id"]
    assert client.delete(f"/api/workflows/{wid}").status_code == 200
    assert client.delete(f"/api/workflows/{wid}").status_code == 404


def test_export_then_import(client):
    wid = client.post("/api/workflows", json={"title": "W"}).json()["id"]
    client.put(f"/api/workflows/{wid}",
               json={"title": "W", "agents": AGENTS, "stages": STAGES})
    text = client.get(f"/api/workflows/{wid}/export").text
    assert "Builder" in text
    r = client.post("/api/workflows/import", json={"yaml": text})
    assert r.status_code == 200
    assert r.json()["id"] != wid
    assert len(client.get("/api/workflows").json()["workflows"]) == 2


def test_export_unknown_is_404(client):
    assert client.get("/api/workflows/deadbeef0000/export").status_code == 404


def test_import_bad_yaml_is_400(client):
    r = client.post("/api/workflows/import", json={"yaml": "- a\n- b\n"})
    assert r.status_code == 400 and "mapping" in r.json()["detail"]


def test_preview(client):
    wid = client.post("/api/workflows", json={"title": "W"}).json()["id"]
    client.put(f"/api/workflows/{wid}",
               json={"title": "W", "agents": AGENTS, "stages": STAGES})
    r = client.post(f"/api/workflows/{wid}/preview", json={"stage_index": 0})
    assert r.status_code == 200
    assert "Coordination: solo. Builder runs this stage alone." in r.json()["prompt"]


def test_preview_out_of_range_is_400(client):
    wid = client.post("/api/workflows", json={"title": "W"}).json()["id"]
    r = client.post(f"/api/workflows/{wid}/preview", json={"stage_index": 3})
    assert r.status_code == 400
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_workflow_api.py -v`
Expected: FAIL — every request returns 404 (routes do not exist).

- [ ] **Step 3: Add the routes**

In `server/app.py`, add `workflows` to the package import list (keep it alphabetical):

```python
from . import (agyparser, archives, attention, autonomy, descriptions,
               grokparser, models, ollamausage, opencodeparser, overrides,
               parser, pins, projects, registry, runner, slackbot,
               summaries, summarizer, tasks, tmuxio, workflows)
```

Insert this block immediately before the `@app.get("/")` page route (`server/app.py:1550`), so every `/api/...` route stays above the HTML page routes:

```python
# ---------------------------------------------------------------------------
# Workflows — reusable multi-agent blueprints (see server/workflows.py).
# ---------------------------------------------------------------------------
class WorkflowBody(BaseModel):
    title: str = ""
    description: str = ""


class WorkflowDocBody(BaseModel):
    title: str | None = None
    description: str | None = None
    agents: list[dict] | None = None
    stages: list[dict] | None = None


class ImportBody(BaseModel):
    yaml: str = ""


class PreviewBody(BaseModel):
    stage_index: int = 0


@app.get("/api/workflows")
def api_workflows():
    """All workflows with counts (list page + the assign picker)."""
    return {"workflows": workflows.list_workflows()}


@app.post("/api/workflows")
def api_create_workflow(body: WorkflowBody):
    if not body.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    return workflows.create_workflow(body.title, body.description)


@app.get("/api/workflows/{wid}")
def api_workflow(wid: str):
    wf = workflows.get_workflow(wid)
    if wf is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    return wf


@app.put("/api/workflows/{wid}")
def api_update_workflow(wid: str, body: WorkflowDocBody):
    """One PUT replaces the whole document — the editor saves on a button, so
    there is no partial-update case to serve."""
    try:
        wf = workflows.update_workflow(wid, title=body.title,
                                       description=body.description,
                                       agents=body.agents, stages=body.stages)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if wf is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    return wf


@app.delete("/api/workflows/{wid}")
def api_delete_workflow(wid: str):
    if not workflows.delete_workflow(wid):
        raise HTTPException(status_code=404, detail="workflow not found")
    return {"id": wid, "deleted": True}


@app.get("/api/workflows/{wid}/export")
def api_export_workflow(wid: str):
    text = workflows.to_yaml(wid)
    if text is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    return PlainTextResponse(text, media_type="text/yaml")


@app.post("/api/workflows/import")
def api_import_workflow(body: ImportBody):
    try:
        return workflows.from_yaml(body.yaml)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/workflows/{wid}/preview")
def api_preview_stage(wid: str, body: PreviewBody):
    """Exactly what a send would type — shown before anything is sent."""
    if workflows.get_workflow(wid) is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    try:
        return {"prompt": workflows.compose_stage(wid, body.stage_index)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
```

Add `PlainTextResponse` to the responses import at the top of the file:

```python
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_api.py -v`
Expected: PASS (12 tests)

- [ ] **Step 5: Commit**

```bash
git add server/app.py tests/test_workflow_api.py
git commit -m "feat(workflows): CRUD, YAML, and preview endpoints"
```

---

### Task 6: Session binding endpoints and send

**Files:**
- Modify: `server/app.py` (route block from Task 5; `api_reset` rekey loop at ~line 1372; `api_sessions` at ~line 168)
- Create: `tests/test_workflow_session_api.py`

**Interfaces:**
- Consumes: Task 3 bindings, Task 2 `compose_stage`, `tmuxio.say`, `_session_exists` (already in `app.py`).
- Produces: routes `GET/POST/DELETE /api/sessions/{sid}/workflow`, `POST /api/sessions/{sid}/workflow/send`, `POST /api/sessions/{sid}/workflow/advance`; Pydantic models `AssignBody`, `AdvanceBody`. `/api/sessions` rows gain a `workflow` key (`null` or `{workflow_id, title, stage_index, stage_count, stage_name}`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_workflow_session_api.py`:

```python
"""
Binding a workflow to a session over HTTP, and sending a stage into it.

The send path is monkeypatched at tmuxio.say: no REPL is started and no
tokens are spent. What is checked is that the composed prompt is what
reaches say, and that a dead pane reports 409.
"""

import pytest
from fastapi.testclient import TestClient

from server import app as app_module
from server import tmuxio, workflows
from server.app import app

SID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

AGENTS = [{"name": "Builder", "role": "Write it", "prompt": "You build."}]
STAGES = [
    {"name": "Build", "mode": "solo", "agent_ids": ["builder"], "goal": "ship"},
    {"name": "Review", "mode": "solo", "agent_ids": ["builder"], "goal": "check"},
]


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "_PATH", str(tmp_path / "workflows.json"))
    # Every session id in this file is made up, so the existence check has to
    # be stubbed — the alternative is depending on the operator's transcripts.
    monkeypatch.setattr(app_module, "_session_exists", lambda sid: sid == SID)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def wid():
    w = workflows.create_workflow("Feature delivery")["id"]
    workflows.update_workflow(w, agents=AGENTS, stages=STAGES)
    return w


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(tmuxio, "say",
                        lambda sid, text, **kw: calls.append((sid, text)) or {"ok": True})
    return calls


def test_unbound_session(client):
    r = client.get(f"/api/sessions/{SID}/workflow")
    assert r.status_code == 200 and r.json() == {"bound": False}


def test_assign_returns_the_composed_stage(client, wid):
    r = client.post(f"/api/sessions/{SID}/workflow", json={"workflow_id": wid})
    assert r.status_code == 200
    body = r.json()
    assert body["bound"] is True
    assert body["stage_index"] == 0 and body["stage_count"] == 2
    assert body["stage_name"] == "Build"
    assert "Coordination: solo. Builder runs this stage alone." in body["prompt"]
    assert body["sent"] is False


def test_assign_unknown_workflow_is_404(client):
    r = client.post(f"/api/sessions/{SID}/workflow", json={"workflow_id": "deadbeef0000"})
    assert r.status_code == 404


def test_assign_unknown_session_is_404(client, wid):
    r = client.post("/api/sessions/nope/workflow", json={"workflow_id": wid})
    assert r.status_code == 404


def test_send_types_the_composed_prompt(client, wid, sent):
    client.post(f"/api/sessions/{SID}/workflow", json={"workflow_id": wid})
    r = client.post(f"/api/sessions/{SID}/workflow/send")
    assert r.status_code == 200
    assert len(sent) == 1
    to, text = sent[0]
    assert to == SID
    assert "## Stage 1/2: Build" in text
    # Sending is recorded, so the button can offer a re-send next time.
    assert client.get(f"/api/sessions/{SID}/workflow").json()["sent"] is True


def test_send_with_no_live_pane_is_409(client, wid, monkeypatch):
    monkeypatch.setattr(tmuxio, "say",
                        lambda sid, text, **kw: {"ok": False, "error": "no live tmux session"})
    client.post(f"/api/sessions/{SID}/workflow", json={"workflow_id": wid})
    r = client.post(f"/api/sessions/{SID}/workflow/send")
    assert r.status_code == 409 and "no live tmux session" in r.json()["detail"]


def test_send_without_a_binding_is_409(client, sent):
    r = client.post(f"/api/sessions/{SID}/workflow/send")
    assert r.status_code == 409
    assert sent == []


def test_advance_and_clamp(client, wid):
    client.post(f"/api/sessions/{SID}/workflow", json={"workflow_id": wid})
    r = client.post(f"/api/sessions/{SID}/workflow/advance", json={"delta": 1})
    assert r.json()["stage_index"] == 1 and r.json()["stage_name"] == "Review"
    r = client.post(f"/api/sessions/{SID}/workflow/advance", json={"delta": 1})
    assert r.json()["stage_index"] == 1
    # The freshly-advanced stage has not been sent yet.
    assert r.json()["sent"] is False


def test_advance_without_a_binding_is_404(client):
    r = client.post(f"/api/sessions/{SID}/workflow/advance", json={"delta": 1})
    assert r.status_code == 404


def test_unassign(client, wid):
    client.post(f"/api/sessions/{SID}/workflow", json={"workflow_id": wid})
    assert client.delete(f"/api/sessions/{SID}/workflow").status_code == 200
    assert client.get(f"/api/sessions/{SID}/workflow").json() == {"bound": False}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_workflow_session_api.py -v`
Expected: FAIL — the `/api/sessions/{sid}/workflow` routes return 404.

- [ ] **Step 3: Add the routes**

Append to the workflow route block in `server/app.py`:

```python
class AssignBody(BaseModel):
    workflow_id: str


class AdvanceBody(BaseModel):
    delta: int = 1


def _binding_view(session_id: str) -> dict:
    """Binding plus the prompt the current stage would send. One call, because
    the panel never wants one without the other."""
    b = workflows.get_binding(session_id)
    if b is None:
        return {"bound": False}
    out = dict(b)
    out["bound"] = True
    stage_id = ""
    wf = workflows.get_workflow(b["workflow_id"])
    stages = (wf or {}).get("stages", [])
    if stages:
        stage_id = stages[b["stage_index"]]["id"]
        out["prompt"] = workflows.compose_stage(b["workflow_id"], b["stage_index"])
    else:
        out["prompt"] = ""
    out["stage_id"] = stage_id
    out["sent"] = stage_id in b.get("sent", [])
    return out


@app.get("/api/sessions/{session_id}/workflow")
def api_session_workflow(session_id: str):
    return _binding_view(session_id)


@app.post("/api/sessions/{session_id}/workflow")
def api_assign_workflow(session_id: str, body: AssignBody):
    if not _session_exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    if workflows.bind(session_id, body.workflow_id) is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    return _binding_view(session_id)


@app.delete("/api/sessions/{session_id}/workflow")
def api_unassign_workflow(session_id: str):
    workflows.unbind(session_id)
    return {"session_id": session_id, "bound": False}


@app.post("/api/sessions/{session_id}/workflow/send")
def api_send_stage(session_id: str):
    """Type the current stage's composed prompt into the live REPL.

    Sending is always a button press: assigning a workflow costs nothing, and
    the operator decides when a stage actually runs.
    """
    view = _binding_view(session_id)
    if not view.get("bound"):
        raise HTTPException(status_code=409, detail="no workflow assigned")
    if not view.get("prompt"):
        raise HTTPException(status_code=409, detail="this workflow has no stages")
    result = tmuxio.say(session_id, view["prompt"])
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "send failed"))
    workflows.mark_sent(session_id, view["stage_id"])
    return _binding_view(session_id)


@app.post("/api/sessions/{session_id}/workflow/advance")
def api_advance_stage(session_id: str, body: AdvanceBody):
    if workflows.advance(session_id, body.delta) is None:
        raise HTTPException(status_code=404, detail="no workflow assigned")
    return _binding_view(session_id)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_session_api.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Carry the binding through a /clear**

In `api_reset` in `server/app.py`, add `workflows` to the rekey loop:

```python
        for store in (overrides, descriptions, tasks, pins, projects, autonomy,
                      attention, workflows):
            store.rekey(session_id, new_id)
```

- [ ] **Step 6: Put the binding on every listed session**

Two endpoints decorate session rows for a card view: `api_sessions` (`server/app.py:165`, the board) and `api_triage` (`server/app.py:1401`, the to-do rail and triage cards). Both already read `proj_map` and `task_counts` once and then set `s["projects"]` / `s["task_count"]` in four loops each — the claude loop plus the agy, grok, and opencode merge loops.

In **both** functions, next to `task_counts = tasks.counts_by_session()`, add:

```python
    wf_map = workflows.bindings_by_session()
```

Then in each of that function's four loops, beside `s["task_count"] = ...`, add the same line keyed off whatever id that loop already uses — `sid` in the first loop, `s["session_id"]` in the three merge loops:

```python
        s["workflow"] = wf_map.get(sid)
```

```python
        s["workflow"] = wf_map.get(s["session_id"])
```

`api_search` (`server/app.py:366`) is deliberately left alone: search results are a lookup list, not a status view, and it has no badge row to hang this on.

- [ ] **Step 7: Add the decoration test**

Append to `tests/test_workflow_session_api.py`:

```python
def test_sessions_list_carries_the_binding(client, wid, monkeypatch):
    """The board badge reads this field, so it must survive the decorate pass."""
    workflows.bind(SID, wid)
    rows = client.get("/api/sessions?limit=all&archived=all").json()["sessions"]
    for row in rows:
        assert "workflow" in row
        if row["session_id"] == SID:
            assert row["workflow"]["title"] == "Feature delivery"
```

- [ ] **Step 8: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: everything passes.

- [ ] **Step 9: Commit**

```bash
git add server/app.py tests/test_workflow_session_api.py
git commit -m "feat(workflows): assign, send, and advance a workflow on a session"
```

---

### Task 7: Workflows list page

**Files:**
- Create: `server/static/workflows.html`
- Modify: `server/app.py` (page routes, after `projects_page`)
- Modify: `server/static/app.js` (`NAV_PILLS` at ~line 232-240; append a `Workflows` controller at the end of the file)
- Modify: `server/static/style.css` (append)
- Modify: `server/static/*.html` (bump every `?v=168` to `?v=169`)

**Interfaces:**
- Consumes: `GET/POST/DELETE /api/workflows`, `POST /api/workflows/import`, `GET /api/workflows/{wid}/export` from Task 5; existing helpers `getJSON`, `esc`, `renderTopNav`.
- Produces: global `Workflows` object with `init()`, `tick()`, `renderList()`, `openEditor(w)`, `remove(wid, title)`, `openImport()`, `exportOne(wid)`.

- [ ] **Step 1: Serve the page**

In `server/app.py`, after `projects_page`:

```python
@app.get("/workflows.html")
def workflows_page():
    return FileResponse(os.path.join(STATIC_DIR, "workflows.html"))
```

- [ ] **Step 2: Create the page**

Create `server/static/workflows.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Workflows · Agent OS</title>
  <link rel="icon" href="/static/favicon.svg?v=1" type="image/svg+xml" />
  <link rel="stylesheet" href="/static/style.css?v=169" />
</head>
<body>
  <header class="topbar">
    <nav id="topnav" class="topnav"></nav>
    <span class="spacer"></span>
    <button class="toggle" id="importWfBtn" title="Create a workflow from a YAML file">⬆ Import YAML</button>
    <button class="toggle" id="newWorkflowBtn" title="Create a new workflow">✨ New workflow</button>
    <span class="meta" id="meta"></span>
  </header>
  <div id="view"></div>
  <script src="/static/app.js?v=169"></script>
  <script>renderTopNav("workflows"); Workflows.init();</script>
</body>
</html>
```

- [ ] **Step 3: Add the nav pill**

In `server/static/app.js`, add to `NAV_PILLS` immediately after the Projects entry:

```javascript
  { href: "/workflows.html", key: "workflows", label: "🧩 Workflows" },
```

- [ ] **Step 4: Add the list controller**

Append to `server/static/app.js`:

```javascript
// ---- Workflows (reusable multi-agent blueprints) -----------------------------
//
// Two views on one page, like Projects: a grid of workflow cards, and the
// editor for one workflow (?id=<wid>). The editor never polls — a background
// refresh mid-edit would throw away typing.

const Workflows = {
  wid: null,

  init() {
    this.wid = new URLSearchParams(location.search).get("id");
    const nb = document.getElementById("newWorkflowBtn");
    if (nb) nb.addEventListener("click", () => this.create());
    const ib = document.getElementById("importWfBtn");
    if (ib) ib.addEventListener("click", () => this.openImport());
    this.tick();
  },

  async tick() {
    try {
      if (this.wid) await this.renderEditor();
      else await this.renderList();
    } catch (e) {
      document.getElementById("meta").textContent = "error: " + e.message;
    }
  },

  async renderList() {
    const data = await getJSON("/api/workflows");
    const view = document.getElementById("view");
    const n = data.workflows.length;
    document.getElementById("meta").textContent = `${n} workflow${n === 1 ? "" : "s"}`;
    if (!n) {
      view.innerHTML = `<div class="empty">No workflows yet. Click “✨ New workflow” to define a roster of agents and the stages they work through, then assign it to a session from that session's page.</div>`;
      return;
    }
    view.innerHTML = `<div class="proj-grid">` + data.workflows.map((w) => `
      <div class="card proj-card">
        <a class="title" href="/workflows.html?id=${encodeURIComponent(w.id)}">${esc(w.title)}</a>
        <div class="proj-desc">${esc(w.description || "")}</div>
        <div class="row">
          <span class="badge"><b>${w.agent_count}</b> agent${w.agent_count === 1 ? "" : "s"}</span>
          <span class="badge"><b>${w.stage_count}</b> stage${w.stage_count === 1 ? "" : "s"}</span>
          <span class="badge"><b>${w.session_count}</b> session${w.session_count === 1 ? "" : "s"}</span>
        </div>
        <div class="tri-actions">
          <a class="tri-relay" href="/workflows.html?id=${encodeURIComponent(w.id)}">✎ Edit</a>
          <button class="tri-relay" data-export="${w.id}">⬇ Export</button>
          <button class="tri-attn" data-del="${w.id}" data-title="${esc(w.title)}">🗑 Delete</button>
        </div>
      </div>`).join("") + `</div>`;
    view.querySelectorAll("[data-export]").forEach((b) =>
      b.addEventListener("click", () => this.exportOne(b.dataset.export)));
    view.querySelectorAll("[data-del]").forEach((b) =>
      b.addEventListener("click", () => this.remove(b.dataset.del, b.dataset.title)));
  },

  async create() {
    const title = prompt("Name this workflow (e.g. Feature delivery)");
    if (!title || !title.trim()) return;
    try {
      const r = await fetch("/api/workflows", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: title.trim(), description: "" }),
      });
      if (!r.ok) throw new Error((await r.json()).detail || ("HTTP " + r.status));
      const w = await r.json();
      location.href = `/workflows.html?id=${encodeURIComponent(w.id)}`;
    } catch (e) { alert("Could not create: " + e.message); }
  },

  async remove(wid, title) {
    if (!confirm(`Delete “${title}”? Any session running it is unassigned. Transcripts are untouched.`)) return;
    try {
      const r = await fetch(`/api/workflows/${encodeURIComponent(wid)}`, { method: "DELETE" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      if (this.wid === wid) location.href = "/workflows.html";
      else this.tick();
    } catch (e) { alert("Could not delete: " + e.message); }
  },

  // Export shows the YAML in a modal rather than downloading it: this page is
  // often open next to a terminal, and select-and-copy is the shortest path
  // from here to a file in a repo.
  async exportOne(wid) {
    let text = "";
    try {
      const r = await fetch(`/api/workflows/${encodeURIComponent(wid)}/export`);
      if (!r.ok) throw new Error("HTTP " + r.status);
      text = await r.text();
    } catch (e) { alert("Export failed: " + e.message); return; }
    this.modal("⬇ Export workflow", `
      <textarea id="wfYamlOut" class="modal-input mono" rows="18" readonly>${esc(text)}</textarea>
      <div class="modal-actions">
        <button id="wfYamlCopy" class="hdr-btn">Copy</button>
        <button id="wfYamlClose" class="hdr-btn primary">Close</button>
      </div>`);
    document.getElementById("wfYamlCopy").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(text); } catch (e) { /* selection still works */ }
      document.getElementById("wfYamlCopy").textContent = "Copied";
    });
    document.getElementById("wfYamlClose").addEventListener("click", () => this.closeModal());
  },

  openImport() {
    this.modal("⬆ Import workflow", `
      <label class="modal-label">Paste a workflow YAML file</label>
      <textarea id="wfYamlIn" class="modal-input mono" rows="16" placeholder="title: Feature delivery&#10;agents:&#10;  - name: Researcher&#10;    prompt: You research.&#10;stages:&#10;  - name: Discovery&#10;    mode: parallel&#10;    agent_ids: [researcher]"></textarea>
      <div class="modal-status" id="wfImportStatus"></div>
      <div class="modal-actions">
        <button id="wfImportCancel" class="hdr-btn">Cancel</button>
        <button id="wfImportGo" class="hdr-btn primary">Import</button>
      </div>`);
    document.getElementById("wfImportCancel").addEventListener("click", () => this.closeModal());
    document.getElementById("wfImportGo").addEventListener("click", async () => {
      const status = document.getElementById("wfImportStatus");
      status.textContent = "Importing…";
      try {
        const r = await fetch("/api/workflows/import", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ yaml: document.getElementById("wfYamlIn").value }),
        });
        if (!r.ok) throw new Error((await r.json()).detail || ("HTTP " + r.status));
        const w = await r.json();
        location.href = `/workflows.html?id=${encodeURIComponent(w.id)}`;
      } catch (e) { status.textContent = "Failed: " + e.message; }
    });
  },

  modal(head, inner) {
    this.closeModal();
    const el = document.createElement("div");
    el.id = "wfModal";
    el.className = "modal-backdrop";
    el.innerHTML = `<div class="modal"><div class="modal-head">${head}</div>${inner}</div>`;
    document.body.appendChild(el);
    el.addEventListener("click", (e) => { if (e.target === el) this.closeModal(); });
  },

  closeModal() {
    const el = document.getElementById("wfModal");
    if (el) el.remove();
  },
};
```

- [ ] **Step 5: Add the styles**

Append to `server/static/style.css`:

```css
/* ---- Workflows -------------------------------------------------------- */
.modal-input.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
```

- [ ] **Step 6: Bump the cache-busting version**

Run: `sed -i '' 's/?v=168/?v=169/g' server/static/*.html`
Verify: `grep -c "v=169" server/static/*.html`
Expected: every page reports at least 2 (stylesheet + script).

- [ ] **Step 7: Verify by hand**

Start the server (`./serve.sh`), open `http://localhost:8765/workflows.html`. Check: the 🧩 Workflows pill is in the nav and marked current; "✨ New workflow" creates one and lands on `?id=`; the empty state reads correctly; Export shows YAML; Import of that same YAML creates a second workflow; Delete removes it.

- [ ] **Step 8: Commit**

```bash
git add server/app.py server/static/workflows.html server/static/app.js server/static/style.css server/static/*.html
git commit -m "feat(workflows): workflows list page with YAML import and export"
```

---

### Task 8: Workflow editor — agents and stages

**Files:**
- Modify: `server/static/app.js` (`Workflows` controller from Task 7)
- Modify: `server/static/style.css` (append)

**Interfaces:**
- Consumes: `GET /api/workflows/{wid}`, `PUT /api/workflows/{wid}`, `POST /api/workflows/{wid}/preview`; the `Workflows.modal`/`closeModal` helpers from Task 7.
- Produces: `Workflows.renderEditor()`, `Workflows.doc` (the in-memory document being edited), `Workflows.collect()`, `Workflows.save()`, `Workflows.preview(index)`.

- [ ] **Step 1: Add the editor to the controller**

Insert these methods into the `Workflows` object in `server/static/app.js`, after `renderList`:

```javascript
  // ---- editor -------------------------------------------------------------
  //
  // The document is held in memory and written back with one PUT on Save.
  // Every field edit updates `this.doc` on input, so adding a stage never
  // discards prompt text you have already typed.
  doc: null,

  async renderEditor() {
    this.doc = await getJSON(`/api/workflows/${encodeURIComponent(this.wid)}`);
    this.paintEditor();
  },

  paintEditor() {
    const d = this.doc;
    document.getElementById("meta").textContent =
      `${d.agents.length} agent${d.agents.length === 1 ? "" : "s"} · ${d.stages.length} stage${d.stages.length === 1 ? "" : "s"}`;
    document.getElementById("view").innerHTML = `
      <div class="wf-editor">
        <div class="proj-head">
          <input id="wfTitle" class="wf-title-input" value="${esc(d.title)}" placeholder="Workflow name" />
          <textarea id="wfDesc" class="modal-input" rows="2" placeholder="What this workflow is for (optional)">${esc(d.description || "")}</textarea>
          <div class="tri-actions">
            <button class="hdr-btn primary" id="wfSave">💾 Save</button>
            <a class="hdr-btn" href="/workflows.html">← All workflows</a>
            <button class="hdr-btn" data-del="${d.id}" data-title="${esc(d.title)}">🗑 Delete</button>
          </div>
          <div class="modal-status" id="wfStatus"></div>
        </div>

        <h3 class="wf-section">Agents <button class="hdr-btn" id="wfAddAgent">＋ Agent</button></h3>
        <div class="wf-agents">${d.agents.map((a, i) => this.agentCard(a, i)).join("") ||
          `<div class="empty">No agents yet. An agent is a role: what it is responsible for, and the system prompt that makes it behave that way.</div>`}</div>

        <h3 class="wf-section">Stages <button class="hdr-btn" id="wfAddStage">＋ Stage</button></h3>
        <div class="wf-stages">${d.stages.map((s, i) => this.stageCard(s, i)).join("") ||
          `<div class="empty">No stages yet. A stage is one step of the procedure: which agents take part, how they work together, and how you know it is done.</div>`}</div>
      </div>`;
    this.wireEditor();
  },

  agentCard(a, i) {
    return `
      <div class="card wf-agent" data-agent="${i}">
        <div class="wf-row">
          <input class="wf-input" data-af="name" data-i="${i}" value="${esc(a.name)}" placeholder="Name, e.g. Researcher" />
          <input class="wf-input" data-af="model" data-i="${i}" value="${esc(a.model || "")}" placeholder="Model, e.g. opus" />
          <button class="tri-attn" data-rm-agent="${i}" title="Remove this agent">✕</button>
        </div>
        <input class="wf-input wide" data-af="role" data-i="${i}" value="${esc(a.role || "")}" placeholder="Responsibility — one line" />
        <textarea class="wf-input wide mono" rows="6" data-af="prompt" data-i="${i}" placeholder="System prompt / agent.md body">${esc(a.prompt || "")}</textarea>
      </div>`;
  },

  stageCard(s, i) {
    const modes = ["coordinator", "handoff", "parallel", "solo"];
    const opts = modes.map((m) =>
      `<option value="${m}"${s.mode === m ? " selected" : ""}>${m}</option>`).join("");
    const picks = this.doc.agents.map((a) => `
      <label class="wf-pick">
        <input type="checkbox" data-stage-agent="${i}" value="${esc(a.id)}"${(s.agent_ids || []).includes(a.id) ? " checked" : ""} />
        ${esc(a.name)}
      </label>`).join("") || `<span class="muted">Add an agent first.</span>`;
    return `
      <div class="card wf-stage" data-stage="${i}">
        <div class="wf-row">
          <span class="badge">${i + 1}</span>
          <input class="wf-input" data-sf="name" data-i="${i}" value="${esc(s.name)}" placeholder="Stage name, e.g. Discovery" />
          <select class="wf-input" data-sf="mode" data-i="${i}" title="How the agents in this stage work together">${opts}</select>
          <button class="tri-relay" data-up="${i}"${i === 0 ? " disabled" : ""} title="Move earlier">↑</button>
          <button class="tri-relay" data-down="${i}"${i === this.doc.stages.length - 1 ? " disabled" : ""} title="Move later">↓</button>
          <button class="tri-attn" data-rm-stage="${i}" title="Remove this stage">✕</button>
        </div>
        <input class="wf-input wide" data-sf="goal" data-i="${i}" value="${esc(s.goal || "")}" placeholder="Goal of this stage" />
        <input class="wf-input wide" data-sf="exit_criteria" data-i="${i}" value="${esc(s.exit_criteria || "")}" placeholder="Exit criteria — how you know it is done" />
        <div class="wf-picks">${picks}</div>
        <button class="tri-relay" data-preview="${i}">👁 Preview prompt</button>
      </div>`;
  },

  wireEditor() {
    const d = this.doc;
    document.getElementById("wfTitle").addEventListener("input", (e) => { d.title = e.target.value; });
    document.getElementById("wfDesc").addEventListener("input", (e) => { d.description = e.target.value; });
    document.getElementById("wfSave").addEventListener("click", () => this.save());
    document.querySelector("[data-del]").addEventListener("click", (e) =>
      this.remove(e.target.dataset.del, e.target.dataset.title));

    document.getElementById("wfAddAgent").addEventListener("click", () => {
      d.agents.push({ id: "", name: "", role: "", model: "opus", prompt: "" });
      this.paintEditor();
    });
    document.getElementById("wfAddStage").addEventListener("click", () => {
      d.stages.push({ name: "", goal: "", mode: "solo", agent_ids: [], exit_criteria: "" });
      this.paintEditor();
    });

    // Field edits write straight into this.doc, so a repaint never loses them.
    document.querySelectorAll("[data-af]").forEach((el) =>
      el.addEventListener("input", () => { d.agents[+el.dataset.i][el.dataset.af] = el.value; }));
    document.querySelectorAll("[data-sf]").forEach((el) =>
      el.addEventListener("input", () => { d.stages[+el.dataset.i][el.dataset.sf] = el.value; }));
    document.querySelectorAll("[data-sf=mode]").forEach((el) =>
      el.addEventListener("change", () => { d.stages[+el.dataset.i].mode = el.value; }));
    document.querySelectorAll("[data-stage-agent]").forEach((cb) =>
      cb.addEventListener("change", () => {
        const stage = d.stages[+cb.dataset.stageAgent];
        const ids = new Set(stage.agent_ids || []);
        if (cb.checked) ids.add(cb.value); else ids.delete(cb.value);
        // Keep roster order, so hand-off chains read the way the list reads.
        stage.agent_ids = d.agents.map((a) => a.id).filter((id) => ids.has(id));
      }));

    document.querySelectorAll("[data-rm-agent]").forEach((b) =>
      b.addEventListener("click", () => {
        const a = d.agents[+b.dataset.rmAgent];
        if (!confirm(`Remove ${a.name || "this agent"}? It is dropped from every stage that uses it.`)) return;
        d.agents.splice(+b.dataset.rmAgent, 1);
        d.stages.forEach((s) => { s.agent_ids = (s.agent_ids || []).filter((id) => id !== a.id); });
        this.paintEditor();
      }));
    document.querySelectorAll("[data-rm-stage]").forEach((b) =>
      b.addEventListener("click", () => { d.stages.splice(+b.dataset.rmStage, 1); this.paintEditor(); }));
    document.querySelectorAll("[data-up]").forEach((b) =>
      b.addEventListener("click", () => this.move(+b.dataset.up, -1)));
    document.querySelectorAll("[data-down]").forEach((b) =>
      b.addEventListener("click", () => this.move(+b.dataset.down, 1)));
    document.querySelectorAll("[data-preview]").forEach((b) =>
      b.addEventListener("click", () => this.preview(+b.dataset.preview)));
  },

  move(i, delta) {
    const j = i + delta;
    const s = this.doc.stages;
    if (j < 0 || j >= s.length) return;
    [s[i], s[j]] = [s[j], s[i]];
    this.paintEditor();
  },

  async save() {
    const status = document.getElementById("wfStatus");
    status.textContent = "Saving…";
    try {
      const r = await fetch(`/api/workflows/${encodeURIComponent(this.wid)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: this.doc.title,
          description: this.doc.description,
          agents: this.doc.agents,
          stages: this.doc.stages,
        }),
      });
      if (!r.ok) throw new Error((await r.json()).detail || ("HTTP " + r.status));
      this.doc = await r.json();       // ids the server minted come back here
      this.paintEditor();
      document.getElementById("wfStatus").textContent = "Saved.";
    } catch (e) { status.textContent = "Failed: " + e.message; }
  },

  // Preview reads the SAVED workflow, so it shows what a send would really
  // type — not what is sitting unsaved in the form.
  async preview(index) {
    let text = "";
    try {
      const r = await fetch(`/api/workflows/${encodeURIComponent(this.wid)}/preview`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stage_index: index }),
      });
      if (!r.ok) throw new Error((await r.json()).detail || ("HTTP " + r.status));
      text = (await r.json()).prompt;
    } catch (e) { alert("Preview failed: " + e.message); return; }
    this.modal("👁 Stage prompt (as saved)", `
      <textarea class="modal-input mono" rows="20" readonly>${esc(text)}</textarea>
      <div class="modal-actions">
        <button id="wfPrevClose" class="hdr-btn primary">Close</button>
      </div>`);
    document.getElementById("wfPrevClose").addEventListener("click", () => this.closeModal());
  },
```

- [ ] **Step 2: Add the editor styles**

Append to `server/static/style.css`:

```css
.wf-editor { padding: 12px 16px; display: flex; flex-direction: column; gap: 14px; }
.wf-title-input { font-size: 20px; font-weight: 600; width: 100%; padding: 6px 8px;
  background: transparent; color: var(--text); border: 1px solid transparent; border-radius: 6px; }
.wf-title-input:focus { border-color: var(--accent); outline: none; }
.wf-section { display: flex; align-items: center; gap: 10px; margin: 6px 0 0; font-size: 15px; }
.wf-agents, .wf-stages { display: flex; flex-direction: column; gap: 10px; }
.wf-agent, .wf-stage { display: flex; flex-direction: column; gap: 8px; padding: 10px 12px; }
.wf-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.wf-input { padding: 5px 8px; border-radius: 6px; border: 1px solid var(--border);
  background: var(--bg); color: var(--text); font: inherit; }
.wf-input.wide { width: 100%; }
.wf-input.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.wf-picks { display: flex; flex-wrap: wrap; gap: 10px; }
.wf-pick { display: flex; align-items: center; gap: 4px; font-size: 13px; }
```

- [ ] **Step 3: Verify by hand**

Reload `http://localhost:8765/workflows.html?id=<wid>`. Check: adding an agent, typing a name and prompt, then adding a second agent does not clear the first; a stage's agent checkboxes list the roster; changing mode to `solo` with two agents ticked fails the save with a readable message; ↑/↓ reorder stages; Save reports "Saved." and a reload shows the same document; Preview shows the composed prompt with the right coordination sentence.

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all green (this task is front-end only, but the suite must stay clean).

- [ ] **Step 5: Commit**

```bash
git add server/static/app.js server/static/style.css
git commit -m "feat(workflows): editor for agents, stages, and stage preview"
```

---

### Task 9: Session panel and board badge

**Files:**
- Modify: `server/static/session.html` (add `#workflow` after `#approval`)
- Modify: `server/static/app.js` (`Detail.load` at ~line 1098; `Dashboard.card` at ~line 534; append a `WorkflowPanel` controller)
- Modify: `server/static/style.css` (append)

**Interfaces:**
- Consumes: `GET/POST/DELETE /api/sessions/{sid}/workflow`, `/workflow/send`, `/workflow/advance` from Task 6; `GET /api/workflows` from Task 5; the `workflow` field on `/api/sessions` rows.
- Produces: global `WorkflowPanel` with `mount(sessionId)`, `render()`, `assign(wid)`, `send()`, `step(delta)`, `unassign()`; global `workflowBadge(s)`.

- [ ] **Step 1: Add the panel slot**

In `server/static/session.html`, after the approval div:

```html
      <div id="approval"></div>
      <div id="workflow"></div>
```

- [ ] **Step 2: Add the panel controller**

Append to `server/static/app.js`:

```javascript
// ---- Workflow panel (session detail) ----------------------------------------
//
// Shows which blueprint this session is running and where it has got to.
// Sending is always a button press: assigning costs nothing, running a stage
// spends tokens, and those are two different decisions.

const WorkflowPanel = {
  id: null,
  state: null,

  async mount(sessionId) {
    this.id = sessionId;
    await this.refresh();
  },

  async refresh() {
    const box = document.getElementById("workflow");
    if (!box || !this.id) return;
    try {
      this.state = await getJSON(`/api/sessions/${encodeURIComponent(this.id)}/workflow`);
    } catch (e) {
      box.innerHTML = `<div class="wf-panel"><span class="muted">workflow: ${esc(e.message)}</span></div>`;
      return;
    }
    this.render();
  },

  async render() {
    const box = document.getElementById("workflow");
    const s = this.state;
    if (!s || !s.bound) return this.renderPicker(box);
    const step = `Stage ${s.stage_index + 1}/${s.stage_count}`;
    box.innerHTML = `
      <div class="wf-panel">
        <div class="wf-panel-head">
          <span class="badge wf-badge">🧩 ${esc(s.title)}</span>
          <span class="badge">${step}${s.stage_name ? " · " + esc(s.stage_name) : ""}</span>
          ${s.sent ? `<span class="badge wf-sent">sent</span>` : ""}
          <span class="spacer"></span>
          <button class="hdr-btn" id="wfPrev"${s.stage_index === 0 ? " disabled" : ""}>← Back</button>
          <button class="hdr-btn primary" id="wfSend">${s.sent ? "▶ Re-send stage" : "▶ Send stage"}</button>
          <button class="hdr-btn" id="wfNext"${s.stage_index >= s.stage_count - 1 ? " disabled" : ""}>✓ Advance</button>
          <button class="hdr-btn" id="wfUnassign" title="Stop running this workflow here">✕ Unassign</button>
        </div>
        <details class="wf-prompt">
          <summary>Prompt this stage will send</summary>
          <pre>${esc(s.prompt || "")}</pre>
        </details>
        <div class="modal-status" id="wfPanelStatus"></div>
      </div>`;
    document.getElementById("wfSend").addEventListener("click", () => this.send());
    document.getElementById("wfPrev").addEventListener("click", () => this.step(-1));
    document.getElementById("wfNext").addEventListener("click", () => this.step(1));
    document.getElementById("wfUnassign").addEventListener("click", () => this.unassign());
  },

  async renderPicker(box) {
    let list = [];
    try { list = (await getJSON("/api/workflows")).workflows || []; }
    catch (e) { box.innerHTML = ""; return; }
    if (!list.length) { box.innerHTML = ""; return; }   // nothing to assign yet
    box.innerHTML = `
      <div class="wf-panel">
        <div class="wf-panel-head">
          <span class="muted">No workflow assigned.</span>
          <select id="wfPick" class="wf-input">
            <option value="">Assign a workflow…</option>
            ${list.map((w) => `<option value="${esc(w.id)}">${esc(w.title)} (${w.stage_count} stage${w.stage_count === 1 ? "" : "s"})</option>`).join("")}
          </select>
          <a class="hdr-btn" href="/workflows.html">🧩 Manage</a>
        </div>
      </div>`;
    document.getElementById("wfPick").addEventListener("change", (e) => {
      if (e.target.value) this.assign(e.target.value);
    });
  },

  async post(path, body) {
    const r = await fetch(`/api/sessions/${encodeURIComponent(this.id)}/workflow${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || ("HTTP " + r.status));
    return r.json();
  },

  async assign(wid) {
    try {
      this.state = await this.post("", { workflow_id: wid });
      this.render();
    } catch (e) { alert("Could not assign: " + e.message); }
  },

  async send() {
    const btn = document.getElementById("wfSend");
    const status = document.getElementById("wfPanelStatus");
    btn.disabled = true;
    status.textContent = "Sending…";
    try {
      this.state = await this.post("/send");
      this.render();
      document.getElementById("wfPanelStatus").textContent = "Sent into the live session.";
      if (typeof Detail !== "undefined" && Detail.id === this.id) Detail.load();
    } catch (e) {
      btn.disabled = false;
      status.textContent = "Failed: " + e.message;   // 409 = no live tmux session
    }
  },

  async step(delta) {
    try {
      this.state = await this.post("/advance", { delta });
      this.render();
    } catch (e) { alert("Could not advance: " + e.message); }
  },

  async unassign() {
    if (!confirm("Unassign this workflow? The stage pointer is forgotten; the workflow itself is untouched.")) return;
    try {
      const r = await fetch(`/api/sessions/${encodeURIComponent(this.id)}/workflow`, { method: "DELETE" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      this.state = { bound: false };
      this.render();
    } catch (e) { alert("Could not unassign: " + e.message); }
  },
};

// Board / triage card badge for a session running a workflow.
function workflowBadge(s) {
  const w = s.workflow;
  if (!w) return "";
  const step = w.stage_count ? ` ${w.stage_index + 1}/${w.stage_count}` : "";
  return `<span class="badge wf-badge" title="Running “${esc(w.title)}” — ${esc(w.stage_name || "")}">🧩 ${esc(w.title)}${step}</span>`;
}
```

- [ ] **Step 3: Mount the panel from Detail**

In `Detail.load()` in `server/static/app.js`, after `this.loadSummary(d);`:

```javascript
      WorkflowPanel.mount(this.id);
```

- [ ] **Step 4: Put the badge on board and triage cards**

Two card renderers show a badge row and both already call `taskBadge(s)`: `Dashboard.card` (`server/static/app.js:551`) and the triage card (`server/static/app.js:3875`). In each, add the badge immediately after `${taskBadge(s)}`:

```javascript
          ${workflowBadge(s)}
```

Leave the attention-rail item (`server/static/app.js:3363`) alone — its row is two lines of chrome next to a title, and a workflow name would not fit.

- [ ] **Step 5: Add the panel styles**

Append to `server/static/style.css`:

```css
.wf-panel { margin: 8px 0; padding: 10px 12px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--panel); display: flex; flex-direction: column; gap: 8px; }
.wf-panel-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.wf-panel-head .spacer { flex: 1; }
.wf-badge { color: var(--result); border-color: rgba(188,140,255,.5); }
.wf-sent { color: var(--assistant); border-color: rgba(63,185,80,.5); }
.wf-prompt summary { cursor: pointer; font-size: 13px; color: var(--muted); }
.wf-prompt pre { white-space: pre-wrap; margin: 6px 0 0; font-size: 12px; max-height: 320px; overflow: auto; }
```

- [ ] **Step 6: Verify by hand**

Open a session at `http://localhost:8765/session.html?id=<id>`. Check: with no workflows defined the panel is absent; after defining one, the picker appears and assigning shows the stage stepper; the collapsed prompt matches the editor's preview; on a session with no live tmux, "▶ Send stage" reports `no live tmux session` in the panel rather than an alert; Advance moves the stepper and flips the button back to "▶ Send stage"; the board card shows the 🧩 badge with the stage counter; Unassign clears it.

Then, on a session that does have a live pane, press "▶ Send stage" once and confirm the composed prompt lands in the REPL. This is the only step in the plan that spends tokens — do it deliberately, on a throwaway session.

- [ ] **Step 7: Run the suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add server/static/session.html server/static/app.js server/static/style.css
git commit -m "feat(workflows): session panel to send and advance stages"
```

---

### Task 10: Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: the finished feature.
- Produces: nothing code depends on.

- [ ] **Step 1: Document the module in the README**

Add a `## Workflows` section after the Projects section, covering: what a workflow is (agent roster + ordered stages), the four coordination modes and what each renders into the prompt, how to assign one to a session, that sending is manual and stage-by-stage, that a workflow never spawns sessions, and the YAML import/export shape with a short complete example file.

- [ ] **Step 2: Add the module to the architecture doc**

Add `server/workflows.py` to the module list in `docs/ARCHITECTURE.md` with a one-line responsibility ("blueprint store: agent rosters, stages, stage→prompt composition, and per-session bindings") and note that `app.py` depends on it the way it depends on `projects.py`.

- [ ] **Step 3: Add a changelog entry**

Add an entry under the current unreleased heading in `CHANGELOG.md`:

```markdown
- Workflows: define reusable multi-agent blueprints (agent roster, stages, coordination mode), assign one to a session, and send each stage into the live REPL. YAML import/export.
```

- [ ] **Step 4: Refresh the knowledge graph**

Run: `graphify update .`
Expected: completes without error (AST-only, no API cost).

- [ ] **Step 5: Commit**

```bash
git add README.md docs/ARCHITECTURE.md CHANGELOG.md
git commit -m "docs: workflows module"
```

`graphify-out/` is gitignored, so the refreshed graph is not part of this commit.

---

## Verification

After Task 10, the whole feature is checkable in one pass:

- [ ] `.venv/bin/python -m pytest tests/ -q` — all tests pass, including the five new files.
- [ ] `git status` — `server/.workflows.json` does not appear (it is ignored).
- [ ] A workflow defined in the UI, exported to YAML, deleted, and re-imported produces the same agents and stages.
- [ ] A session with that workflow assigned shows the correct stage prompt, sends it into a live REPL, and advances.
- [ ] `/clear` on a bound session (`POST /api/sessions/{id}/reset`) leaves the new session id carrying the same workflow and stage.
