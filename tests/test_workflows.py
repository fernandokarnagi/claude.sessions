"""
Workflows — the blueprint store.

A workflow is an ordered list of stages, each naming agents by id. The agents
themselves live in ~/.claude/agents and are not stored here: see test_agents.py
for the roster, and conftest.py for the scratch folder these tests read.

Every test points the store at a tmp file, so the real state under server/ is
never touched.
"""

import pytest

from server import workflows


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "_PATH", str(tmp_path / "workflows.json"))


def test_create_defaults():
    wf = workflows.create_workflow("  Feature delivery  ", "  research then build  ")
    assert wf["title"] == "Feature delivery"          # stored trimmed
    assert wf["description"] == "research then build"
    assert wf["stages"] == []
    assert "agents" not in wf                         # the roster is not ours
    assert len(wf["id"]) == 12
    assert wf["created_at"] and wf["updated_at"]


def test_create_blank_title_falls_back():
    assert workflows.create_workflow("   ")["title"] == "Untitled workflow"


def test_update_assigns_stage_ids_and_keeps_agent_ids():
    wid = workflows.create_workflow("W")["id"]
    wf = workflows.update_workflow(wid, stages=[
        {"name": "Discovery", "goal": "look around", "mode": "parallel",
         "agent_ids": ["Researcher", "Builder"], "exit_criteria": "a list"},
    ])
    assert [s["id"] for s in wf["stages"]] == ["s1"]
    assert wf["stages"][0]["agent_ids"] == ["Researcher", "Builder"]


def test_saving_drops_a_legacy_agent_roster():
    """Workflows written before the central roster carried their own agents.
    The first save after upgrading is where that copy goes."""
    wid = workflows.create_workflow("W")["id"]
    with workflows._lock:
        data = workflows._load()
        data["workflows"][wid]["agents"] = [{"id": "builder", "name": "Builder"}]
        workflows._save(data)
    wf = workflows.update_workflow(wid, title="W")
    assert "agents" not in wf
    assert "agents" not in workflows.get_workflow(wid)


def test_stage_ids_are_never_reused():
    wid = workflows.create_workflow("W")["id"]
    workflows.update_workflow(wid, stages=[
        {"name": "One", "mode": "solo", "agent_ids": ["Researcher"]},
        {"name": "Two", "mode": "solo", "agent_ids": ["Builder"]},
    ])
    # Drop the first stage, then add another: the new one must not be s1.
    wf = workflows.update_workflow(wid, stages=[
        {"id": "s2", "name": "Two", "mode": "solo", "agent_ids": ["Builder"]},
        {"name": "Three", "mode": "solo", "agent_ids": ["Builder"]},
    ])
    assert [s["id"] for s in wf["stages"]] == ["s2", "s3"]


def test_duplicate_stage_ids_are_repaired_not_rejected():
    """A well-formed but duplicate id is bookkeeping, not operator content —
    validate() mints a fresh one instead of raising, the same way it repairs
    a missing or malformed id."""
    wid = workflows.create_workflow("W")["id"]
    wf = workflows.update_workflow(wid, stages=[
        {"id": "s1", "name": "One", "mode": "solo", "agent_ids": ["Researcher"]},
        {"id": "s1", "name": "Two", "mode": "solo", "agent_ids": ["Builder"]},
    ])
    assert [s["id"] for s in wf["stages"]] == ["s1", "s2"]


def test_stage_ids_survive_deleting_the_last_stage():
    """The high-water mark comes from what is stored, not from what the
    caller sent — otherwise deleting the newest stage and adding one in the
    same save would hand the new stage the id that was just freed."""
    wid = workflows.create_workflow("W")["id"]
    workflows.update_workflow(wid, stages=[
        {"name": "One", "mode": "solo", "agent_ids": ["Builder"]},
        {"name": "Two", "mode": "solo", "agent_ids": ["Builder"]},
    ])
    wf = workflows.update_workflow(wid, stages=[
        {"id": "s1", "name": "One", "mode": "solo", "agent_ids": ["Builder"]},
        {"name": "Fresh", "mode": "solo", "agent_ids": ["Builder"]},
    ])
    assert [s["id"] for s in wf["stages"]] == ["s1", "s3"]


def test_an_agent_id_with_no_file_is_kept():
    """The roster is a folder that changes without this module watching. A
    renamed or deleted agent file must not make the workflow unsavable — the
    id stays, and compose_stage is where the operator hears about it."""
    wid = workflows.create_workflow("W")["id"]
    wf = workflows.update_workflow(wid, stages=[
        {"name": "X", "mode": "solo", "agent_ids": ["ghost"]},
    ])
    assert wf["stages"][0]["agent_ids"] == ["ghost"]


def test_duplicate_agent_id_in_one_stage_rejected():
    wid = workflows.create_workflow("W")["id"]
    with pytest.raises(ValueError, match="duplicate agent"):
        workflows.update_workflow(wid, stages=[
            {"name": "X", "mode": "parallel", "agent_ids": ["Builder", "Builder"]},
        ])


def test_bad_mode_rejected():
    wid = workflows.create_workflow("W")["id"]
    with pytest.raises(ValueError, match="mode"):
        workflows.update_workflow(wid, stages=[
            {"name": "X", "mode": "swarm", "agent_ids": ["Builder"]},
        ])


def test_solo_stage_needs_exactly_one_agent():
    wid = workflows.create_workflow("W")["id"]
    with pytest.raises(ValueError, match="solo"):
        workflows.update_workflow(wid, stages=[
            {"name": "X", "mode": "solo", "agent_ids": ["Researcher", "Builder"]},
        ])


def test_nameless_stage_rejected():
    wid = workflows.create_workflow("W")["id"]
    with pytest.raises(ValueError, match="name"):
        workflows.update_workflow(wid, stages=[{"name": "  ", "mode": "solo"}])


def test_list_counts_and_persistence():
    a = workflows.create_workflow("A")
    workflows.update_workflow(a["id"], stages=[
        {"name": "One", "mode": "solo", "agent_ids": ["Builder"]},
        {"name": "Two", "mode": "parallel", "agent_ids": ["Builder", "Researcher"]},
    ])
    workflows.create_workflow("B")
    rows = workflows.list_workflows()
    assert [r["title"] for r in rows] == ["B", "A"]        # newest first
    row = next(r for r in rows if r["id"] == a["id"])
    # Distinct agents across stages, not the sum per stage.
    assert row["agent_count"] == 2 and row["stage_count"] == 2
    assert row["session_count"] == 0
    assert "stages" not in row                             # list stays light


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
