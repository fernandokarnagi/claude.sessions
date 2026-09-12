"""The index covers all four providers, not just Claude.

Each store has its own shape — a JSONL file, a directory, a database per
conversation, one database for everything — and each one is built here from
scratch. Nothing reads the operator's real history.
"""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import agyparser, grokparser, index, opencodeparser, parser  # noqa: E402

OPENCODE_SCHEMA = """
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

T0 = 1787000000000        # opencode stamps milliseconds


def write_grok(root, sid, lines):
    d = root / "sessions" / "%2Fproj" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps({
        "info": {"id": sid, "cwd": "/proj"},
        "created_at": "2026-09-12T10:00:00.000000Z",
        "updated_at": "2026-09-12T10:20:00.000000Z",
        "generated_title": "a grok session",
        "num_messages": len(lines),
        "current_model_id": "grok-4.6",
    }), encoding="utf-8")
    (d / "chat_history.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8")
    return d


def write_agy(root, cid, steps):
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(root / f"{cid}.db"))
    conn.execute("CREATE TABLE steps (idx INTEGER, step_type INTEGER, "
                 "step_payload BLOB)")
    conn.executemany("INSERT INTO steps VALUES (?,?,?)", steps)
    conn.commit()
    conn.close()


def write_opencode(root, sid, parts):
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(root / "opencode.db"))
    conn.executescript(OPENCODE_SCHEMA)
    conn.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, "/proj/app", "an opencode session",
         json.dumps({"id": "deepseek-v4-flash:cloud", "providerID": "ollama"}),
         "build", 0.0, 10, 10, 0, 0, T0, T0 + 60_000, None))
    conn.execute("INSERT INTO message VALUES (?,?,?)",
                 ("m1", sid, json.dumps({"role": "user"})))
    for i, data in enumerate(parts):
        conn.execute("INSERT INTO part VALUES (?,?,?,?,?)",
                     (f"p{i}", "m1", sid, T0 + i * 1000, json.dumps(data)))
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def stores(tmp_path, monkeypatch):
    """One session per provider, all four stores empty to start with."""
    monkeypatch.setattr(index, "DB_PATH", str(tmp_path / ".search.db"))
    monkeypatch.setattr(parser, "PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setattr(agyparser, "CONV_DIR", str(tmp_path / "agy"))
    monkeypatch.setattr(grokparser, "SESS_ROOT", str(tmp_path / "grok" / "sessions"))
    monkeypatch.setattr(opencodeparser, "DATA_DIR", str(tmp_path / "opencode"))
    agyparser._SUMM_CACHE.clear()
    grokparser._DIR_CACHE.clear()
    grokparser._SUMM_CACHE.clear()
    grokparser._TS_CACHE.clear()
    opencodeparser._SUMM_CACHE.clear()
    os.makedirs(tmp_path / "projects", exist_ok=True)
    return tmp_path


# ---- grok --------------------------------------------------------------------

def test_a_grok_session_is_indexed(stores):
    write_grok(stores / "grok", "grok-1", [
        {"type": "user", "content": "check the nginx timeout", "prompt_index": 0},
        {"type": "assistant", "content": "raised it to 60 seconds"},
    ])
    assert index.refresh()["indexed"] == 1
    out = index.search("nginx")
    assert out["order"] == ["grok-1"]
    assert out["hits"]["grok-1"][0]["provider"] == "grok"


def test_a_grok_session_nested_two_levels_down_is_found(stores):
    """The session directory is sessions/<encoded cwd>/<id>, not sessions/<id>."""
    write_grok(stores / "grok", "grok-1", [
        {"type": "user", "content": "nginx again", "prompt_index": 0}])
    assert "grok-1" in index.sources()


def test_a_changed_grok_log_is_reindexed(stores):
    write_grok(stores / "grok", "grok-1", [
        {"type": "user", "content": "first ask", "prompt_index": 0}])
    index.refresh()
    write_grok(stores / "grok", "grok-1", [
        {"type": "user", "content": "first ask", "prompt_index": 0},
        {"type": "assistant", "content": "the postgres pool was exhausted"}])
    assert index.refresh()["indexed"] == 1
    assert index.search("postgres")["order"] == ["grok-1"]


# ---- agy ---------------------------------------------------------------------

def test_an_agy_conversation_is_indexed(stores):
    # agy stores protobuf-ish blobs; the parser lifts printable runs out of them.
    write_agy(stores / "agy", "agy-1", [
        (0, 14, b"\x0a\x20migrate the redis cache to cluster mode"),
        (1, 15, b"\x0a\x20the cluster is live and the keys moved"),
    ])
    assert index.refresh()["indexed"] == 1
    out = index.search("redis")
    assert out["order"] == ["agy-1"]
    assert out["hits"]["agy-1"][0]["provider"] == "agy"


def test_an_agy_conversation_keeps_reading_order(stores):
    """The detail view is newest-first; the index stores the order it was said."""
    write_agy(stores / "agy", "agy-1", [
        (0, 14, b"\x0a\x20the first thing that was asked"),
        (1, 15, b"\x0a\x20the second thing that was said"),
    ])
    index.refresh()
    first = index.search("first thing")["hits"]["agy-1"][0]
    second = index.search("second thing")["hits"]["agy-1"][0]
    assert first["seq"] < second["seq"]


# ---- opencode ----------------------------------------------------------------

def test_an_opencode_session_is_indexed(stores):
    write_opencode(stores / "opencode", "ses_one", [
        {"type": "text", "text": "add a kubernetes liveness probe"},
        {"type": "reasoning", "text": "the deployment manifest needs it"},
    ])
    assert index.refresh()["indexed"] == 1
    out = index.search("kubernetes")
    assert out["order"] == ["ses_one"]
    assert out["hits"]["ses_one"][0]["provider"] == "opencode"


def test_opencode_sessions_are_stamped_one_by_one(stores):
    """They share one database file, so a per-file stamp would mark every
    session dirty whenever any of them was written to."""
    write_opencode(stores / "opencode", "ses_one", [
        {"type": "text", "text": "add a kubernetes liveness probe"}])
    index.refresh()
    conn = sqlite3.connect(str(stores / "opencode" / "opencode.db"))
    conn.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("ses_two", "/proj/app", "another", "{}", "build", 0.0,
                  0, 0, 0, 0, T0, T0 + 90_000, None))
    conn.execute("INSERT INTO message VALUES (?,?,?)",
                 ("m2", "ses_two", json.dumps({"role": "user"})))
    conn.execute("INSERT INTO part VALUES (?,?,?,?,?)",
                 ("px", "m2", "ses_two", T0 + 2000,
                  json.dumps({"type": "text", "text": "restart the ingress"})))
    conn.commit()
    conn.close()
    # Only the new session is re-read, even though the file itself changed.
    assert index.refresh()["indexed"] == 1
    assert index.search("ingress")["order"] == ["ses_two"]


# ---- all together ------------------------------------------------------------

def test_one_query_reaches_every_provider(stores):
    root = stores
    d = os.path.join(str(root / "projects"), "-proj-one")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "claude-1.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "summary", "cwd": "/proj/one",
                             "sessionId": "claude-1",
                             "timestamp": "2026-09-12T10:00:00.000Z"}) + "\n")
        fh.write(json.dumps({"type": "user", "timestamp": "2026-09-12T10:00:00.000Z",
                             "message": {"role": "user",
                                         "content": "the deploy pipeline"}}) + "\n")
    write_grok(root / "grok", "grok-1", [
        {"type": "user", "content": "the deploy pipeline", "prompt_index": 0}])
    write_agy(root / "agy", "agy-1",
              [(0, 14, b"\x0a\x20fix the deploy pipeline please")])
    write_opencode(root / "opencode", "ses_one",
                   [{"type": "text", "text": "the deploy pipeline is red"}])
    index.refresh()
    out = index.search("deploy pipeline")
    assert set(out["order"]) == {"claude-1", "grok-1", "agy-1", "ses_one"}
    assert {h[0]["provider"] for h in out["hits"].values()} == {
        "claude", "grok", "agy", "opencode"}


def test_a_provider_with_no_store_is_simply_absent(stores):
    """Three of the four are not installed here — refresh must not care."""
    assert index.refresh() == {"indexed": 0, "removed": 0, "messages": 0,
                               "sessions": 0, "pending": 0,
                               "took": pytest.approx(0, abs=5)}
