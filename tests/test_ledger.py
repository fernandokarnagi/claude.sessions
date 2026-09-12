"""Unit tests for server.ledger — cost arithmetic over session summaries."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import ledger, parser, prices  # noqa: E402


@pytest.fixture(autouse=True)
def clean_caches(tmp_path, monkeypatch):
    """No cache carries between tests, and no test reads the real price file."""
    ledger._session_cache.clear()
    ledger._daily_cache.clear()
    monkeypatch.setattr(prices, "PRICES_FILE", str(tmp_path / "no-such-prices.json"))
    monkeypatch.setattr(prices, "_cache", {"at": 0.0, "data": {}})


def summary(sid="s1", model="claude-opus-5", mtime=1000.0, **kw) -> dict:
    """A session summary with only the fields the ledger reads."""
    s = {
        "session_id": sid,
        "model": model,
        "mtime": mtime,
        "project": "/home/me/proj",
        "created_at": "2026-09-12T10:00:00.000Z",
        "updated_at": "2026-09-12T12:00:00.000Z",
        "tokens": {"input": 1_000_000, "output": 1_000_000,
                   "cache_read": 0, "cache_creation": 0, "total": 2_000_000},
    }
    s.update(kw)
    return s


# ---- one session -------------------------------------------------------------

def test_for_session_prices_a_metered_model():
    # Opus 5 is $5 in / $25 out per MTok, so 1M each is $30.
    c = ledger.for_session(summary())
    assert c["priced"] is True
    assert c["billing"] == prices.METERED
    assert c["usd"] == pytest.approx(30.0)


def test_for_session_shows_no_dollars_for_a_subscription_model():
    c = ledger.for_session(summary(model="deepseek-v4-flash:cloud"))
    assert c["priced"] is False
    assert c["usd"] == 0.0
    assert c["billing"] == prices.SUBSCRIPTION


def test_for_session_handles_a_session_with_no_model():
    c = ledger.for_session(summary(model=None))
    assert c["priced"] is False
    assert c["billing"] == prices.UNKNOWN


def test_for_session_caches_on_mtime():
    s = summary()
    first = ledger.for_session(s)
    # Same mtime, different tokens: the cache answers, the tokens are ignored.
    s["tokens"]["output"] = 99_000_000
    assert ledger.for_session(s)["usd"] == first["usd"]
    # A write to the transcript moves mtime, and the cost follows.
    s["mtime"] = 2000.0
    assert ledger.for_session(s)["usd"] > first["usd"]


def test_for_session_survives_junk():
    assert ledger.for_session(None)["priced"] is False
    assert ledger.for_session({})["usd"] == 0.0


# ---- rollup ------------------------------------------------------------------

def test_rollup_groups_by_project_and_model():
    rows = [
        summary("a", project="/proj/one"),
        summary("b", project="/proj/one"),
        summary("c", model="claude-sonnet-5", project="/proj/two"),
    ]
    r = ledger.rollup(rows)
    projects = {p["key"]: p for p in r["by_project"]}
    assert projects["/proj/one"]["sessions"] == 2
    assert projects["/proj/one"]["usd"] == pytest.approx(60.0)
    assert projects["/proj/one"]["tokens"]["total"] == 4_000_000
    # Sonnet 5 is $2/$10, so 1M each is $12.
    assert projects["/proj/two"]["usd"] == pytest.approx(12.0)
    models = {m["key"]: m for m in r["by_model"]}
    assert set(models) == {"claude-opus-5", "claude-sonnet-5"}
    assert r["totals"]["usd"] == pytest.approx(72.0)
    assert r["totals"]["sessions"] == 3


def test_rollup_marks_a_group_unpriced_when_any_member_is():
    rows = [summary("a"), summary("b", model="gemma4:31b")]
    r = ledger.rollup(rows)
    assert r["by_project"][0]["priced"] is False
    assert r["totals"]["unpriced_sessions"] == 1
    # The dollars that ARE known still show; only the flag says they're partial.
    assert r["totals"]["usd"] == pytest.approx(30.0)


def test_rollup_burn_is_dollars_per_session_hour():
    # Two hours of wall clock, $30 spent.
    r = ledger.rollup([summary(mtime=parser._epoch("2026-09-12T12:00:00.000Z"))])
    assert r["totals"]["hours"] == pytest.approx(2.0)
    assert r["burn"] == pytest.approx(15.0)


def test_rollup_burn_is_zero_when_no_wall_clock_is_known():
    r = ledger.rollup([summary(created_at=None, mtime=0.0, updated_at=None)])
    assert r["burn"] == 0.0


def test_rollup_of_nothing_is_empty_not_an_error():
    r = ledger.rollup([])
    assert r["totals"]["usd"] == 0.0
    assert r["by_project"] == []
    assert r["burn"] == 0.0


def test_rollup_orders_by_spend():
    rows = [
        summary("a", model="claude-sonnet-5", project="/cheap"),
        summary("b", project="/dear"),
    ]
    assert [p["key"] for p in ledger.rollup(rows)["by_project"]] == ["/dear", "/cheap"]


# ---- by day ------------------------------------------------------------------

def write_transcript(tmp_path, monkeypatch, sid, events) -> str:
    proj = tmp_path / "-home-me-proj"
    proj.mkdir(exist_ok=True)
    f = proj / f"{sid}.jsonl"
    f.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    monkeypatch.setattr(parser, "PROJECTS_DIR", str(tmp_path))
    return str(f)


def usage_event(ts, model, inp, out):
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant", "model": model,
                        "content": [{"type": "text", "text": "hi"}],
                        "usage": {"input_tokens": inp, "output_tokens": out,
                                  "cache_read_input_tokens": 0,
                                  "cache_creation_input_tokens": 0}}}


def test_daily_buckets_usage_by_calendar_date(tmp_path, monkeypatch):
    path = write_transcript(tmp_path, monkeypatch, "s1", [
        usage_event("2026-09-10T23:00:00.000Z", "claude-opus-5", 1_000_000, 0),
        usage_event("2026-09-11T01:00:00.000Z", "claude-opus-5", 0, 1_000_000),
        usage_event("2026-09-11T02:00:00.000Z", "claude-opus-5", 0, 1_000_000),
    ])
    rows = ledger.daily(path)
    assert [r["date"] for r in rows] == ["2026-09-10", "2026-09-11"]
    assert rows[0]["usd"] == pytest.approx(5.0)
    assert rows[1]["usd"] == pytest.approx(50.0)


def test_daily_prices_each_model_on_its_own_side_of_a_switch(tmp_path, monkeypatch):
    path = write_transcript(tmp_path, monkeypatch, "s2", [
        usage_event("2026-09-11T01:00:00.000Z", "claude-opus-5", 1_000_000, 0),
        usage_event("2026-09-11T02:00:00.000Z", "claude-sonnet-5", 1_000_000, 0),
    ])
    rows = ledger.daily(path)
    assert len(rows) == 1
    assert rows[0]["usd"] == pytest.approx(7.0)   # $5 on Opus + $2 on Sonnet


def test_daily_flags_a_day_that_mixes_priced_and_unpriced_models(tmp_path, monkeypatch):
    path = write_transcript(tmp_path, monkeypatch, "s3", [
        usage_event("2026-09-11T01:00:00.000Z", "claude-opus-5", 1_000_000, 0),
        usage_event("2026-09-11T02:00:00.000Z", "gemma4:31b", 5_000_000, 0),
    ])
    row = ledger.daily(path)[0]
    assert row["priced"] is False
    assert row["usd"] == pytest.approx(5.0)
    assert row["tokens"]["total"] == 6_000_000


def test_daily_ignores_synthetic_turns_as_a_model(tmp_path, monkeypatch):
    path = write_transcript(tmp_path, monkeypatch, "s4", [
        usage_event("2026-09-11T01:00:00.000Z", "<synthetic>", 1_000_000, 0),
    ])
    row = ledger.daily(path)[0]
    assert row["priced"] is False
    assert row["usd"] == 0.0


def test_daily_of_a_missing_file_is_empty(tmp_path):
    assert ledger.daily(str(tmp_path / "gone.jsonl")) == []


def test_daily_caches_until_the_file_moves(tmp_path, monkeypatch):
    path = write_transcript(tmp_path, monkeypatch, "s5", [
        usage_event("2026-09-11T01:00:00.000Z", "claude-opus-5", 1_000_000, 0),
    ])
    first = ledger.daily(path)
    calls = []
    real = parser._iter_events
    monkeypatch.setattr(parser, "_iter_events",
                        lambda p: (calls.append(p), real(p))[1])
    assert ledger.daily(path) == first
    assert calls == []      # served from cache, the file was not reopened


def test_by_day_sums_the_fleet_and_skips_cold_sessions(tmp_path, monkeypatch):
    path = write_transcript(tmp_path, monkeypatch, "s6", [
        usage_event("2026-09-11T01:00:00.000Z", "claude-opus-5", 1_000_000, 0),
    ])
    hot = summary("s6", mtime=os.path.getmtime(path))
    cold = summary("s7", mtime=0.0, updated_at="2020-01-01T00:00:00.000Z")
    rows = ledger.by_day([hot, cold], days=30)
    assert [r["date"] for r in rows] == ["2026-09-11"]
    assert rows[0]["usd"] == pytest.approx(5.0)


def test_by_day_attributes_a_session_with_no_transcript_to_its_last_activity(tmp_path, monkeypatch):
    monkeypatch.setattr(parser, "PROJECTS_DIR", str(tmp_path))
    s = summary("agy-1", mtime=parser._epoch("2026-09-12T12:00:00.000Z"))
    rows = ledger.by_day([s], days=0)
    assert [r["date"] for r in rows] == ["2026-09-12"]
    assert rows[0]["usd"] == pytest.approx(30.0)


def test_path_for_finds_the_transcript(tmp_path, monkeypatch):
    path = write_transcript(tmp_path, monkeypatch, "s8", [])
    assert ledger.path_for("s8") == path
    assert ledger.path_for("nope") is None
    assert ledger.path_for("") is None
