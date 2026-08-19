"""
Stamping a time on every grok turn.

grok's chat_history.jsonl carries no timestamps, so the history view rendered
every message box with an em dash where Claude sessions show a clock time. The
times live in updates.jsonl instead — keyed by promptIndex for user turns and
toolCallId for tool results — and the turns in between interpolate.

Everything here is a fake ~/.grok tree; nothing reads the real one.
"""

import json

import pytest

from server import grokparser


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


@pytest.fixture
def session(tmp_path, monkeypatch):
    """One grok session: two prompts, an answer, and two tool results."""
    root = tmp_path / "sessions" / "%2Fproj" / "sid-1"
    root.mkdir(parents=True)
    monkeypatch.setattr(grokparser, "SESS_ROOT", str(tmp_path / "sessions"))
    grokparser._DIR_CACHE.clear()
    grokparser._SUMM_CACHE.clear()
    grokparser._TS_CACHE.clear()

    (root / "summary.json").write_text(json.dumps({
        "info": {"id": "sid-1", "cwd": "/proj"},
        "created_at": "2026-08-18T15:13:21.015882Z",
        "updated_at": "2026-08-18T15:20:00.000000Z",
        "generated_title": "a grok session",
        "num_messages": 6,
        "current_model_id": "grok-4.6",
    }), encoding="utf-8")

    _write(root / "chat_history.jsonl", [
        {"type": "user", "content": "first ask", "prompt_index": 0},
        {"type": "reasoning", "content": "thinking about it"},
        {"type": "assistant", "content": "on it"},
        {"type": "tool_result", "content": "file read", "tool_call_id": "call-a"},
        {"type": "user", "content": "second ask", "prompt_index": 1},
        {"type": "tool_result", "content": "grep done", "tool_call_id": "call-b"},
    ])
    _write(root / "updates.jsonl", [
        {"timestamp": 1787066080, "method": "_x.ai/session/update", "params": {
            "update": {"sessionUpdate": "user_message_chunk",
                       "content": {"type": "text", "text": "first ask"},
                       "_meta": {"promptIndex": 0}}}},
        {"timestamp": 1787066086, "method": "_x.ai/session/update", "params": {
            "update": {"sessionUpdate": "tool_call", "toolCallId": "call-a",
                       "title": "read_file"}}},
        {"timestamp": 1787066090, "method": "_x.ai/session/update", "params": {
            "update": {"sessionUpdate": "hook_execution", "event_name": "stop"}}},
        {"timestamp": 1787066400, "method": "_x.ai/session/update", "params": {
            "update": {"sessionUpdate": "user_message_chunk",
                       "content": {"type": "text", "text": "second ask"},
                       "_meta": {"promptIndex": 1}}}},
        {"timestamp": 1787066500, "method": "_x.ai/session/update", "params": {
            "update": {"sessionUpdate": "tool_call_update", "toolCallId": "call-b",
                       "title": "grep"}}},
    ])
    return root


def _by_text(acts):
    return {a["text"]: a["ts"] for a in acts}


def test_user_turns_take_the_time_of_their_prompt(session):
    ts = _by_text(grokparser.get_session("sid-1")["activities"])
    assert ts["first ask"].startswith("2026-08-18T15:14:40")
    assert ts["second ask"].startswith("2026-08-18T15:20:00")


def test_tool_results_take_the_time_of_their_call(session):
    ts = _by_text(grokparser.get_session("sid-1")["activities"])
    assert ts["file read"].startswith("2026-08-18T15:14:46")
    assert ts["grep done"].startswith("2026-08-18T15:21:40")


def test_untimed_turns_borrow_the_next_known_time(session):
    """Reasoning and assistant text are written just before the tool call that
    follows them — close enough that the following call's clock reads right."""
    ts = _by_text(grokparser.get_session("sid-1")["activities"])
    assert ts["thinking about it"] == ts["file read"]
    assert ts["on it"] == ts["file read"]


def test_every_turn_gets_a_time_even_with_no_updates_file(session):
    (session / "updates.jsonl").unlink()
    grokparser._TS_CACHE.clear()
    acts = grokparser.get_session("sid-1")["activities"]
    assert acts and all(a["ts"] for a in acts)


def test_the_board_preview_skips_the_updates_read(session, monkeypatch):
    """list_sessions renders no times, so it must not touch the (megabyte)
    update stream — one board refresh covers every grok session on disk."""
    monkeypatch.setattr(grokparser, "_update_times",
                        lambda d: pytest.fail("board read updates.jsonl"))
    assert grokparser.list_sessions()
