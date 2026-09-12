"""The autonomy watcher's budget brake: what a cap actually does to a session."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import autonomy, budgets, ledger, parser, prices  # noqa: E402


def summary(sid, model="claude-opus-5", out=1_000_000) -> dict:
    """A summary with only what the cap check reads. 1M output on Opus 5 is $25."""
    return {"session_id": sid, "model": model, "mtime": 1000.0,
            "project": "/proj", "tokens": {"input": 0, "output": out,
                                           "cache_read": 0, "cache_creation": 0,
                                           "total": out}}


@pytest.fixture(autouse=True)
def fleet(tmp_path, monkeypatch):
    """Two sessions on file, a scratch budget file, a scratch autonomy file.

    The operator's own .autonomy.json and .budgets.json are never touched —
    this suite forces sessions to manual, and doing that to the real file
    would silently disarm a live fleet.
    """
    monkeypatch.setattr(budgets, "_PATH", str(tmp_path / ".budgets.json"))
    monkeypatch.setattr(autonomy, "_PATH", str(tmp_path / ".autonomy.json"))
    monkeypatch.delenv("BUDGETS_DISABLED", raising=False)
    monkeypatch.setattr(prices, "PRICES_FILE", str(tmp_path / "no-prices.json"))
    monkeypatch.setattr(prices, "_cache", {"at": 0.0, "data": {}})
    ledger._session_cache.clear()
    autonomy.set_paused(False)
    autonomy.set_budget_hook(None)
    autonomy._last_budget_check = 0.0

    sessions = [summary("rich"), summary("thrifty", out=1_000)]
    monkeypatch.setattr(parser, "list_sessions",
                        lambda *a, **k: {"sessions": sessions, "total": len(sessions)})
    return sessions


# ---- the brake ---------------------------------------------------------------

def test_nothing_happens_without_caps():
    autonomy.set("rich", "yolo")
    out = autonomy.enforce_budgets()
    assert out["stopped"] == []
    assert out["checked"] == 0        # no caps set: the fleet isn't even read
    assert autonomy.get("rich") == "yolo"


def test_a_session_past_its_cap_drops_to_manual():
    autonomy.set("rich", "yolo")
    budgets.set_cap("rich", 10.0)
    out = autonomy.enforce_budgets()
    assert out["stopped"] == ["rich"]
    assert autonomy.get("rich") == "manual"


def test_a_session_under_its_cap_keeps_its_level():
    autonomy.set("thrifty", "yolo")
    budgets.set_cap("thrifty", 10.0)
    autonomy.enforce_budgets()
    assert autonomy.get("thrifty") == "yolo"


def test_one_session_stopping_leaves_the_others_alone():
    autonomy.set("rich", "yolo")
    autonomy.set("thrifty", "auto-safe")
    budgets.set_cap("rich", 10.0)
    autonomy.enforce_budgets()
    assert autonomy.get("thrifty") == "auto-safe"
    assert autonomy.is_paused() is False


def test_a_breach_only_fires_once():
    autonomy.set("rich", "yolo")
    budgets.set_cap("rich", 10.0)
    autonomy.enforce_budgets()
    # The operator puts it back on yolo, deliberately, without raising the cap.
    autonomy.set("rich", "yolo")
    assert autonomy.enforce_budgets()["stopped"] == []
    assert autonomy.get("rich") == "yolo"


def test_raising_the_cap_arms_it_again():
    autonomy.set("rich", "yolo")
    budgets.set_cap("rich", 10.0)
    autonomy.enforce_budgets()
    budgets.set_cap("rich", 20.0)      # still under the $25 already spent
    autonomy.set("rich", "yolo")
    assert autonomy.enforce_budgets()["stopped"] == ["rich"]
    assert autonomy.get("rich") == "manual"


def test_an_unpriced_session_never_trips_a_cap(fleet):
    fleet[0]["model"] = "deepseek-v4-flash:cloud"
    ledger._session_cache.clear()
    autonomy.set("rich", "yolo")
    budgets.set_cap("rich", 0.01)
    assert autonomy.enforce_budgets()["stopped"] == []
    assert autonomy.get("rich") == "yolo"


# ---- fleet cap ---------------------------------------------------------------

def test_the_fleet_cap_pauses_autonomy():
    budgets.set_fleet_cap(10.0)
    out = autonomy.enforce_budgets()
    assert out["fleet_stopped"] is True
    assert autonomy.is_paused() is True
    # Per-session levels are untouched: the pause is one switch, reversible.
    assert autonomy.get("rich") == "manual"


def test_the_fleet_cap_does_not_re_pause_after_a_manual_resume():
    budgets.set_fleet_cap(10.0)
    autonomy.enforce_budgets()
    autonomy.set_paused(False)         # operator resumes on purpose
    assert autonomy.enforce_budgets()["fleet_stopped"] is False
    assert autonomy.is_paused() is False


def test_a_fleet_under_its_cap_keeps_running():
    budgets.set_fleet_cap(1000.0)
    assert autonomy.enforce_budgets()["fleet_stopped"] is False
    assert autonomy.is_paused() is False


def test_the_fleet_total_is_the_sum_of_the_sessions():
    budgets.set_fleet_cap(1_000_000.0)
    out = autonomy.enforce_budgets()
    assert out["total_usd"] == pytest.approx(25.025)


# ---- kill switch and notifications -------------------------------------------

def test_the_kill_switch_stops_enforcement(monkeypatch):
    monkeypatch.setenv("BUDGETS_DISABLED", "1")
    autonomy.set("rich", "yolo")
    budgets.set_cap("rich", 0.01)
    budgets.set_fleet_cap(0.01)
    out = autonomy.enforce_budgets()
    assert out["stopped"] == []
    assert autonomy.get("rich") == "yolo"
    assert autonomy.is_paused() is False


def test_both_kinds_of_breach_are_notified():
    seen = []
    autonomy.set_budget_hook(lambda sid, event, detail: seen.append((sid, event)))
    budgets.set_cap("rich", 10.0)
    budgets.set_fleet_cap(10.0)
    autonomy.enforce_budgets()
    assert seen == [("rich", "session-cap"), (None, "fleet-cap")]


def test_a_broken_notifier_does_not_stop_the_brake():
    def boom(sid, event, detail):
        raise RuntimeError("slack is down")
    autonomy.set_budget_hook(boom)
    autonomy.set("rich", "yolo")
    budgets.set_cap("rich", 10.0)
    assert autonomy.enforce_budgets()["stopped"] == ["rich"]
    assert autonomy.get("rich") == "manual"


# ---- the watcher's throttle --------------------------------------------------

def test_the_watcher_checks_caps_at_most_once_per_interval(monkeypatch):
    calls = []
    monkeypatch.setattr(autonomy, "enforce_budgets", lambda: calls.append(1))
    autonomy._budget_pass()
    autonomy._budget_pass()
    assert len(calls) == 1
    # Once the interval has passed, it runs again.
    autonomy._last_budget_check -= autonomy.BUDGET_SECS + 1
    autonomy._budget_pass()
    assert len(calls) == 2
