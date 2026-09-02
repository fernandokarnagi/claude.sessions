"""
Resolving an opencode sub-agent run to the turns it actually wrote.

opencode stores a run as a whole child session rather than as a file beside the
parent's transcript, and it names that child inside the spawning `task` part at
state.metadata.sessionId. Two things went wrong with that:

  * The roster paired parts to children positionally. A task that fails before
    spawning anything ("Unknown agent type: …") has no metadata and no child, so
    the walk handed it the *next* run's session and left that run with none —
    two wrong rows from one failure.
  * The transcript endpoint only knew Claude Code's on-disk layout, so every
    opencode run 404'd and the UI reported the transcript as gone.

These build a fake opencode.db; nothing touches the real store.
"""

import json
import sqlite3

import pytest

from server import opencodeparser

# Its own schema rather than test_opencodeparser's: the roster reads
# part.time_updated (a run's last write), which that fixture's table omits.
# Without the column the query fails and every run silently disappears — which
# is exactly the failure these tests exist to catch, so it must not be faked.
SCHEMA = """
CREATE TABLE session (
  id TEXT PRIMARY KEY, directory TEXT, title TEXT, model TEXT, agent TEXT,
  cost REAL, tokens_input INTEGER, tokens_output INTEGER,
  tokens_cache_read INTEGER, tokens_cache_write INTEGER,
  time_created INTEGER, time_updated INTEGER, parent_id TEXT
);
CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT);
CREATE TABLE part (
  id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
  time_created INTEGER, time_updated INTEGER, data TEXT
);
"""

T0 = 1787000000000


def _session(conn, sid, **kw):
    row = {"id": sid, "directory": "/proj/app", "title": "a opencode session",
           "model": json.dumps({"id": "mimo-v2.5", "providerID": "opencode-go"}),
           "agent": "build", "cost": 0.0125,
           "tokens_input": 100, "tokens_output": 20,
           "tokens_cache_read": 5, "tokens_cache_write": 3,
           "time_created": T0, "time_updated": T0 + 60_000, "parent_id": None}
    row.update(kw)
    conn.execute(
        "INSERT INTO session VALUES (:id,:directory,:title,:model,:agent,:cost,"
        ":tokens_input,:tokens_output,:tokens_cache_read,:tokens_cache_write,"
        ":time_created,:time_updated,:parent_id)", row)


def _msg(conn, mid, sid, role):
    conn.execute("INSERT INTO message VALUES (?,?,?)",
                 (mid, sid, json.dumps({"role": role})))


def _part(conn, pid, mid, sid, offset, data):
    conn.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                 (pid, mid, sid, T0 + offset, T0 + offset, json.dumps(data)))


def _task(conn, pid, sid, offset, call_id, desc, *,
          child=None, status="completed", error=None, agent="general"):
    """A `task` part on the parent — the row a sub-agent run is listed from."""
    state = {"status": status, "input": {"description": desc,
                                         "subagent_type": agent}}
    if child:
        state["metadata"] = {"parentSessionId": sid, "sessionId": child}
    if error:
        state["error"] = error
    _part(conn, pid, "m_task", sid, offset,
          {"type": "tool", "tool": "task", "callID": call_id, "state": state})


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A parent with three runs, the middle one failing before it spawned.

    This is the shape that broke positional pairing: parts at 1s/2s/3s but only
    two children, and the child born last belongs to the part filed last.
    """
    monkeypatch.setattr(opencodeparser, "DATA_DIR", str(tmp_path))
    opencodeparser._SUMM_CACHE.clear()
    conn = sqlite3.connect(str(tmp_path / "opencode.db"))
    conn.executescript(SCHEMA)

    _session(conn, "ses_main")
    _session(conn, "ses_kid1", parent_id="ses_main", time_created=T0 + 1_500)
    _session(conn, "ses_kid2", parent_id="ses_main", time_created=T0 + 3_500)
    _msg(conn, "m_task", "ses_main", "assistant")
    _msg(conn, "m_k1", "ses_kid1", "assistant")
    _msg(conn, "m_k2", "ses_kid2", "assistant")

    _task(conn, "t1", "ses_main", 1_000, "call_one", "chunk 1", child="ses_kid1")
    _task(conn, "t2", "ses_main", 2_000, "call_two", "chunk 2", status="error",
          error="Unknown agent type: general-purpose is not a valid agent type",
          agent="general-purpose")
    _task(conn, "t3", "ses_main", 3_000, "call_three", "chunk 3", child="ses_kid2")

    _part(conn, "k1a", "m_k1", "ses_kid1", 1_600, {"type": "text", "text": "kid one ran"})
    _part(conn, "k2a", "m_k2", "ses_kid2", 3_600, {"type": "text", "text": "kid two ran"})
    conn.commit()
    conn.close()
    return tmp_path


# --- pairing ----------------------------------------------------------------

def test_each_run_points_at_the_child_its_own_part_names(db):
    runs = {r["agent_id"]: r["child_session_id"]
            for r in opencodeparser.subagents("ses_main")}
    assert runs == {"call_one": "ses_kid1",
                    "call_two": None,        # errored before spawning
                    "call_three": "ses_kid2"}


def test_a_failed_run_does_not_consume_the_next_runs_child(db):
    """The positional-pairing bug, pinned.

    Walking both lists in order gave call_two the child that belongs to
    call_three, so one row showed another run's transcript and the other showed
    none. Both halves are asserted because either alone still passes the bug.
    """
    runs = {r["agent_id"]: r for r in opencodeparser.subagents("ses_main")}
    assert runs["call_two"]["child_session_id"] is None
    assert runs["call_three"]["child_session_id"] == "ses_kid2"


def test_the_failure_reason_reaches_the_roster(db):
    runs = {r["agent_id"]: r for r in opencodeparser.subagents("ses_main")}
    assert "Unknown agent type" in runs["call_two"]["error"]
    assert runs["call_one"]["error"] is None


# --- transcripts ------------------------------------------------------------

def test_a_run_expands_into_its_child_sessions_turns(db):
    run = opencodeparser.subagent_transcript("ses_main", "call_one")
    assert run["child_session_id"] == "ses_kid1"
    assert [a["text"] for a in run["activities"]] == ["kid one ran"]
    assert run["total"] == 1 and run["truncated"] is False


def test_the_last_run_reads_its_own_turns_not_the_failed_ones(db):
    run = opencodeparser.subagent_transcript("ses_main", "call_three")
    assert [a["text"] for a in run["activities"]] == ["kid two ran"]


def test_a_run_that_never_spawned_resolves_with_its_error(db):
    """Not a 404: the run is real, it just has nothing to replay."""
    run = opencodeparser.subagent_transcript("ses_main", "call_two")
    assert run is not None
    assert run["activities"] == [] and run["total"] == 0
    assert "Unknown agent type" in run["error"]


def test_an_unknown_run_is_absent(db):
    assert opencodeparser.subagent_transcript("ses_main", "call_nope") is None


def test_truncation_reports_the_full_count(db):
    run = opencodeparser.subagent_transcript("ses_main", "call_one", limit=0)
    assert run["total"] == 1 and run["truncated"] is True


# --- the current task -------------------------------------------------------
#
# A session lives for days and every `task` part it ever ran stays in the store,
# so the roster grew into a log: a run from yesterday's question sat at the top
# of a panel opened to answer "what is it delegating now?". The roster is scoped
# to the newest task — everything since the last user prompt — and older runs
# stay reachable by id.

def _prompt(conn, mid, sid, offset, text="do the next thing"):
    """A user turn on the parent — where the current task begins."""
    _msg(conn, mid, sid, "user")
    _part(conn, mid + "_p", mid, sid, offset, {"type": "text", "text": text})


def _reopen(db):
    return sqlite3.connect(str(db / "opencode.db"))


def test_runs_from_an_earlier_task_leave_the_roster(db):
    conn = _reopen(db)
    _prompt(conn, "m_user2", "ses_main", 10_000)
    conn.commit()
    conn.close()
    assert opencodeparser.subagents("ses_main") == []


def test_the_current_tasks_own_runs_stay(db):
    conn = _reopen(db)
    _prompt(conn, "m_user2", "ses_main", 10_000)
    _task(conn, "t4", "ses_main", 11_000, "call_four", "chunk 4", child="ses_kid2")
    conn.commit()
    conn.close()
    assert [r["agent_id"] for r in opencodeparser.subagents("ses_main")] == ["call_four"]


def test_a_run_still_going_outlives_the_prompt_that_started_it(db):
    """A live run is never old news, whatever turn it was spawned in."""
    conn = _reopen(db)
    _task(conn, "t4", "ses_main", 4_000, "call_four", "chunk 4", status="running")
    _prompt(conn, "m_user2", "ses_main", 10_000)
    conn.commit()
    conn.close()
    assert [r["agent_id"] for r in opencodeparser.subagents("ses_main")] == ["call_four"]


def test_an_earlier_run_is_still_readable_by_id(db):
    """Scoping the roster hides a run from the list, not from the dashboard."""
    conn = _reopen(db)
    _prompt(conn, "m_user2", "ses_main", 10_000)
    conn.commit()
    conn.close()
    run = opencodeparser.subagent_transcript("ses_main", "call_one")
    assert run["child_session_id"] == "ses_kid1"
