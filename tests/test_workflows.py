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


def test_duplicate_stage_ids_are_repaired_not_rejected():
    """A well-formed but duplicate id is bookkeeping, not operator content —
    validate() mints a fresh one instead of raising, the same way it repairs
    a missing or malformed id."""
    wid = workflows.create_workflow("W")["id"]
    wf = workflows.update_workflow(wid, agents=AGENTS, stages=[
        {"id": "s1", "name": "One", "mode": "solo", "agent_ids": ["researcher"]},
        {"id": "s1", "name": "Two", "mode": "solo", "agent_ids": ["builder"]},
    ])
    assert [s["id"] for s in wf["stages"]] == ["s1", "s2"]


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
