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


def test_sessions_list_carries_the_binding(client, wid, monkeypatch):
    """The board badge reads this field, so it must survive the decorate pass."""
    workflows.bind(SID, wid)
    rows = client.get("/api/sessions?limit=all&archived=all").json()["sessions"]
    for row in rows:
        assert "workflow" in row
        if row["session_id"] == SID:
            assert row["workflow"]["title"] == "Feature delivery"
