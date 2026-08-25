"""
Reading opencode sessions off its SQLite store.

opencode is the one agent on the board that keeps every session in a single
database instead of a file (or directory) per session, so "does this session
exist" is a query and the cache has no mtime to key on — session.time_updated
stands in for it. These tests build a fake opencode.db and never touch the real
~/.local/share/opencode one.
"""

import json
import sqlite3

import pytest

from server import opencodeparser


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
  time_created INTEGER, data TEXT
);
"""

# opencode stamps milliseconds, not seconds.
T0 = 1787000000000


def _session(conn, sid, **kw):
    row = {"id": sid, "directory": "/proj/app", "title": "a opencode session",
           "model": json.dumps({"id": "deepseek-v4-flash:cloud",
                                "providerID": "ollama"}),
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
    conn.execute("INSERT INTO part VALUES (?,?,?,?,?)",
                 (pid, mid, sid, T0 + offset, json.dumps(data)))


@pytest.fixture
def db(tmp_path, monkeypatch):
    """One used session with a full turn, plus a subagent child and an
    untouched session — the two that shouldn't reach the board."""
    monkeypatch.setattr(opencodeparser, "DATA_DIR", str(tmp_path))
    opencodeparser._SUMM_CACHE.clear()
    conn = sqlite3.connect(str(tmp_path / "opencode.db"))
    conn.executescript(SCHEMA)

    _session(conn, "ses_main")
    _session(conn, "ses_child", parent_id="ses_main", title="subagent run")
    _session(conn, "ses_unused", title="never used", time_updated=T0 + 5)

    _msg(conn, "m1", "ses_main", "user")
    _msg(conn, "m2", "ses_main", "assistant")
    _msg(conn, "m3", "ses_child", "assistant")

    _part(conn, "p1", "m1", "ses_main", 1_000,
          {"type": "text", "text": "add a health endpoint"})
    _part(conn, "p2", "m2", "ses_main", 2_000,
          {"type": "step-start"})
    _part(conn, "p3", "m2", "ses_main", 3_000,
          {"type": "reasoning", "text": "check the router first"})
    _part(conn, "p4", "m2", "ses_main", 4_000,
          {"type": "tool", "tool": "read",
           "state": {"status": "completed", "input": {"filePath": "app/api.py"}}})
    _part(conn, "p5", "m2", "ses_main", 5_000,
          {"type": "text", "text": "done — /health returns 200"})
    _part(conn, "p6", "m3", "ses_child", 6_000,
          {"type": "text", "text": "child said this"})
    conn.commit()
    conn.close()
    return tmp_path


def test_has_session_is_a_query_not_a_file_check(db):
    assert opencodeparser.has_session("ses_main")
    assert not opencodeparser.has_session("ses_nope")


def test_board_hides_subagent_and_never_used_sessions(db):
    """A child session belongs to its parent's transcript, and a session with no
    messages has nothing to show — but both stay resolvable by id."""
    ids = [s["session_id"] for s in opencodeparser.list_sessions()]
    assert ids == ["ses_main"]
    assert opencodeparser.get_summary("ses_child")["title"] == "subagent run"
    assert opencodeparser.get_summary("ses_unused") is not None


def test_model_renders_the_way_the_launcher_takes_it(db):
    """session.model is JSON; the dashboard (and --model) want provider/model."""
    assert opencodeparser.get_summary("ses_main")["model"] \
        == "ollama/deepseek-v4-flash:cloud"


def test_summary_carries_the_opencode_origin_and_token_totals(db):
    s = opencodeparser.get_summary("ses_main")
    assert s["origin"] == s["source"] == s["entrypoint"] == "opencode"
    assert s["project"] == "app"
    assert s["tokens"]["total"] == 100 + 20 + 5 + 3
    assert s["step_count"] == 2


def test_milliseconds_become_seconds(db):
    """Everything else on the board speaks epoch seconds."""
    assert opencodeparser.get_summary("ses_main")["mtime"] == (T0 + 60_000) / 1000


def test_text_parts_take_the_role_of_their_parent_message(db):
    """Part rows carry the content but not who said it."""
    acts = opencodeparser.get_session("ses_main")["activities"]
    by_text = {a["text"]: a["kind"] for a in acts}
    assert by_text["add a health endpoint"] == "user"
    assert by_text["done — /health returns 200"] == "assistant"


def test_bookkeeping_parts_are_dropped(db):
    kinds = {a["kind"] for a in opencodeparser.get_session("ses_main")["activities"]}
    assert kinds == {"user", "assistant", "thinking", "tool"}


def test_a_tool_turn_shows_what_it_touched(db):
    tool = next(a for a in opencodeparser.get_session("ses_main")["activities"]
                if a["kind"] == "tool")
    assert tool["name"] == "read"
    assert tool["text"] == "app/api.py"


def test_a_failed_tool_shows_its_error_instead(db):
    conn = sqlite3.connect(str(db / "opencode.db"))
    _part(conn, "p7", "m2", "ses_main", 7_000,
          {"type": "tool", "tool": "bash",
           "state": {"status": "error", "error": "exit 127: no such command",
                     "input": {"command": "frobnicate"}}})
    conn.commit()
    conn.close()
    opencodeparser._SUMM_CACHE.clear()
    texts = [a["text"] for a in opencodeparser.get_session("ses_main")["activities"]]
    assert "exit 127: no such command" in texts


def test_a_question_tool_reads_as_the_question_it_asked(db):
    """It has no command or path to show, so it used to fall through to a raw
    json.dumps of its input — the question, every option and every description
    as one wall of quotes."""
    conn = sqlite3.connect(str(db / "opencode.db"))
    _part(conn, "p8", "m2", "ses_main", 8_000,
          {"type": "tool", "tool": "question",
           "state": {"status": "completed", "input": {"questions": [
               {"header": "Explore User node",
                "question": "Want me to trace it?",
                "options": [
                    {"label": "Yes, trace it", "description": "Walk the graph"},
                    {"label": "No, just show the report",
                     "description": "Keep to the summary"}]}]}}})
    conn.commit()
    conn.close()
    opencodeparser._SUMM_CACHE.clear()
    texts = [a["text"] for a in opencodeparser.get_session("ses_main")["activities"]]
    asked = next(t for t in texts if "trace it" in t)
    assert asked == "Want me to trace it?\n1. Yes, trace it  2. No, just show the report"
    assert "{" not in asked


def test_injected_editor_context_is_not_the_users_words(db):
    """opencode rides IDE context on the user's message as an extra text part —
    rendering it as something the human typed would be a lie."""
    conn = sqlite3.connect(str(db / "opencode.db"))
    _part(conn, "p8", "m1", "ses_main", 900,
          {"type": "text", "text": "Called the Read tool", "synthetic": True})
    _part(conn, "p9", "m1", "ses_main", 950,
          {"type": "text", "text": "open file: api.py",
           "metadata": {"kind": "editor_context"}})
    conn.commit()
    conn.close()
    opencodeparser._SUMM_CACHE.clear()
    texts = [a["text"] for a in opencodeparser.get_session("ses_main")["activities"]]
    assert "Called the Read tool" not in texts
    assert "open file: api.py" not in texts


def test_detail_is_newest_first_and_the_board_preview_is_chronological(db):
    """The history view reads down from the latest turn; the card preview reads
    like a conversation. Same convention as every other parser."""
    detail = opencodeparser.get_session("ses_main")["activities"]
    assert detail[0]["text"] == "done — /health returns 200"
    preview = opencodeparser.get_summary("ses_main")["last_activities"]
    assert preview[-1]["text"] == "done — /health returns 200"


def test_the_board_preview_is_capped(db):
    assert len(opencodeparser.get_summary("ses_main")["last_activities"]) \
        <= opencodeparser._MAX_ACT


def test_the_cache_is_keyed_on_time_updated(db):
    """There's no per-session file to stat, so a write has to move time_updated
    for the summary to refresh."""
    first = opencodeparser.get_summary("ses_main")
    conn = sqlite3.connect(str(db / "opencode.db"))
    conn.execute("UPDATE session SET title = 'renamed' WHERE id = 'ses_main'")
    conn.commit()
    assert opencodeparser.get_summary("ses_main")["title"] == first["title"]
    conn.execute("UPDATE session SET time_updated = ? WHERE id = 'ses_main'",
                 (T0 + 120_000,))
    conn.commit()
    conn.close()
    assert opencodeparser.get_summary("ses_main")["title"] == "renamed"


def test_usage_is_built_from_the_store_not_a_repl_scrape(db):
    r = opencodeparser.usage_text("ses_main")
    assert r["ok"]
    assert "ollama/deepseek-v4-flash:cloud" in r["text"]
    assert "1 user / 1 assistant" in r["text"]
    assert "Tool calls:       1" in r["text"]


def test_usage_reports_an_unknown_session(db):
    assert opencodeparser.usage_text("ses_nope") == {
        "ok": False, "error": "opencode session not found"}


def test_a_missing_database_is_not_an_error(tmp_path, monkeypatch):
    """Nobody has opencode installed on a fresh machine — the board just shows
    no opencode sessions rather than failing to load."""
    monkeypatch.setattr(opencodeparser, "DATA_DIR", str(tmp_path / "nothing"))
    opencodeparser._SUMM_CACHE.clear()
    assert opencodeparser.list_sessions() == []
    assert opencodeparser.session_ids() == set()
    assert not opencodeparser.has_session("ses_main")
    assert opencodeparser.get_session("ses_main") is None


def test_the_store_is_only_ever_opened_read_only(db):
    """opencode holds this file open in WAL mode while a session runs; a poll
    loop that took a write lock would stall the agent."""
    conn = opencodeparser._connect()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE session SET title = 'x'")
    conn.close()
