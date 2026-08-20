"""Tests for server.summaries (cache) and server.summarizer (fake subprocess)."""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import summaries, summarizer, parser  # noqa: E402


# ---- summaries cache ---------------------------------------------------------

@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(summaries, "_PATH", str(tmp_path / "sum.json"))
    return summaries


def test_cache_set_get_same_mtime(cache):
    cache.set("s1", 100.0, "Do X")
    assert cache.get("s1", 100.0) == "Do X"


def test_cache_miss_on_changed_mtime(cache):
    cache.set("s1", 100.0, "Do X")
    assert cache.get("s1", 200.0) is None      # session worked again -> stale
    assert cache.get("s1", 100.4) == "Do X"    # within 1s slop -> still valid


def test_cache_persists(cache):
    cache.set("s2", 50.0, "Persisted")
    assert cache.get("s2", 50.0) == "Persisted"


# ---- summarizer (fake claude subprocess) -------------------------------------

class _FakeProc:
    def __init__(self, stdout: bytes):
        self._stdout = stdout
        self.returncode = 0

    async def communicate(self):
        return self._stdout, b""


def test_generate_parses_result_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(parser, "SUMMARIZER_CWD", str(tmp_path / "sumcwd"))
    deleted = {}
    monkeypatch.setattr(summarizer, "_delete_throwaway",
                        lambda sid: deleted.update({"sid": sid}))

    payload = b'{"type":"result","result":"You need to confirm the deploy.","session_id":"throw-1"}'

    async def fake_exec(*args, **kwargs):
        assert kwargs.get("cwd") == str(tmp_path / "sumcwd")
        return _FakeProc(payload)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    out = asyncio.run(summarizer.generate("So, should I deploy to prod?"))
    assert out == "You need to confirm the deploy."
    assert deleted["sid"] == "throw-1"  # throwaway transcript cleanup attempted


def test_generate_empty_input_returns_none():
    assert asyncio.run(summarizer.generate("   ")) is None


def test_generate_handles_non_json(tmp_path, monkeypatch):
    monkeypatch.setattr(parser, "SUMMARIZER_CWD", str(tmp_path / "sumcwd"))

    async def fake_exec(*args, **kwargs):
        return _FakeProc(b"plain text answer")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    out = asyncio.run(summarizer.generate("question?"))
    assert out == "plain text answer"


# ---- last reply -> task ------------------------------------------------------

def test_as_task_uses_the_task_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(parser, "SUMMARIZER_CWD", str(tmp_path / "sumcwd"))
    monkeypatch.setattr(summarizer, "_delete_throwaway", lambda sid: None)
    seen = {}

    async def fake_exec(*args, **kwargs):
        seen["prompt"] = args[-1]
        return _FakeProc(b'{"result":"Go with option 2, the cached parser.",'
                         b'"session_id":"throw-2"}')

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    out = asyncio.run(summarizer.as_task("Option 1 or option 2? I'd pick 2."))
    assert out == "Go with option 2, the cached parser."
    assert "follow-up message the user should send back" in seen["prompt"]


def test_summarize_queues_the_generated_text(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from server import app as appmod, tasks

    monkeypatch.setattr(tasks, "_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(appmod, "_last_assistant_text", lambda sid: "Shall I deploy?")

    async def fake_as_task(text):
        assert text == "Shall I deploy?"
        return "Deploy to prod."

    monkeypatch.setattr(appmod.summarizer, "as_task", fake_as_task)

    client = TestClient(appmod.app)
    r = client.post("/api/sessions/s1/tasks/summarize")
    assert r.status_code == 200
    assert r.json()["text"] == "Deploy to prod."
    assert [t["text"] for t in tasks.list_tasks("s1")] == ["Deploy to prod."]
    assert tasks.pending_count("s1") == 1     # queued, not asked


def test_summarize_uses_the_posted_message(tmp_path, monkeypatch):
    """The 📋 Task button posts the message it sits on, not the latest one."""
    from fastapi.testclient import TestClient
    from server import app as appmod, tasks

    monkeypatch.setattr(tasks, "_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(appmod, "_last_assistant_text", lambda sid: "the newest reply")
    seen = {}

    async def fake_as_task(text):
        seen["text"] = text
        return "Answer the older question."

    monkeypatch.setattr(appmod.summarizer, "as_task", fake_as_task)

    r = TestClient(appmod.app).post("/api/sessions/s1/tasks/summarize",
                                    json={"text": "an older reply"})
    assert r.status_code == 200
    assert seen["text"] == "an older reply"
    assert [t["text"] for t in tasks.list_tasks("s1")] == ["Answer the older question."]


def test_summarize_404s_for_an_unknown_session(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from server import app as appmod, tasks

    monkeypatch.setattr(tasks, "_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(appmod, "_last_assistant_text", lambda sid: None)
    monkeypatch.setattr(appmod, "_session_exists", lambda sid: False)

    r = TestClient(appmod.app).post("/api/sessions/nope/tasks/summarize")
    assert r.status_code == 404


# ---- exclusion of summarizer sessions ----------------------------------------

def test_summarizer_sessions_excluded_from_list(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(parser, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(parser, "SUMMARIZER_CWD", "/throwaway/cwd")
    parser._summary_cache.clear()

    # a normal session
    p1 = tmp_path / "-real"; p1.mkdir()
    (p1 / "real.jsonl").write_text(json.dumps({
        "type": "user", "timestamp": "2026-05-31T10:00:00.000Z", "cwd": "/real/proj",
        "message": {"role": "user", "content": "hi"}}) + "\n")
    # a throwaway summarizer session
    p2 = tmp_path / "-throw"; p2.mkdir()
    (p2 / "throw.jsonl").write_text(json.dumps({
        "type": "user", "timestamp": "2026-05-31T10:00:00.000Z", "cwd": "/throwaway/cwd",
        "message": {"role": "user", "content": "summarize"}}) + "\n")

    ids = [s["session_id"] for s in parser.list_sessions(limit=None)["sessions"]]
    assert "real" in ids
    assert "throw" not in ids  # excluded
