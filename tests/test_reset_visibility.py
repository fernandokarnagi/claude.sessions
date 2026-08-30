"""
A session that was just reset has no turns yet — and has to stay on the board.

grok and opencode both write the session before the conversation, so both hide
empty ones: an abandoned launch would otherwise leave a blank row behind. Reset
lands in exactly that state on purpose (a fresh session, a relaunched REPL, no
turns), and hiding it took the session off the board along with the to-dos and
pins the reset had just carried onto it — they were migrated correctly, with
nowhere to show.

So both parsers keep an empty session the caller vouches for: one with a live
REPL, or one pinned to the To-do inbox. Claude Code needs none of this — its
transcript exists from the session's first line.
"""

import json

import pytest

from server import grokparser, opencodeparser
from tests.test_opencodeparser import SCHEMA, T0, _session, _msg, _part

EMPTY = "ses_fbempty00000000000000000"
USED = "ses_fbused000000000000000000"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An opencode DB with one used session and one that has no messages."""
    import sqlite3
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    _session(conn, USED, title="a used session")
    _session(conn, EMPTY, title="a fresh session")
    _msg(conn, "m1", USED, "user")
    _part(conn, "p1", "m1", USED, 0, {"type": "text", "text": "hello"})
    conn.commit()
    conn.close()
    monkeypatch.setattr(opencodeparser, "db_path", lambda: str(db))
    opencodeparser._SUMM_CACHE.clear()
    return db


def _ids(rows):
    return [r["session_id"] for r in rows]


# --- opencode ---------------------------------------------------------------

def test_an_empty_opencode_session_is_hidden_by_default(store):
    assert _ids(opencodeparser.list_sessions()) == [USED]


def test_an_empty_opencode_session_the_caller_vouches_for_is_shown(store):
    assert set(_ids(opencodeparser.list_sessions(keep_empty={EMPTY}))) == {USED, EMPTY}


def test_vouching_for_an_unknown_id_adds_nothing(store):
    assert _ids(opencodeparser.list_sessions(keep_empty={"ses_nope"})) == [USED]


def test_a_kept_empty_opencode_session_reports_no_steps(store):
    rows = opencodeparser.list_sessions(keep_empty={EMPTY})
    fresh = next(r for r in rows if r["session_id"] == EMPTY)
    assert fresh["step_count"] == 0
    assert fresh["origin"] == "opencode"


# --- grok -------------------------------------------------------------------

@pytest.fixture
def grok_store(tmp_path, monkeypatch):
    """A grok tree with one session that has turns and one with none."""
    root = tmp_path / "sessions" / "%2Fproj"
    for sid, turns in (("sid-used", 4), ("sid-fresh", 0)):
        d = root / sid
        d.mkdir(parents=True)
        (d / "summary.json").write_text(json.dumps({
            "info": {"id": sid, "cwd": "/proj"},
            "created_at": "2026-08-29T10:00:00.000000Z",
            "updated_at": "2026-08-29T10:00:00.000000Z",
            "generated_title": sid,
            "num_messages": turns,
            "current_model_id": "grok-4.6",
        }), encoding="utf-8")
    monkeypatch.setattr(grokparser, "SESS_ROOT", str(tmp_path / "sessions"))
    grokparser._DIR_CACHE.clear()
    grokparser._SUMM_CACHE.clear()
    return root


def test_an_empty_grok_session_is_hidden_by_default(grok_store):
    assert _ids(grokparser.list_sessions()) == ["sid-used"]


def test_an_empty_grok_session_the_caller_vouches_for_is_shown(grok_store):
    got = set(_ids(grokparser.list_sessions(keep_empty={"sid-fresh"})))
    assert got == {"sid-used", "sid-fresh"}
