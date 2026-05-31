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
