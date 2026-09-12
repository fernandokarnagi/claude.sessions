"""The full-text index: what gets indexed, what changes it, what it finds.

Every provider store is pointed at a scratch directory, so this suite never
reads the operator's transcripts and never writes the real server/.search.db.
"""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import agyparser, grokparser, index, opencodeparser, parser  # noqa: E402


def event(role, text, ts="2026-09-12T10:00:00.000Z") -> str:
    return json.dumps({"type": role, "timestamp": ts,
                       "message": {"role": role, "content": text}})


def transcript(root, sid, lines) -> str:
    d = os.path.join(root, "-proj-one")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{sid}.jsonl")
    header = json.dumps({"type": "summary", "cwd": "/proj/one",
                         "sessionId": sid, "timestamp": "2026-09-12T10:00:00.000Z"})
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header + "\n")
        for line in lines:
            fh.write(line + "\n")
    return path


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    """A fleet of one Claude session; the other three stores are empty."""
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(index, "DB_PATH", str(tmp_path / ".search.db"))
    monkeypatch.setattr(parser, "PROJECTS_DIR", str(projects))
    monkeypatch.setattr(parser, "_summary_cache", {})
    monkeypatch.setattr(agyparser, "CONV_DIR", str(tmp_path / "agy"))
    monkeypatch.setattr(grokparser, "SESS_ROOT", str(tmp_path / "grok"))
    monkeypatch.setattr(opencodeparser, "DATA_DIR", str(tmp_path / "opencode"))
    transcript(str(projects), "sess-one", [
        event("user", "please wire the kafka consumer to the retry topic"),
        event("assistant", "Done — the consumer now retries with a backoff."),
    ])
    return str(projects)


# ---- building ----------------------------------------------------------------

def test_a_first_pass_indexes_every_session():
    out = index.refresh()
    assert out["indexed"] == 1
    assert out["messages"] == 2
    assert index.stats()["sessions"] == 1


def test_an_unchanged_session_is_not_read_twice():
    index.refresh()
    assert index.refresh()["indexed"] == 0


def test_force_rereads_everything():
    index.refresh()
    assert index.refresh(force=True)["indexed"] == 1


def test_a_changed_transcript_is_reindexed(scratch):
    index.refresh()
    transcript(scratch, "sess-one", [
        event("user", "please wire the kafka consumer to the retry topic"),
        event("assistant", "Done — the consumer now retries with a backoff."),
        event("user", "now add a dead letter queue"),
    ])
    assert index.refresh()["indexed"] == 1
    assert index.search("dead letter")["sessions"] == 1


def test_reindexing_replaces_rather_than_appends(scratch):
    """A transcript can be rewritten, not only extended — the old text must go."""
    index.refresh()
    transcript(scratch, "sess-one", [event("user", "something else entirely")])
    index.refresh()
    assert index.search("kafka")["sessions"] == 0
    assert index.stats()["messages"] == 1


def test_a_deleted_transcript_leaves_the_index(scratch):
    index.refresh()
    os.remove(os.path.join(scratch, "-proj-one", "sess-one.jsonl"))
    out = index.refresh()
    assert out["removed"] == 1
    assert index.search("kafka")["sessions"] == 0
    assert index.stats()["sessions"] == 0


def test_limit_slices_a_first_build(scratch):
    for i in range(3):
        transcript(scratch, f"extra-{i}", [event("user", f"message {i}")])
    out = index.refresh(limit=2)
    assert out["indexed"] == 2
    assert out["pending"] == 2          # four sessions on file, two done
    assert index.refresh(limit=2)["pending"] == 0


def test_the_summarizer_s_own_sessions_are_skipped(scratch):
    d = os.path.join(scratch, "-summarizer")
    os.makedirs(d)
    with open(os.path.join(d, "sum-one.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "summary", "cwd": parser.SUMMARIZER_CWD,
                             "sessionId": "sum-one",
                             "timestamp": "2026-09-12T10:00:00.000Z"}) + "\n")
        fh.write(event("user", "internal summarizer chatter") + "\n")
    index.refresh()
    assert index.search("chatter")["sessions"] == 0


def test_an_unreadable_session_does_not_abort_the_pass(scratch, monkeypatch):
    transcript(scratch, "sess-two", [event("user", "the good one")])
    real = index.messages

    def boom(provider, sid, path):
        if sid == "sess-one":
            raise RuntimeError("corrupt")
        return real(provider, sid, path)

    monkeypatch.setattr(index, "messages", boom)
    assert index.refresh()["indexed"] == 1
    assert index.search("good one")["sessions"] == 1


# ---- what a message carries --------------------------------------------------

def test_tool_calls_carry_their_tool_name_as_the_role(scratch):
    blocks = [{"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}]
    transcript(scratch, "sess-tool", [
        json.dumps({"type": "assistant", "timestamp": "2026-09-12T10:00:00.000Z",
                    "message": {"role": "assistant", "content": blocks}})])
    index.refresh()
    hit = index.search("ls")["hits"]["sess-tool"][0]
    assert hit["role"] == "tool:Bash"


def test_a_huge_tool_result_is_clipped(scratch):
    blob = "needle " + ("x" * 50_000)
    blocks = [{"type": "tool_result", "content": blob}]
    transcript(scratch, "sess-big", [
        json.dumps({"type": "user", "timestamp": "2026-09-12T10:00:00.000Z",
                    "message": {"role": "user", "content": blocks}})])
    index.refresh()
    conn = sqlite3.connect(index.db_path())
    try:
        (n,) = conn.execute(
            "SELECT length(text) FROM msgs WHERE session_id = 'sess-big'").fetchone()
    finally:
        conn.close()
    assert n == index.MAX_TOOL_TEXT
    assert index.search("needle")["sessions"] == 1    # the head survives


# ---- searching ---------------------------------------------------------------

def test_a_match_comes_back_with_its_position_and_role():
    index.refresh()
    hits = index.search("kafka")["hits"]["sess-one"]
    assert hits[0]["role"] == "user"
    assert hits[0]["seq"] == 0
    assert hits[0]["provider"] == "claude"


def test_a_snippet_marks_the_match():
    index.refresh()
    snip = index.search("kafka")["hits"]["sess-one"][0]["snippet"]
    assert index.MARK_OPEN + "kafka" + index.MARK_CLOSE in snip


def test_an_empty_query_matches_nothing():
    index.refresh()
    assert index.search("   ")["sessions"] == 0


def test_two_words_both_have_to_match():
    index.refresh()
    assert index.search("kafka consumer")["sessions"] == 1
    assert index.search("kafka giraffe")["sessions"] == 0


def test_a_phrase_in_quotes_matches_in_order():
    index.refresh()
    assert index.search('"retry topic"')["sessions"] == 1
    assert index.search('"topic retry"')["sessions"] == 0


def test_punctuation_does_not_blow_up_the_query():
    """FTS5 rejects bare syntax like `foo(bar` — the fallback treats it as text."""
    index.refresh()
    out = index.search("kafka(consumer")
    assert out["error"] is None
    assert out["sessions"] == 1


def test_per_session_caps_how_many_lines_come_back(scratch):
    transcript(scratch, "sess-many",
               [event("user", f"kafka line {i}") for i in range(10)])
    index.refresh()
    assert len(index.search("kafka", per_session=3)["hits"]["sess-many"]) == 3


def test_limit_caps_how_many_sessions_come_back(scratch):
    for i in range(5):
        transcript(scratch, f"kafka-{i}", [event("user", "kafka everywhere")])
    index.refresh()
    assert index.search("kafka", limit=2)["sessions"] == 2


def test_search_can_be_narrowed_to_a_set_of_sessions(scratch):
    transcript(scratch, "sess-two", [event("user", "kafka again")])
    index.refresh()
    out = index.search("kafka", session_ids={"sess-two"})
    assert out["order"] == ["sess-two"]


def test_searching_before_the_first_build_is_not_an_error():
    assert index.search("kafka")["sessions"] == 0


def test_dropping_the_index_removes_the_file():
    index.refresh()
    assert os.path.exists(index.db_path())
    index.drop()
    assert not os.path.exists(index.db_path())
    assert index.stats()["sessions"] == 0
