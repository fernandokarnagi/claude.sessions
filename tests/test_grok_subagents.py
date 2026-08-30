"""
The grok sub-agent roster, scoped to the task in hand.

grok narrates delegation in updates.jsonl — `subagent_spawned` / `subagent_finished`
— and that file keeps every run the session ever made. Listed unscoped, a run
from an hour-old question sat at the top of a panel opened to see what the
session is delegating right now.

Everything here is a fake ~/.grok tree; nothing reads the real one.
"""

import json

import pytest

from server import grokparser

T0 = 1787839400


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _upd(offset, update):
    return {"timestamp": T0 + offset, "method": "session/update",
            "params": {"sessionId": "sid-1", "update": update}}


def _prompt(offset, text="do the next thing"):
    return _upd(offset, {"sessionUpdate": "user_message_chunk",
                         "content": {"type": "text", "text": text}})


def _spawned(offset, aid, desc):
    return _upd(offset, {"sessionUpdate": "subagent_spawned", "subagent_id": aid,
                         "subagent_type": "explore", "description": desc})


def _finished(offset, aid):
    return _upd(offset, {"sessionUpdate": "subagent_finished", "subagent_id": aid,
                         "status": "completed", "turns": 4, "duration_ms": 2000})


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A session dir plus a helper that writes its updates.jsonl."""
    root = tmp_path / "sessions" / "%2Fproj" / "sid-1"
    root.mkdir(parents=True)
    monkeypatch.setattr(grokparser, "SESS_ROOT", str(tmp_path / "sessions"))
    grokparser._DIR_CACHE.clear()
    grokparser._SUB_CACHE.clear()

    def updates(rows):
        _write(root / "updates.jsonl", rows)
        return str(root)

    return updates


def test_a_run_from_an_earlier_task_leaves_the_roster(session):
    d = session([_prompt(0), _spawned(1, "a1", "read the repo"), _finished(9, "a1"),
                 _prompt(100, "now something else")])
    assert grokparser._subagents(d) == []


def test_the_current_tasks_runs_stay(session):
    d = session([_prompt(0), _spawned(1, "a1", "read the repo"), _finished(9, "a1"),
                 _prompt(100), _spawned(101, "a2", "sequence the work"),
                 _finished(120, "a2")])
    assert [r["agent_id"] for r in grokparser._subagents(d)] == ["a2"]


def test_a_run_still_going_outlives_the_next_prompt(session):
    d = session([_prompt(0), _spawned(1, "a1", "read the repo"), _prompt(100)])
    assert [r["agent_id"] for r in grokparser._subagents(d)] == ["a1"]


def test_a_session_that_never_prompted_keeps_what_it_has(session):
    """No boundary to scope to — list the runs rather than hide them all."""
    d = session([_spawned(1, "a1", "read the repo"), _finished(9, "a1")])
    assert [r["agent_id"] for r in grokparser._subagents(d)] == ["a1"]
