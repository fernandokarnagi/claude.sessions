"""Sub-agent runs: detection, the status they earn, and the roster they expose.

The bug these guard against: Claude Code parks a sub-agent's turns in a sibling
directory and leaves the main transcript untouched for the whole run, so a
session delegating heavy work looked idle and decayed to WAITING within 30s.
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import parser, subagents  # noqa: E402
from server.app import _apply_delegating  # noqa: E402


def _write(path, events):
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _main_events(agent_calls=(), resolved=()):
    """A transcript that dispatches `agent_calls` and gets results for `resolved`."""
    events = [
        {"type": "user", "timestamp": "2026-08-25T10:00:00.000Z", "cwd": "/home/me/proj",
         "message": {"role": "user", "content": "do the big thing"}},
    ]
    for i, tid in enumerate(agent_calls):
        events.append({
            "type": "assistant", "timestamp": f"2026-08-25T10:00:0{i + 1}.000Z",
            "message": {"role": "assistant", "model": "claude-opus-5", "content": [
                {"type": "tool_use", "id": tid, "name": "Agent",
                 "input": {"subagent_type": "Explore", "description": f"probe {tid}"}}]}})
    for tid in resolved:
        events.append({
            "type": "user", "timestamp": "2026-08-25T10:05:00.000Z",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": "report"}]}})
    return events


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A transcript plus a helper to plant sub-agent files beside it."""
    proj = tmp_path / "-home-me-proj"
    proj.mkdir()
    path = proj / "sess1.jsonl"
    monkeypatch.setattr(parser, "PROJECTS_DIR", str(tmp_path))
    parser._summary_cache.clear()
    subagents._meta_cache.clear()

    def plant(agent_id, tool_use_id, agent_type="Explore", desc="probe", turns=2):
        d = proj / "sess1" / "subagents"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{agent_id}.meta.json").write_text(json.dumps(
            {"agentType": agent_type, "description": desc, "toolUseId": tool_use_id}))
        (d / f"{agent_id}.jsonl").write_text("\n".join(
            json.dumps({"message": {"role": "user", "content": "go"}})
            for _ in range(turns)) + "\n")
        return d / f"{agent_id}.jsonl"

    return path, plant


# ---- open call tracking ------------------------------------------------------

def test_unresolved_agent_call_counts_as_running(session):
    path, _ = session
    _write(path, _main_events(agent_calls=["call_a", "call_b"], resolved=["call_a"]))
    s = parser.get_summary("sess1")
    assert s["subagents_running"] == 1
    assert list(s["open_agent_calls"]) == ["call_b"]


def test_resolved_calls_leave_nothing_running(session):
    path, _ = session
    _write(path, _main_events(agent_calls=["call_a"], resolved=["call_a"]))
    assert parser.get_summary("sess1")["subagents_running"] == 0


def test_a_session_that_never_delegated_costs_nothing(session):
    path, _ = session
    _write(path, _main_events())
    s = parser.get_summary("sess1")
    assert s["subagents_running"] == 0
    assert s["subagent_mtime"] is None


def test_sidechain_calls_are_not_the_main_thread_ledger(session):
    """A nested Agent call inside a sub-agent belongs to that run, not to us."""
    path, _ = session
    events = _main_events()
    events.append({"type": "assistant", "isSidechain": True,
                   "timestamp": "2026-08-25T10:00:02.000Z",
                   "message": {"role": "assistant", "content": [
                       {"type": "tool_use", "id": "nested", "name": "Agent", "input": {}}]}})
    _write(path, events)
    assert parser.get_summary("sess1")["subagents_running"] == 0


# ---- the status fix ----------------------------------------------------------

def test_a_stale_transcript_stays_live_while_subagents_write(session):
    """The whole point: main file untouched for minutes, session still THINKING."""
    path, plant = session
    _write(path, _main_events(agent_calls=["call_a"]))
    sub = plant("agent-a", "call_a")
    old = time.time() - 600                      # transcript last written 10m ago
    os.utime(path, (old, old))
    parser._summary_cache.clear()

    assert parser.get_summary("sess1")["status"] == "THINKING"

    # …and once the sub-agents go quiet too, the session ages normally again.
    os.utime(sub, (old, old))
    parser._summary_cache.clear()
    assert parser.get_summary("sess1")["status"] == "WAITING"


def test_delegating_overrides_thinking_and_waiting():
    for st in ("THINKING", "WAITING"):
        s = _apply_delegating({"status": st, "subagents_running": 2})
        assert s["status"] == "DELEGATING"


def test_delegating_does_not_resurrect_an_abandoned_session():
    """An unresolved call on a session quiet for hours is a dead run, not work."""
    for st in ("SITTING", "SLEEPING", "ENDED"):
        s = _apply_delegating({"status": st, "subagents_running": 1})
        assert s["status"] == st


def test_no_running_subagents_leaves_status_alone():
    s = _apply_delegating({"status": "WAITING", "subagents_running": 0})
    assert s["status"] == "WAITING"


# ---- the roster --------------------------------------------------------------

def test_roster_marks_only_the_open_call_running(session):
    path, plant = session
    _write(path, _main_events(agent_calls=["call_a", "call_b"], resolved=["call_a"]))
    plant("agent-a", "call_a", agent_type="Explore", desc="read the repo")
    plant("agent-b", "call_b", agent_type="Plan", desc="sequence the work")

    runs = parser.get_session("sess1")["subagents"]
    by_type = {r["agent_type"]: r for r in runs}
    assert by_type["Explore"]["running"] is False
    assert by_type["Plan"]["running"] is True
    assert by_type["Plan"]["description"] == "sequence the work"
    assert by_type["Plan"]["agent_id"] == "b"


def test_deep_roster_counts_turns(session):
    path, plant = session
    _write(path, _main_events(agent_calls=["call_a"]))
    plant("agent-a", "call_a", turns=5)
    assert parser.get_session("sess1")["subagents"][0]["turns"] == 5


def test_a_run_spawned_before_its_files_exist_still_counts(session):
    """meta.json lands a beat after the tool_use — don't blink in that gap."""
    path, _ = session
    _write(path, _main_events(agent_calls=["call_a"]))
    s = parser.get_summary("sess1")
    assert s["subagents_running"] == 1
    assert parser.get_session("sess1")["subagents"] == []


def test_active_descriptions_ride_along_for_the_badge(session):
    path, _ = session
    _write(path, _main_events(agent_calls=["call_a"]))
    assert parser.get_summary("sess1")["subagents_active"] == ["probe call_a"]


def test_latest_mtime_is_zero_without_a_subagents_dir(session):
    path, _ = session
    _write(path, _main_events())
    assert subagents.latest_mtime(str(path)) == 0.0


# ---- one run's own turns -----------------------------------------------------

def _rich_run(path, agent_id="agent-r1"):
    """A sub-agent transcript with the shapes the detail view has to render."""
    d = path.parent / "sess1" / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{agent_id}.meta.json").write_text(json.dumps(
        {"agentType": "Explore", "description": "find the parser", "toolUseId": "call_a"}))
    _write(d / f"{agent_id}.jsonl", [
        {"timestamp": "2026-08-25T10:00:00.000Z",
         "message": {"role": "user", "content": "find where status is computed"}},
        {"timestamp": "2026-08-25T10:00:01.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "thinking", "thinking": "grep first"},
             {"type": "tool_use", "id": "t1", "name": "Grep",
              "input": {"pattern": "compute_status"}}]}},
        {"timestamp": "2026-08-25T10:00:02.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1", "content": "parser.py:227"}]}},
        {"timestamp": "2026-08-25T10:00:03.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "It is parser.compute_status."}]}},
    ])
    return d / f"{agent_id}.jsonl"


def test_transcript_expands_every_block_kind(session):
    path, _ = session
    _rich_run(path)
    run = subagents.transcript(str(path), "r1")
    assert run["agent_id"] == "r1" and run["agent_type"] == "Explore"
    assert run["description"] == "find the parser"
    kinds = [a["kind"] for a in run["activities"]]
    assert kinds == ["user", "thinking", "tool", "result", "assistant"]
    tool = run["activities"][2]
    assert tool["name"] == "Grep" and "compute_status" in tool["text"]
    assert run["total"] == 5 and run["truncated"] is False


def test_transcript_accepts_the_prefixed_id_too(session):
    """The roster hands out the bare id, but the file is named agent-<id>.
    Both spellings address the same run."""
    path, _ = session
    _rich_run(path)
    assert subagents.transcript(str(path), "agent-r1")["total"] == 5


def test_transcript_of_an_unknown_run_is_none(session):
    path, _ = session
    _rich_run(path)
    assert subagents.transcript(str(path), "nope") is None


def test_transcript_of_a_session_with_no_subagents_is_none(session):
    path, _ = session
    assert subagents.transcript(str(path), "r1") is None


@pytest.mark.parametrize("bad", [
    "../../../etc/passwd",
    "..%2f..%2fsecret",
    "/etc/passwd",
    "a/b",
    "",
])
def test_transcript_refuses_ids_that_are_really_paths(session, bad):
    """The id arrives from a URL. Anything that could walk out of the session's
    own subagents folder is refused rather than sanitised."""
    path, _ = session
    _rich_run(path)
    assert subagents.transcript(str(path), bad) is None


def test_transcript_keeps_the_newest_turns_when_truncated(session):
    path, _ = session
    _rich_run(path)
    run = subagents.transcript(str(path), "r1", limit=2)
    assert run["truncated"] is True and run["total"] == 5
    assert [a["kind"] for a in run["activities"]] == ["result", "assistant"]


def test_transcript_falls_back_to_the_first_prompt_for_a_description(session):
    """Runs written by older builds have no meta file. The opening instruction
    is what the run was asked to do, which is what the roster line wants."""
    path, _ = session
    _rich_run(path)
    (path.parent / "sess1" / "subagents" / "agent-r1.meta.json").unlink()
    subagents._meta_cache.clear()
    run = subagents.transcript(str(path), "r1")
    assert run["description"] == "find where status is computed"
    assert run["agent_type"] == "agent"


def test_transcript_skips_empty_blocks(session):
    """A turn that carries nothing renders as nothing — an empty row in the
    detail view is indistinguishable from a bug."""
    path, _ = session
    d = path.parent / "sess1" / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    _write(d / "agent-r2.jsonl", [
        {"message": {"role": "assistant", "content": [{"type": "text", "text": "   "}]}},
        {"message": {"role": "assistant", "content": [{"type": "text", "text": "real"}]}},
    ])
    run = subagents.transcript(str(path), "r2")
    assert [a["text"] for a in run["activities"]] == ["real"]


# ---- over HTTP ---------------------------------------------------------------

def test_api_serves_one_run(session):
    from fastapi.testclient import TestClient
    from server.app import app

    path, _ = session
    _write(path, _main_events(agent_calls=["call_a"]))
    _rich_run(path)
    c = TestClient(app)
    r = c.get("/api/sessions/sess1/subagents/r1")
    assert r.status_code == 200
    assert r.json()["activities"][-1]["text"] == "It is parser.compute_status."


def test_api_404s_for_an_unknown_run(session):
    from fastapi.testclient import TestClient
    from server.app import app

    path, _ = session
    _write(path, _main_events())
    c = TestClient(app)
    assert c.get("/api/sessions/sess1/subagents/ghost").status_code == 404


def test_api_404s_for_an_unknown_session(session):
    from fastapi.testclient import TestClient
    from server.app import app

    c = TestClient(app)
    assert c.get("/api/sessions/nosuch/subagents/r1").status_code == 404
