"""
Stage composition — turning a stage into the prompt that gets typed.

This is the substance of the module: the stored blueprint only matters
because of what it renders to. Pure function, no I/O beyond the store.
"""

import pytest

from server import workflows

@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "_PATH", str(tmp_path / "workflows.json"))


def build(mode, agent_ids, **stage):
    wid = workflows.create_workflow("Feature delivery", "Research then build")["id"]
    workflows.update_workflow(wid, stages=[
        dict({"name": "Discovery", "goal": "Establish what exists",
              "mode": mode, "agent_ids": agent_ids,
              "exit_criteria": "A written list"}, **stage),
    ])
    return wid


def test_header_carries_workflow_and_stage():
    text = workflows.compose_stage(build("solo", ["Builder"]), 0)
    assert "# Workflow: Feature delivery" in text
    assert "## Stage: Discovery" in text
    assert "Goal: Establish what exists" in text
    assert "Exit criteria: A written list" in text


def test_description_and_stage_count_stay_out():
    """Both describe work the agent has not been sent, and it will do it."""
    text = workflows.compose_stage(build("solo", ["Builder"]), 0)
    assert "Research then build" not in text
    assert "1/1" not in text


def test_no_model_line():
    """An agent has no model of its own — the session's model is fixed at
    launch, so naming one here could only mislead."""
    text = workflows.compose_stage(build("solo", ["Builder"]), 0)
    assert "Model:" not in text


def test_scope_note_ends_every_stage():
    text = workflows.compose_stage(build("solo", ["Builder"]), 0)
    assert "## Scope" in text
    assert text.rstrip().endswith(workflows.SCOPE_NOTE)


def test_solo_sentence_and_body():
    text = workflows.compose_stage(build("solo", ["Builder"]), 0)
    assert "Coordination: solo. Builder runs this stage alone." in text
    assert "### Builder — Write the code" in text
    assert "You write the code." in text
    assert "Researcher" not in text          # non-participants stay out


def test_coordinator_names_the_lead():
    text = workflows.compose_stage(build("coordinator", ["Builder", "Researcher", "Reviewer"]), 0)
    assert ("Coordination: coordinator. Builder leads this stage and delegates "
            "to Researcher and Reviewer. Builder owns the final answer.") in text
    for name in ("Builder", "Researcher", "Reviewer"):
        assert f"### {name} — " in text


def test_handoff_shows_the_chain():
    text = workflows.compose_stage(build("handoff", ["Researcher", "Builder", "Reviewer"]), 0)
    assert ("Coordination: hand-off. Run in order: Researcher → Builder → Reviewer. "
            "Each agent takes the previous agent's output as its input.") in text


def test_parallel_sentence():
    text = workflows.compose_stage(build("parallel", ["Researcher", "Reviewer"]), 0)
    assert ("Coordination: parallel. Researcher and Reviewer each work the same "
            "input independently; merge the results at the end.") in text


def test_empty_optional_fields_are_omitted():
    wid = build("solo", ["Builder"], goal="", exit_criteria="")
    workflows.update_workflow(wid, description="")
    text = workflows.compose_stage(wid, 0)
    assert "Goal:" not in text
    assert "Exit criteria:" not in text
    assert text.startswith("# Workflow: Feature delivery\n\n## Stage: Discovery")


def test_stage_with_no_agents_says_so():
    wid = build("parallel", [])
    text = workflows.compose_stage(wid, 0)
    assert "No agents are assigned to this stage." in text


def test_index_out_of_range():
    wid = build("solo", ["Builder"])
    with pytest.raises(ValueError, match="stage index"):
        workflows.compose_stage(wid, 1)


def test_unknown_workflow():
    with pytest.raises(ValueError, match="workflow not found"):
        workflows.compose_stage("deadbeef0000", 0)


def test_a_missing_agent_file_is_named_in_the_prompt():
    """A stage written for two agents that runs with one is a different stage,
    and the operator reads this prompt before sending it."""
    text = workflows.compose_stage(build("parallel", ["Builder", "ghost"]), 0)
    assert "### Builder — Write the code" in text
    assert "no agent file was found for ghost" in text
