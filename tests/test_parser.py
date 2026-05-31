"""Unit tests for server.parser, run against a synthetic transcript fixture."""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import parser  # noqa: E402


# ---- fixture -----------------------------------------------------------------

EVENTS = [
    {"type": "user", "timestamp": "2026-05-31T10:00:00.000Z", "cwd": "/home/me/proj",
     "message": {"role": "user", "content": "first prompt"}},
    {"type": "ai-title", "aiTitle": "My Test Session"},
    {"type": "assistant", "timestamp": "2026-05-31T10:00:05.000Z",
     "message": {"role": "assistant", "model": "claude-opus-4-8",
                 "content": [
                     {"type": "thinking", "thinking": "let me think"},
                     {"type": "text", "text": "here is my answer"},
                     {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                 ],
                 "usage": {"input_tokens": 100, "output_tokens": 20,
                           "cache_read_input_tokens": 50, "cache_creation_input_tokens": 10}}},
    {"type": "user", "timestamp": "2026-05-31T10:00:06.000Z",
     "message": {"role": "user", "content": [
         {"type": "tool_result", "tool_use_id": "x", "content": "file1\nfile2"}]}},
    {"type": "assistant", "timestamp": "2026-05-31T10:00:08.000Z", "entrypoint": "claude-vscode",
     "message": {"role": "assistant", "model": "claude-opus-4-8",
                 "content": [{"type": "text", "text": "done"}],
                 "usage": {"input_tokens": 200, "output_tokens": 30,
                           "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}},
    "{ this is a broken json line",  # must be skipped, not crash
]


@pytest.fixture
def transcript(tmp_path, monkeypatch):
    proj = tmp_path / "-home-me-proj"
    proj.mkdir()
    f = proj / "abc123.jsonl"
    lines = []
    for e in EVENTS:
        lines.append(e if isinstance(e, str) else json.dumps(e))
    f.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(parser, "PROJECTS_DIR", str(tmp_path))
    parser._summary_cache.clear()
    return f


# ---- status ------------------------------------------------------------------

def test_status_thinking():
    now = 1_000_000.0
    assert parser.compute_status(now - 3, now) == "THINKING"        # < 10s

def test_status_waiting():
    now = 1_000_000.0
    assert parser.compute_status(now - 120, now) == "WAITING"       # 2 min

def test_status_sitting():
    now = 1_000_000.0
    assert parser.compute_status(now - 3600, now) == "SITTING"      # 1 h (30min..2h)

def test_status_sleeping():
    now = 1_000_000.0
    assert parser.compute_status(now - 6 * 3600, now) == "SLEEPING" # 6 h (2h..24h)

def test_status_ended():
    now = 1_000_000.0
    assert parser.compute_status(now - 30 * 3600, now) == "ENDED"   # 30 h (>24h)

def test_status_tier_boundaries():
    now = 1_000_000.0
    assert parser.compute_status(now - 29, now) == "THINKING"       # just under 30s
    assert parser.compute_status(now - 30, now) == "WAITING"        # exactly 30s -> WAITING
    assert parser.compute_status(now - 30 * 60, now) == "SITTING"   # exactly 30min -> next tier
    assert parser.compute_status(now - 2 * 3600, now) == "SLEEPING" # exactly 2h -> next tier
    assert parser.compute_status(now - 24 * 3600, now) == "ENDED"   # exactly 24h -> ENDED


# ---- summaries ---------------------------------------------------------------

def test_list_sessions_status_filter(transcript, monkeypatch):
    # fixture session is old (2026 timestamps) -> ENDED; filter should respect it
    s = parser.list_sessions()["sessions"][0]
    st = s["status"]
    # filtering to its own status keeps it
    assert parser.list_sessions(statuses={st})["total"] == 1
    # filtering to a different status excludes it
    other = "THINKING" if st != "THINKING" else "WAITING"
    assert parser.list_sessions(statuses={other})["total"] == 0


def test_list_sessions_basic(transcript):
    out = parser.list_sessions()
    assert out["total"] == 1
    s = out["sessions"][0]
    assert s["session_id"] == "abc123"
    assert s["title"] == "My Test Session"
    assert s["project"] == "/home/me/proj"
    assert s["model"] == "claude-opus-4-8"
    assert s["created_at"] == "2026-05-31T10:00:00.000Z"

def test_entrypoint_captured(transcript):
    s = parser.list_sessions()["sessions"][0]
    assert s["entrypoint"] == "claude-vscode"  # last seen entrypoint wins
    assert s["cwd"] == "/home/me/proj"


def test_token_totals(transcript):
    s = parser.list_sessions()["sessions"][0]
    t = s["tokens"]
    assert t["input"] == 300
    assert t["output"] == 50
    assert t["cache_read"] == 50
    assert t["cache_creation"] == 10
    assert t["total"] == 410

def test_last_two_activities(transcript):
    s = parser.list_sessions()["sessions"][0]
    acts = s["last_activities"]
    assert len(acts) == 2  # only the final two renderable events
    assert acts[-1]["kind"] == "assistant"
    assert "done" in acts[-1]["text"]

def test_title_fallback_to_first_user_message(tmp_path, monkeypatch):
    proj = tmp_path / "-x"; proj.mkdir()
    (proj / "noTitle.jsonl").write_text(json.dumps({
        "type": "user", "timestamp": "2026-05-31T10:00:00.000Z",
        "message": {"role": "user", "content": "hello world"}}) + "\n")
    monkeypatch.setattr(parser, "PROJECTS_DIR", str(tmp_path))
    parser._summary_cache.clear()
    s = parser.list_sessions()["sessions"][0]
    assert s["title"] == "hello world"


# ---- detail ------------------------------------------------------------------

def test_search_by_session_id(transcript):
    out = parser.search_sessions("abc")
    assert out["total"] == 1
    assert out["sessions"][0]["session_id"] == "abc123"

def test_search_by_path(transcript):
    out = parser.search_sessions("/home/me")
    assert out["total"] == 1
    assert out["sessions"][0]["cwd"] == "/home/me/proj"

def test_search_by_title_case_insensitive(transcript):
    out = parser.search_sessions("TEST session")
    assert out["total"] == 1

def test_search_empty_returns_nothing(transcript):
    assert parser.search_sessions("")["total"] == 0
    assert parser.search_sessions("   ")["total"] == 0

def test_search_no_match(transcript):
    assert parser.search_sessions("zzz-nomatch")["total"] == 0

def test_search_wildcard_star(transcript):
    # title is "My Test Session"
    assert parser.search_sessions("*test*")["total"] == 1
    assert parser.search_sessions("my test*")["total"] == 1
    assert parser.search_sessions("*nope*")["total"] == 0

def test_search_wildcard_question(transcript):
    # "abc123" -> "abc12?" matches the 6-char id field
    assert parser.search_sessions("abc12?")["total"] == 1

def test_search_wildcard_is_full_field_match(transcript):
    # bare wildcard-less word is 'contains'; with wildcard it's a glob on the
    # whole field, so 'test' alone (no *) still matches via substring...
    assert parser.search_sessions("test")["total"] == 1
    # ...but 'test*' (anchored at start) does NOT match "my test session"
    assert parser.search_sessions("test*")["total"] == 0

def test_search_by_override_title(transcript):
    # renamed title is searchable when passed via extra_titles
    out = parser.search_sessions("renamed", extra_titles={"abc123": "Renamed Thing"})
    assert out["total"] == 1
    out2 = parser.search_sessions("*thing", extra_titles={"abc123": "Renamed Thing"})
    assert out2["total"] == 1


def test_get_session_desc_order(transcript):
    d = parser.get_session("abc123")
    assert d is not None
    acts = d["activities"]
    # newest first: last text block "done" should precede the first user prompt
    kinds = [a["kind"] for a in acts]
    assert kinds[0] == "assistant" and acts[0]["text"] == "done"
    assert acts[-1]["kind"] == "user" and acts[-1]["text"] == "first prompt"
    # tool_use block carries its name
    assert any(a.get("name") == "Bash" for a in acts)

def test_get_session_missing(transcript):
    assert parser.get_session("nope") is None


# ---- cache -------------------------------------------------------------------

def test_cache_reused_until_mtime_changes(transcript, monkeypatch):
    calls = {"n": 0}
    real = parser._build_summary
    def counting(path):
        calls["n"] += 1
        return real(path)
    monkeypatch.setattr(parser, "_build_summary", counting)
    parser._summary_cache.clear()

    parser.list_sessions(); parser.list_sessions()
    assert calls["n"] == 1  # second call served from cache

    # touch the file -> mtime changes -> re-parse
    os.utime(transcript, (time.time(), time.time() + 5))
    parser.list_sessions()
    assert calls["n"] == 2
