"""
The cost plane over HTTP: what /api/cost reports and what a budget PUT does.

The fleet is stubbed at parser.list_sessions, so these tests never read the
operator's transcripts and never depend on what those happen to have spent.
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import budgets, ledger, parser, prices  # noqa: E402
from server.app import app  # noqa: E402

SID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def summary(sid, model="claude-opus-5", out=1_000_000, project="/proj/one") -> dict:
    # mtime is the transcript's last write, so it has to agree with updated_at
    # — the wall clock behind the burn rate is measured between the two.
    return {
        "session_id": sid, "title": "t", "model": model, "project": project,
        "cwd": project, "status": "SLEEPING",
        "mtime": parser._epoch("2026-09-12T12:00:00.000Z"),
        "created_at": "2026-09-12T10:00:00.000Z",
        "updated_at": "2026-09-12T12:00:00.000Z",
        "tokens": {"input": 0, "output": out, "cache_read": 0,
                   "cache_creation": 0, "total": out},
    }


@pytest.fixture(autouse=True)
def fleet(tmp_path, monkeypatch):
    """Three sessions: two metered on one project, one on a cloud plan."""
    monkeypatch.setattr(budgets, "_PATH", str(tmp_path / ".budgets.json"))
    monkeypatch.setattr(prices, "PRICES_FILE", str(tmp_path / "no-prices.json"))
    monkeypatch.setattr(prices, "_cache", {"at": 0.0, "data": {}})
    monkeypatch.delenv("BUDGETS_DISABLED", raising=False)
    ledger._session_cache.clear()
    ledger._daily_cache.clear()
    sessions = [
        summary(SID),                                        # $25
        summary("bbb", model="claude-sonnet-5"),             # $10
        summary("ccc", model="glm-5.2:cloud", project="/proj/two"),
    ]
    monkeypatch.setattr(parser, "list_sessions",
                        lambda *a, **k: {"sessions": sessions, "total": len(sessions)})
    monkeypatch.setattr(parser, "_summary_for_id",
                        lambda sid: next((s for s in sessions
                                          if s["session_id"] == sid), None))
    monkeypatch.setattr(ledger, "by_day", lambda sessions, days=30: [
        {"date": "2026-09-12", "usd": 35.0,
         "tokens": dict(ledger.ZERO), "priced": False}])
    return sessions


@pytest.fixture
def client():
    return TestClient(app)


# ---- /api/cost ---------------------------------------------------------------

def test_cost_by_project(client):
    d = client.get("/api/cost?group=project").json()
    rows = {r["key"]: r for r in d["by_project"]}
    assert rows["/proj/one"]["usd"] == pytest.approx(35.0)
    assert rows["/proj/one"]["sessions"] == 2
    assert rows["/proj/two"]["usd"] == 0.0
    assert rows["/proj/two"]["priced"] is False
    assert "by_model" not in d


def test_cost_by_model(client):
    d = client.get("/api/cost?group=model").json()
    rows = {r["key"]: r for r in d["by_model"]}
    assert rows["claude-opus-5"]["usd"] == pytest.approx(25.0)
    assert rows["claude-sonnet-5"]["usd"] == pytest.approx(10.0)
    assert "by_project" not in d


def test_cost_by_day(client):
    d = client.get("/api/cost?group=day&days=7").json()
    assert d["days"] == 7
    assert d["by_day"][0]["date"] == "2026-09-12"
    assert "by_project" not in d


def test_cost_group_all_carries_every_cut(client):
    d = client.get("/api/cost?group=all").json()
    assert {"by_project", "by_model", "by_day", "by_billing"} <= set(d)


def test_cost_totals_and_billing_split(client):
    d = client.get("/api/cost?group=project").json()
    assert d["totals"]["usd"] == pytest.approx(35.0)
    assert d["totals"]["sessions"] == 3
    assert d["totals"]["unpriced_sessions"] == 1
    split = {r["billing"]: r for r in d["by_billing"]}
    assert split["metered"]["sessions"] == 2
    assert split["subscription"]["sessions"] == 1
    assert split["subscription"]["usd"] == 0.0


def test_cost_burn_is_dollars_per_session_hour(client):
    # Three sessions, two hours of wall clock each: $35 over 6 hours.
    d = client.get("/api/cost?group=project").json()
    assert d["burn"] == pytest.approx(35.0 / 6.0)


def test_cost_rejects_an_unknown_grouping(client):
    assert client.get("/api/cost?group=phase-of-the-moon").status_code == 400


def test_cost_carries_the_budget_state(client):
    budgets.set_fleet_cap(500)
    d = client.get("/api/cost").json()
    assert d["budgets"]["fleet_cap"] == 500.0
    assert d["budgets"]["enabled"] is True


# ---- per-session budget ------------------------------------------------------

def test_budget_starts_unset(client):
    d = client.get(f"/api/sessions/{SID}/budget").json()
    assert d["cap"] is None
    assert d["over"] is False
    assert d["usd"] == pytest.approx(25.0)


def test_setting_and_reading_back_a_cap(client):
    assert client.put(f"/api/sessions/{SID}/budget",
                      json={"cap": 40}).json()["cap"] == 40.0
    d = client.get(f"/api/sessions/{SID}/budget").json()
    assert d["cap"] == 40.0
    assert d["over"] is False


def test_a_cap_below_the_spend_reads_as_over(client):
    client.put(f"/api/sessions/{SID}/budget", json={"cap": 10})
    assert client.get(f"/api/sessions/{SID}/budget").json()["over"] is True


def test_clearing_a_cap(client):
    client.put(f"/api/sessions/{SID}/budget", json={"cap": 10})
    assert client.put(f"/api/sessions/{SID}/budget",
                      json={"cap": None}).json()["cap"] is None
    assert budgets.get(SID) is None


def test_a_budget_for_an_unknown_session_is_not_an_error(client):
    # The board can hold an id whose transcript has since gone. Reporting zero
    # beats a 404 the UI would have to special-case.
    d = client.get("/api/sessions/no-such-session/budget").json()
    assert d["cap"] is None and d["usd"] == 0.0 and d["priced"] is False


# ---- fleet budget ------------------------------------------------------------

def test_fleet_cap_round_trip(client):
    assert client.put("/api/budget/fleet", json={"cap": 250}).json()["fleet_cap"] == 250.0
    assert client.get("/api/budget/fleet").json()["fleet_cap"] == 250.0
    assert client.put("/api/budget/fleet", json={"cap": None}).json()["fleet_cap"] is None


# ---- rates -------------------------------------------------------------------

def test_prices_endpoint_shows_the_table(client):
    d = client.get("/api/prices").json()
    assert d["rates"]["claude-opus-5"]["input"] == 5.0
    assert "subscription" in d["classes"]


# ---- the page ----------------------------------------------------------------

def test_the_cost_page_is_served(client):
    r = client.get("/cost.html")
    assert r.status_code == 200
    assert "Cost" in r.text
