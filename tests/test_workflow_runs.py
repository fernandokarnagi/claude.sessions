"""
The stage run log — what was actually sent into a session, and how it went.

A run opens when a stage is sent and closes when the session goes quiet. The
end time is the session's last write, not the moment the dashboard noticed, so
a duration is what the turn took rather than how often the page was polled.
"""

import pytest
from fastapi.testclient import TestClient

from server import app as app_module
from server import parser, tmuxio, workflows
from server.app import app

SID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

STAGES = [
    {"name": "Build", "mode": "solo", "agent_ids": ["Builder"], "goal": "ship"},
    {"name": "Review", "mode": "solo", "agent_ids": ["Builder"], "goal": "check"},
]


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "_PATH", str(tmp_path / "workflows.json"))
    monkeypatch.setattr(app_module, "_session_exists", lambda sid: sid == SID)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(tmuxio, "say",
                        lambda sid, text, **kw: calls.append((sid, text)) or {"ok": True})
    return calls


@pytest.fixture
def bound(client):
    wid = workflows.create_workflow("Feature delivery")["id"]
    workflows.update_workflow(wid, stages=STAGES)
    client.post(f"/api/sessions/{SID}/workflow", json={"workflow_id": wid})
    return wid


def quiet(monkeypatch, model="claude-opus-5", ago=999.0, at=None):
    """Pretend the session's transcript last moved `ago` seconds back.

    `at` is the write the run's end time is taken from, and closing a run
    refuses an end time older than its start — so it has to be later than the
    wall clock the send ran on. Anchoring it to now rather than to a literal
    keeps that true on every day the suite is run. Returns the value used.
    """
    import time
    from datetime import datetime, timedelta, timezone
    at = at or (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    monkeypatch.setattr(app_module, "_session_pulse",
                        lambda sid: {"model": model, "mtime": time.time() - ago,
                                     "updated_at": at})
    return at


def runs(client):
    return client.get(f"/api/sessions/{SID}/workflow/runs").json()["runs"]


def test_no_runs_before_anything_is_sent(client, bound):
    assert runs(client) == []


def test_sending_opens_a_run_with_the_model_and_agents(client, bound, sent, monkeypatch):
    quiet(monkeypatch, ago=0.0)          # still working, so the run stays open
    client.post(f"/api/sessions/{SID}/workflow/send")
    (r,) = runs(client)
    assert r["stage_name"] == "Build" and r["stage_index"] == 0
    assert r["model"] == "claude-opus-5"
    assert r["agent_ids"] == ["Builder"]
    assert r["chars"] == len(sent[0][1])
    assert r["edited"] is False
    assert r["started_at"] and r["ended_at"] is None


def test_a_missing_agent_id_is_logged_as_itself(client, sent, monkeypatch):
    wid = workflows.create_workflow("W")["id"]
    workflows.update_workflow(wid, stages=[
        {"name": "X", "mode": "parallel", "agent_ids": ["Builder", "ghost"]}])
    client.post(f"/api/sessions/{SID}/workflow", json={"workflow_id": wid})
    quiet(monkeypatch, ago=0.0)
    client.post(f"/api/sessions/{SID}/workflow/send")
    assert runs(client)[0]["agent_ids"] == ["Builder", "ghost"]


def test_an_edited_prompt_is_sent_and_flagged(client, bound, sent, monkeypatch):
    quiet(monkeypatch, ago=0.0)
    r = client.post(f"/api/sessions/{SID}/workflow/send",
                    json={"prompt": "Do the build, but start with the tests."})
    assert r.status_code == 200
    assert sent[0][1] == "Do the build, but start with the tests."
    log = runs(client)[0]
    assert log["edited"] is True and log["chars"] == len(sent[0][1])


def test_resending_the_composed_prompt_is_not_an_edit(client, bound, sent, monkeypatch):
    """The editor opens prefilled, so sending it untouched must not read as a
    change — otherwise every send would look edited."""
    quiet(monkeypatch, ago=0.0)
    composed = client.get(f"/api/sessions/{SID}/workflow").json()["prompt"]
    client.post(f"/api/sessions/{SID}/workflow/send", json={"prompt": composed})
    assert runs(client)[0]["edited"] is False


def test_an_empty_prompt_falls_back_to_the_stage(client, bound, sent, monkeypatch):
    quiet(monkeypatch, ago=0.0)
    client.post(f"/api/sessions/{SID}/workflow/send", json={"prompt": "   "})
    assert "## Stage: Build" in sent[0][1]
    assert runs(client)[0]["edited"] is False


def test_an_oversized_prompt_is_413(client, bound, sent):
    r = client.post(f"/api/sessions/{SID}/workflow/send",
                    json={"prompt": "x" * (app_module._MAX_STAGE_PROMPT + 1)})
    assert r.status_code == 413
    assert sent == []                     # nothing reached the pane


def test_a_quiet_session_closes_the_run(client, bound, sent, monkeypatch):
    quiet(monkeypatch, ago=0.0)
    client.post(f"/api/sessions/{SID}/workflow/send")
    # The transcript stopped moving, and long enough ago to count as done.
    at = quiet(monkeypatch, ago=parser.THINKING_MAX_AGE + 5)
    r = runs(client)[0]
    assert r["ended_at"] == at
    assert r.get("superseded") is not True


def test_a_still_working_session_leaves_the_run_open(client, bound, sent, monkeypatch):
    quiet(monkeypatch, ago=0.0)
    client.post(f"/api/sessions/{SID}/workflow/send")
    assert runs(client)[0]["ended_at"] is None


def test_sending_again_supersedes_the_open_run(client, bound, sent, monkeypatch):
    """Sending while a turn is in flight ends that turn's claim on the pane —
    whatever it was doing, this send is where it stopped being the one."""
    quiet(monkeypatch, ago=0.0)
    client.post(f"/api/sessions/{SID}/workflow/send")
    client.post(f"/api/sessions/{SID}/workflow/send")
    log = runs(client)
    assert len(log) == 2
    assert log[0]["ended_at"] is None                 # newest first
    assert log[1]["ended_at"] and log[1]["superseded"] is True


def test_advancing_keeps_the_history_of_earlier_stages(client, bound, sent, monkeypatch):
    quiet(monkeypatch, ago=parser.THINKING_MAX_AGE + 5)
    client.post(f"/api/sessions/{SID}/workflow/send")
    client.post(f"/api/sessions/{SID}/workflow/advance", json={"delta": 1})
    client.post(f"/api/sessions/{SID}/workflow/send")
    assert [r["stage_name"] for r in runs(client)] == ["Review", "Build"]


def test_the_panel_carries_the_run_count(client, bound, sent, monkeypatch):
    quiet(monkeypatch, ago=0.0)
    assert client.get(f"/api/sessions/{SID}/workflow").json()["run_count"] == 0
    client.post(f"/api/sessions/{SID}/workflow/send")
    assert client.get(f"/api/sessions/{SID}/workflow").json()["run_count"] == 1


def test_unassigning_forgets_the_log(client, bound, sent, monkeypatch):
    """The log lives on the binding: it is the history of this session running
    this workflow, and unassigning ends that."""
    quiet(monkeypatch, ago=0.0)
    client.post(f"/api/sessions/{SID}/workflow/send")
    client.delete(f"/api/sessions/{SID}/workflow")
    assert runs(client) == []


def test_a_write_older_than_the_send_does_not_close_the_run(monkeypatch):
    """The transcript's last write predating the send means the session has
    not answered yet — closing there would report a negative duration."""
    workflows.create_workflow("W")
    wid = workflows.list_workflows()[0]["id"]
    workflows.update_workflow(wid, stages=STAGES)
    workflows.bind(SID, wid)
    workflows.start_run(SID, stage_id="s1", stage_name="Build", stage_index=0,
                        model="m", agent_ids=["Builder"], chars=10, edited=False)
    assert workflows.close_open_run(SID, "2000-01-01T00:00:00+00:00") is False
    assert workflows.list_runs(SID)[0]["ended_at"] is None
