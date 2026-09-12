"""/api/search over transcript text: what comes back, and in what shape.

The fleet is two scratch transcripts in a temp directory, indexed for real —
the point of these tests is the seam between the index and the board summary,
so stubbing the index out would test nothing. The provider fan-outs (agy, grok,
opencode) are stubbed because they shell out to tmux.
"""

import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import (agyparser, app as appmod, archives, attention,  # noqa: E402
                    budgets, descriptions, grokparser, index, ledger,
                    opencodeparser, overrides, parser, prices, projects,
                    registry, runner, tasks, tmuxio)
from server.app import app  # noqa: E402

ONE = "11111111-1111-1111-1111-111111111111"
TWO = "22222222-2222-2222-2222-222222222222"


def event(role, text, ts="2026-09-12T10:00:00.000Z") -> str:
    return json.dumps({"type": role, "timestamp": ts,
                       "message": {"role": role, "content": text}})


def transcript(root, sid, title, lines):
    d = os.path.join(root, "-proj-one")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{sid}.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "summary", "summary": title, "cwd": "/proj/one",
                             "sessionId": sid,
                             "timestamp": "2026-09-12T10:00:00.000Z"}) + "\n")
        for line in lines:
            fh.write(line + "\n")


@pytest.fixture(autouse=True)
def fleet(tmp_path, monkeypatch):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    monkeypatch.setattr(parser, "PROJECTS_DIR", str(projects_dir))
    monkeypatch.setattr(parser, "_summary_cache", {})
    monkeypatch.setattr(index, "DB_PATH", str(tmp_path / ".search.db"))
    # The other three stores are empty here; without this the index would read
    # the operator's real agy / grok / opencode history.
    monkeypatch.setattr(agyparser, "CONV_DIR", str(tmp_path / "agy"))
    monkeypatch.setattr(grokparser, "SESS_ROOT", str(tmp_path / "grok"))
    monkeypatch.setattr(opencodeparser, "DATA_DIR", str(tmp_path / "opencode"))
    monkeypatch.setattr(budgets, "_PATH", str(tmp_path / ".budgets.json"))
    monkeypatch.setattr(prices, "PRICES_FILE", str(tmp_path / "no-prices.json"))
    monkeypatch.setattr(prices, "_cache", {"at": 0.0, "data": {}})
    ledger._session_cache.clear()

    transcript(str(projects_dir), ONE, "kafka work", [
        event("user", "wire the kafka consumer to the retry topic"),
        event("assistant", "Done — it retries with a backoff now."),
    ])
    transcript(str(projects_dir), TWO, "unrelated", [
        event("user", "rename the billing column"),
    ])

    # Everything the board decorates with, held still.
    monkeypatch.setattr(registry, "web_mtimes", lambda: {})
    monkeypatch.setattr(runner, "running_ids", lambda: set())
    monkeypatch.setattr(tmuxio, "tmux_sessions", lambda: set())
    monkeypatch.setattr(archives, "archived_ids", lambda: set())
    monkeypatch.setattr(attention, "marked_ids", lambda: set())
    monkeypatch.setattr(overrides, "all_titles", lambda: {})
    monkeypatch.setattr(descriptions, "all_descriptions", lambda: {})
    monkeypatch.setattr(projects, "tags_by_session", lambda: {ONE: [{"id": "p1"}]})
    monkeypatch.setattr(tasks, "counts_by_session", lambda: {ONE: 3})
    for name in ("_agy_summaries", "_grok_summaries", "_opencode_summaries"):
        monkeypatch.setattr(appmod, name, lambda *a, **k: [])
    index.refresh()
    return projects_dir


@pytest.fixture
def client():
    return TestClient(app)


def ids(payload) -> list:
    return [s["session_id"] for s in payload["sessions"]]


# ---- finding by text ---------------------------------------------------------

def test_text_mode_finds_a_session_by_what_was_said(client):
    # "backoff" is in the reply only — never in a header field.
    d = client.get("/api/search?q=backoff&mode=text").json()
    assert ids(d) == [ONE]
    assert d["total"] == 1


def test_text_mode_ignores_the_header_fields(client):
    # An id prefix is a header match, and nothing was ever said about it.
    assert ids(client.get("/api/search?q=22222222&mode=text").json()) == []


def test_meta_mode_is_the_old_behaviour(client):
    d = client.get("/api/search?q=22222222&mode=meta").json()
    assert ids(d) == [TWO]
    assert d["hits"] == {}
    assert d["index"] is None


def test_meta_mode_does_not_read_the_transcripts(client):
    assert ids(client.get("/api/search?q=backoff&mode=meta").json()) == []


def test_both_is_the_default_and_covers_either_route(client):
    assert ids(client.get("/api/search?q=backoff").json()) == [ONE]
    assert ids(client.get("/api/search?q=22222222").json()) == [TWO]


def test_a_session_matching_both_ways_appears_once(client):
    # "kafka" is in ONE's first prompt, which is also its title.
    d = client.get("/api/search?q=kafka&mode=both").json()
    assert ids(d) == [ONE]


def test_an_unknown_mode_is_rejected(client):
    assert client.get("/api/search?q=x&mode=sideways").status_code == 400


# ---- what a result carries ---------------------------------------------------

def test_a_result_carries_the_full_session_header(client):
    s = client.get("/api/search?q=kafka&mode=text").json()["sessions"][0]
    for field in ("status", "model", "tokens", "cost", "project", "title",
                  "updated_at", "archived", "projects", "task_count"):
        assert field in s, field
    assert s["task_count"] == 3
    assert s["projects"] == [{"id": "p1"}]


def test_hits_carry_the_matching_lines(client):
    d = client.get("/api/search?q=kafka&mode=text").json()
    hit = d["hits"][ONE][0]
    assert hit["role"] == "user"
    assert hit["provider"] == "claude"
    assert index.MARK_OPEN in hit["snippet"]


def test_hits_are_capped_per_session(client):
    d = client.get("/api/search?q=kafka&mode=text&per_session=1").json()
    assert len(d["hits"][ONE]) == 1


def test_the_answer_says_what_the_index_holds(client):
    d = client.get("/api/search?q=kafka&mode=text").json()
    assert d["index"]["sessions"] == 2
    assert d["mode"] == "text"


def test_an_empty_query_returns_no_text_hits(client):
    d = client.get("/api/search?q=&mode=text").json()
    assert d["sessions"] == [] and d["hits"] == {}


# ---- archived ----------------------------------------------------------------

def test_an_archived_session_is_hidden_by_default(client, monkeypatch):
    monkeypatch.setattr(archives, "archived_ids", lambda: {ONE})
    d = client.get("/api/search?q=kafka&mode=text").json()
    assert ids(d) == []
    assert d["hits"] == {}          # no snippets for a session you can't see


def test_archived_include_brings_it_back(client, monkeypatch):
    monkeypatch.setattr(archives, "archived_ids", lambda: {ONE})
    d = client.get("/api/search?q=kafka&mode=text&archived=include").json()
    assert ids(d) == [ONE]
    assert ONE in d["hits"]


# ---- the index endpoints -----------------------------------------------------

def test_the_index_endpoint_reports_state(client):
    d = client.get("/api/search/index").json()
    assert d["sessions"] == 2
    assert d["messages"] == 3


def test_reindex_kicks_a_pass(client, monkeypatch):
    calls = []
    monkeypatch.setattr(index, "ensure_fresh", lambda force=False: calls.append(force) or True)
    assert client.post("/api/search/reindex").json()["started"] is True
    assert calls == [True]


def test_rebuild_throws_the_index_away_first(client, monkeypatch):
    monkeypatch.setattr(index, "ensure_fresh", lambda force=False: False)
    assert client.post("/api/search/reindex?rebuild=true").json()["rebuild"] is True
    assert index.stats()["sessions"] == 0
