"""
Workflow CRUD over HTTP. Validation is the store's; this file checks that the
right ValueError becomes the right status code.
"""

import pytest
from fastapi.testclient import TestClient

from server import workflows
from server.app import app

STAGES = [{"name": "Build", "mode": "solo", "agent_ids": ["Builder"], "goal": "ship"}]


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
    body = {"title": "W", "description": "d", "stages": STAGES}
    assert client.put(f"/api/workflows/{wid}", json=body).status_code == 200
    doc = client.get(f"/api/workflows/{wid}").json()
    assert doc["stages"][0]["id"] == "s1"
    assert doc["stages"][0]["agent_ids"] == ["Builder"]


def test_get_unknown_is_404(client):
    assert client.get("/api/workflows/deadbeef0000").status_code == 404


def test_put_unknown_is_404(client):
    r = client.put("/api/workflows/deadbeef0000",
                   json={"title": "W", "stages": []})
    assert r.status_code == 404


def test_put_validation_is_400(client):
    wid = client.post("/api/workflows", json={"title": "W"}).json()["id"]
    bad = {"title": "W",
           "stages": [{"name": "X", "mode": "swarm", "agent_ids": ["Builder"]}]}
    r = client.put(f"/api/workflows/{wid}", json=bad)
    assert r.status_code == 400 and "mode" in r.json()["detail"]


def test_delete(client):
    wid = client.post("/api/workflows", json={"title": "W"}).json()["id"]
    assert client.delete(f"/api/workflows/{wid}").status_code == 200
    assert client.delete(f"/api/workflows/{wid}").status_code == 404


def test_export_then_import(client):
    wid = client.post("/api/workflows", json={"title": "W"}).json()["id"]
    client.put(f"/api/workflows/{wid}",
               json={"title": "W", "stages": STAGES})
    text = client.get(f"/api/workflows/{wid}/export").text
    assert "Builder" in text and "agents:" not in text
    r = client.post("/api/workflows/import", json={"yaml": text})
    assert r.status_code == 200
    assert r.json()["id"] != wid
    assert len(client.get("/api/workflows").json()["workflows"]) == 2


def test_export_unknown_is_404(client):
    assert client.get("/api/workflows/deadbeef0000/export").status_code == 404


def test_import_bad_yaml_is_400(client):
    r = client.post("/api/workflows/import", json={"yaml": "- a\n- b\n"})
    assert r.status_code == 400 and "mapping" in r.json()["detail"]


def test_import_oversized_yaml_is_413(client):
    text = "title: W\n" + ("x" * (1024 * 1024 + 1))
    r = client.post("/api/workflows/import", json={"yaml": text})
    assert r.status_code == 413
    assert r.json()["detail"] == "workflow file too large"


def test_import_rejects_alias_amplification(client):
    """yaml.safe_load resolves aliases by sharing references, so a handful
    of bytes can expand into megabytes once the parsed result is str()'d.
    Refusing any alias at parse time closes this off at the root, and must
    leave no partial workflow behind."""
    text = (
        "title: W\n"
        "a: &a [x, x, x, x, x, x, x, x, x]\n"
        "b: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a]\n"
        "c: [*b, *b, *b, *b, *b, *b, *b, *b, *b]\n"
    )
    r = client.post("/api/workflows/import", json={"yaml": text})
    assert r.status_code == 400
    assert "alias" in r.json()["detail"].lower()
    assert client.get("/api/workflows").json()["workflows"] == []


def test_import_rejects_deep_nesting(client):
    """A document well inside the byte guard can still blow the recursion
    limit in PyYAML's constructor; that must come back as 400, not 500."""
    depth = 20000
    text = "title: W\nx: " + "[" * depth + "]" * depth
    r = client.post("/api/workflows/import", json={"yaml": text})
    assert r.status_code == 400
    assert "nested" in r.json()["detail"].lower()
    assert client.get("/api/workflows").json()["workflows"] == []


def test_import_ignores_a_legacy_agents_block(client):
    """Agents come from ~/.claude/agents now. An agents block in an older file
    is not this store's business, so it is skipped rather than validated."""
    text = "title: W\nagents: [just-a-string]\nstages: []\n"
    r = client.post("/api/workflows/import", json={"yaml": text})
    assert r.status_code == 200
    assert "agents" not in r.json()


def test_import_rejects_non_mapping_stage(client):
    text = "title: W\nstages: [42]\n"
    r = client.post("/api/workflows/import", json={"yaml": text})
    assert r.status_code == 400
    assert "stages[0]" in r.json()["detail"]


def test_duplicate_agent_id_in_a_stage_is_rejected(client):
    """Not reachable from the UI (checkboxes are set-semantics) but
    reachable from YAML and PUT; parallel mode doesn't catch this by
    accident the way solo's len(ids) > 1 rule does."""
    wid = client.post("/api/workflows", json={"title": "W"}).json()["id"]
    body = {
        "title": "W",
        "stages": [{"name": "X", "mode": "parallel",
                    "agent_ids": ["Builder", "Builder"]}],
    }
    r = client.put(f"/api/workflows/{wid}", json=body)
    assert r.status_code == 400
    assert "duplicate agent id" in r.json()["detail"]


def test_preview(client):
    wid = client.post("/api/workflows", json={"title": "W"}).json()["id"]
    client.put(f"/api/workflows/{wid}",
               json={"title": "W", "stages": STAGES})
    r = client.post(f"/api/workflows/{wid}/preview", json={"stage_index": 0})
    assert r.status_code == 200
    assert "Coordination: solo. Builder runs this stage alone." in r.json()["prompt"]


def test_preview_out_of_range_is_400(client):
    wid = client.post("/api/workflows", json={"title": "W"}).json()["id"]
    r = client.post(f"/api/workflows/{wid}/preview", json={"stage_index": 3})
    assert r.status_code == 400


def test_preview_composes_the_stage_in_the_body(client):
    """The editor previews what is on screen. Ticking an agent and pressing
    preview must show that agent, before anything is saved."""
    wid = client.post("/api/workflows", json={"title": "W"}).json()["id"]
    client.put(f"/api/workflows/{wid}", json={"title": "W", "stages": STAGES})
    r = client.post(f"/api/workflows/{wid}/preview", json={
        "stage_index": 0,
        "stage": {"name": "Build", "mode": "parallel",
                  "agent_ids": ["Builder", "Reviewer"]},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["saved"] is False
    assert "Builder and Reviewer each work the same input" in body["prompt"]


def test_preview_of_an_unsaved_stage_does_not_save_it(client):
    wid = client.post("/api/workflows", json={"title": "W"}).json()["id"]
    client.put(f"/api/workflows/{wid}", json={"title": "W", "stages": STAGES})
    client.post(f"/api/workflows/{wid}/preview", json={
        "stage_index": 0,
        "stage": {"name": "Renamed", "mode": "solo", "agent_ids": ["Reviewer"]},
    })
    doc = client.get(f"/api/workflows/{wid}").json()
    assert doc["stages"][0]["name"] == "Build"
    assert doc["stages"][0]["agent_ids"] == ["Builder"]


def test_preview_of_an_invalid_stage_is_400(client):
    wid = client.post("/api/workflows", json={"title": "W"}).json()["id"]
    r = client.post(f"/api/workflows/{wid}/preview", json={
        "stage": {"name": "X", "mode": "swarm", "agent_ids": []}})
    assert r.status_code == 400 and "mode" in r.json()["detail"]


def test_preview_without_a_stage_still_reads_the_store(client):
    wid = client.post("/api/workflows", json={"title": "W"}).json()["id"]
    client.put(f"/api/workflows/{wid}", json={"title": "W", "stages": STAGES})
    body = client.post(f"/api/workflows/{wid}/preview", json={"stage_index": 0}).json()
    assert body["saved"] is True
    assert "## Stage: Build" in body["prompt"]
