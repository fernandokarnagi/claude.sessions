"""
Bindings — one workflow pinned to one session, plus a stage pointer.

The operator drives the pointer by hand; nothing here advances on its own.
"""

import pytest

from server import workflows

SID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

STAGES = [
    {"name": "One", "mode": "solo", "agent_ids": ["Builder"]},
    {"name": "Two", "mode": "solo", "agent_ids": ["Builder"]},
    {"name": "Three", "mode": "solo", "agent_ids": ["Builder"]},
]


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "_PATH", str(tmp_path / "workflows.json"))


@pytest.fixture
def wid():
    w = workflows.create_workflow("Feature delivery")["id"]
    workflows.update_workflow(w, stages=STAGES)
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
