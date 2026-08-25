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


def test_export_with_shared_default_model_round_trips():
    """to_yaml must never emit an anchor: PyYAML's SafeRepresenter only
    shares references for containers, and to_yaml builds four distinct ones
    for title/description/agents/stages, so two agents sharing the default
    model should still export as two independent maps and re-import clean."""
    wid = workflows.create_workflow("W")["id"]
    workflows.update_workflow(wid, agents=[
        {"name": "A", "prompt": "do a"},
        {"name": "B", "prompt": "do b"},
    ], stages=[])
    text = workflows.to_yaml(wid)
    assert "&" not in text and "*" not in text
    copy = workflows.from_yaml(text)
    assert [a["model"] for a in copy["agents"]] == ["opus", "opus"]


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
