"""Unit tests for server.budgets — spend caps and what counts as a breach."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import budgets, ledger, prices  # noqa: E402


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    """A budget file of this test's own. The operator's real caps are never
    touched — the same rule that keeps the title overrides safe."""
    monkeypatch.setattr(budgets, "_PATH", str(tmp_path / ".budgets.json"))
    monkeypatch.delenv("BUDGETS_DISABLED", raising=False)
    ledger._session_cache.clear()
    monkeypatch.setattr(prices, "PRICES_FILE", str(tmp_path / "no-prices.json"))
    monkeypatch.setattr(prices, "_cache", {"at": 0.0, "data": {}})
    return tmp_path


def summary(sid="s1", model="claude-opus-5", out=1_000_000) -> dict:
    return {"session_id": sid, "model": model, "mtime": 1000.0,
            "tokens": {"input": 0, "output": out,
                       "cache_read": 0, "cache_creation": 0, "total": out}}


# ---- reading and writing a cap -----------------------------------------------

def test_no_cap_by_default():
    assert budgets.get("s1") is None
    assert budgets.fleet_cap() is None
    assert budgets.all_caps() == {}


def test_set_and_read_back():
    assert budgets.set_cap("s1", 12.5) == 12.5
    assert budgets.get("s1") == 12.5
    assert budgets.all_caps() == {"s1": 12.5}


def test_clearing_removes_the_cap():
    budgets.set_cap("s1", 12.5)
    budgets.clear("s1")
    assert budgets.get("s1") is None
    assert budgets.all_caps() == {}


def test_a_zero_or_junk_cap_reads_as_no_cap():
    # A zero cap would mean every session is instantly over — that is a
    # foot-gun, not a setting, so it clears instead.
    for bad in (0, -5, "", "abc", None):
        budgets.set_cap("s1", 25.0)
        assert budgets.set_cap("s1", bad) is None
        assert budgets.get("s1") is None


def test_a_string_amount_from_a_form_is_accepted():
    assert budgets.set_cap("s1", "30") == 30.0


def test_fleet_cap_round_trips():
    assert budgets.set_fleet_cap(200) == 200.0
    assert budgets.fleet_cap() == 200.0
    assert budgets.set_fleet_cap(None) is None


def test_a_corrupt_file_reads_as_no_caps(scratch):
    (scratch / ".budgets.json").write_text("{ not json", encoding="utf-8")
    assert budgets.get("s1") is None
    assert budgets.state()["caps"] == {}


def test_the_file_stays_readable(scratch):
    budgets.set_cap("s1", 5)
    budgets.set_fleet_cap(50)
    data = json.loads((scratch / ".budgets.json").read_text(encoding="utf-8"))
    assert data["caps"] == {"s1": 5.0}
    assert data["fleet_cap"] == 50.0


# ---- breach ------------------------------------------------------------------

def test_a_session_under_its_cap_is_not_over():
    budgets.set_cap("s1", 100.0)
    # 1M output on Opus 5 is $25.
    r = budgets.over(summary())
    assert r["usd"] == pytest.approx(25.0)
    assert r["over"] is False
    assert r["cap"] == 100.0


def test_a_session_past_its_cap_is_over():
    budgets.set_cap("s1", 10.0)
    assert budgets.over(summary())["over"] is True


def test_reaching_the_cap_exactly_counts_as_over():
    budgets.set_cap("s1", 25.0)
    assert budgets.over(summary())["over"] is True


def test_a_session_with_no_cap_is_never_over():
    assert budgets.over(summary())["over"] is False


def test_an_unpriced_session_is_never_over():
    # No dollar figure exists for a subscription model, so a cap has nothing
    # to measure — firing would be firing on an invented number.
    budgets.set_cap("s1", 0.01)
    r = budgets.over(summary(model="deepseek-v4-flash:cloud"))
    assert r["priced"] is False
    assert r["over"] is False


def test_over_accepts_a_cost_and_cap_the_caller_already_holds():
    r = budgets.over(summary(), cost={"usd": 99.0, "priced": True}, cap=10.0)
    assert r["over"] is True
    assert r["usd"] == 99.0


def test_fleet_over_compares_the_total():
    assert budgets.fleet_over(500.0) is False    # no cap set
    budgets.set_fleet_cap(100.0)
    assert budgets.fleet_over(99.0) is False
    assert budgets.fleet_over(100.0) is True


# ---- enforcement marks -------------------------------------------------------

def test_a_session_is_only_marked_enforced_once():
    assert budgets.was_enforced("s1") is False
    budgets.mark_enforced("s1")
    first = budgets.state()["enforced"]["s1"]
    budgets.mark_enforced("s1")
    assert budgets.state()["enforced"]["s1"] == first
    assert budgets.was_enforced("s1") is True


def test_raising_the_cap_lets_enforcement_fire_again():
    budgets.set_cap("s1", 10.0)
    budgets.mark_enforced("s1")
    budgets.set_cap("s1", 1000.0)
    assert budgets.was_enforced("s1") is False


def test_fleet_enforcement_toggles():
    assert budgets.fleet_enforced() is False
    budgets.mark_fleet_enforced(True)
    assert budgets.fleet_enforced() is True
    budgets.mark_fleet_enforced(False)
    assert budgets.fleet_enforced() is False


def test_setting_a_fleet_cap_lets_it_fire_again():
    budgets.mark_fleet_enforced(True)
    budgets.set_fleet_cap(300)
    assert budgets.fleet_enforced() is False


# ---- kill switch -------------------------------------------------------------

def test_the_kill_switch_turns_enforcement_off(monkeypatch):
    assert budgets.enabled() is True
    monkeypatch.setenv("BUDGETS_DISABLED", "1")
    assert budgets.enabled() is False
    # The caps themselves stay on file — this switches acting on them off,
    # it does not throw the settings away.
    budgets.set_cap("s1", 10.0)
    assert budgets.get("s1") == 10.0
    assert budgets.state()["enabled"] is False


# ---- rekey -------------------------------------------------------------------

def test_a_cap_follows_a_session_through_a_reset():
    budgets.set_cap("old", 20.0)
    budgets.mark_enforced("old")
    budgets.rekey("old", "new")
    assert budgets.get("new") == 20.0
    assert budgets.get("old") is None
    # The spend went with the old transcript, so the breach did not travel.
    assert budgets.was_enforced("new") is False


def test_rekey_of_a_session_with_no_cap_does_nothing():
    budgets.rekey("old", "new")
    assert budgets.all_caps() == {}
