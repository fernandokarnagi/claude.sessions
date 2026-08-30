"""
YAML round-trip — a workflow you can diff, review, and keep in git.

The document carries stages only. Agents live in ~/.claude/agents and are
referenced by id, so a workflow file is portable between machines that share
an agent roster and stays readable on one that does not.

Import always mints a new id: bringing a file in must never silently
overwrite a workflow you are already running.
"""

import pytest
import yaml

from server import workflows

STAGES = [
    {"name": "Discovery", "goal": "look", "mode": "parallel",
     "agent_ids": ["Researcher", "Builder"], "exit_criteria": "a list"},
    {"name": "Build", "goal": "write", "mode": "solo",
     "agent_ids": ["Builder"], "exit_criteria": "green tests"},
]


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "_PATH", str(tmp_path / "workflows.json"))


@pytest.fixture
def wid():
    w = workflows.create_workflow("Feature delivery", "Research then build")["id"]
    workflows.update_workflow(w, stages=STAGES)
    return w


def test_export_shape(wid):
    doc = yaml.safe_load(workflows.to_yaml(wid))
    assert doc["title"] == "Feature delivery"
    assert "agents" not in doc                       # the roster is not ours
    assert [s["name"] for s in doc["stages"]] == ["Discovery", "Build"]
    # Identity and timestamps belong to this machine, not to the document.
    assert "id" not in doc and "created_at" not in doc and "updated_at" not in doc


def test_export_unknown_workflow():
    assert workflows.to_yaml("deadbeef0000") is None


def test_export_with_shared_field_values_round_trips():
    """to_yaml must never emit an anchor: PyYAML's SafeRepresenter only shares
    references for containers, and two stages that happen to hold equal values
    should still export as two independent maps and re-import clean."""
    wid = workflows.create_workflow("W")["id"]
    workflows.update_workflow(wid, stages=[
        {"name": "A", "mode": "solo", "agent_ids": ["Builder"]},
        {"name": "B", "mode": "solo", "agent_ids": ["Builder"]},
    ])
    text = workflows.to_yaml(wid)
    assert "&" not in text and "*" not in text
    copy = workflows.from_yaml(text)
    assert [st["name"] for st in copy["stages"]] == ["A", "B"]


def test_importing_a_legacy_file_ignores_its_agent_roster():
    """Files exported before the central roster carried an agents block. The
    stages still name agents by id, and those ids are what resolve now, so the
    block is dropped rather than making the file unimportable."""
    copy = workflows.from_yaml(
        "title: Legacy\n"
        "agents:\n"
        "  - name: Builder\n"
        "    prompt: You build.\n"
        "stages:\n"
        "  - name: X\n"
        "    mode: solo\n"
        "    agent_ids: [Builder]\n")
    assert "agents" not in copy
    assert copy["stages"][0]["agent_ids"] == ["Builder"]


def test_round_trip_makes_an_equal_copy_with_a_new_id(wid):
    copy = workflows.from_yaml(workflows.to_yaml(wid))
    assert copy["id"] != wid
    original = workflows.get_workflow(wid)
    for field in ("title", "description", "stages"):
        assert copy[field] == original[field]


def test_import_rejects_non_mapping():
    with pytest.raises(ValueError, match="mapping"):
        workflows.from_yaml("- just\n- a list\n")


def test_import_rejects_broken_yaml():
    with pytest.raises(ValueError, match="YAML"):
        workflows.from_yaml("title: [unclosed\n")


def test_import_rejects_missing_title():
    with pytest.raises(ValueError, match="title"):
        workflows.from_yaml("stages: []\n")


def test_import_keeps_an_agent_id_with_no_file():
    """A file written on another machine names agents this one may not have.
    Importing it must still work — the missing agent shows up when the stage
    is composed, not as a refusal to read the file at all."""
    wf = workflows.from_yaml(
        "title: W\nstages:\n  - name: X\n    mode: solo\n    agent_ids: [ghost]\n")
    assert wf["stages"][0]["agent_ids"] == ["ghost"]


def test_import_rejects_bad_mode():
    text = "title: W\nstages:\n  - name: X\n    mode: swarm\n    agent_ids: [Builder]\n"
    with pytest.raises(ValueError, match="mode"):
        workflows.from_yaml(text)


def test_import_without_ids_mints_them():
    text = "title: W\nstages:\n  - name: X\n    mode: solo\n    agent_ids: [Builder]\n"
    wf = workflows.from_yaml(text)
    assert wf["stages"][0]["id"] == "s1"
