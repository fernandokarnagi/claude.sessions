"""Tests for server.registry and server.runner (no real model calls)."""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import registry, runner  # noqa: E402


# ---- registry ----------------------------------------------------------------

@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "_PATH", str(tmp_path / "adopted.json"))
    return registry


def test_registry_set_and_query(reg):
    assert reg.get_web_mtime("s1") is None
    reg.set_web_mtime("s1", 1234.5)
    assert reg.get_web_mtime("s1") == 1234.5
    assert reg.web_mtimes()["s1"] == 1234.5


def test_registry_persists(reg):
    reg.set_web_mtime("s2", 99.0)
    # fresh read from disk (simulates a restart)
    assert reg.web_mtimes()["s2"] == 99.0


def test_registry_ignores_none(reg):
    reg.set_web_mtime("s3", None)
    assert reg.get_web_mtime("s3") is None


# ---- runner.parse_event_line -------------------------------------------------

def test_parse_event_line_ok():
    assert runner.parse_event_line('{"type":"assistant"}') == {"type": "assistant"}

def test_parse_event_line_blank():
    assert runner.parse_event_line("   ") is None

def test_parse_event_line_bad():
    assert runner.parse_event_line("not json") is None


# ---- runner.run_turn (fake subprocess) ---------------------------------------

class _FakeStdout:
    def __init__(self, lines):
        self._lines = [l.encode() for l in lines]

    def __aiter__(self):
        async def gen():
            for l in self._lines:
                yield l
        return gen()


class _FakeStderr:
    async def read(self):
        return b""


class _FakeProc:
    def __init__(self, lines):
        self.stdout = _FakeStdout(lines)
        self.stderr = _FakeStderr()
        self.returncode = 0

    async def wait(self):
        self.returncode = 0


def test_run_turn_streams_and_records_mtime(tmp_path, monkeypatch):
    from server import parser
    monkeypatch.setattr(registry, "_PATH", str(tmp_path / "web.json"))
    # a real transcript file so parser.session_mtime("sX") resolves
    proj = tmp_path / "-proj"; proj.mkdir()
    (proj / "sX.jsonl").write_text("{}\n")
    monkeypatch.setattr(parser, "PROJECTS_DIR", str(tmp_path))

    lines = [
        '{"type":"system","subtype":"init","model":"claude-opus-4-8"}\n',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n',
        "\n",  # blank line should be skipped
        '{"type":"result","subtype":"success","usage":{"input_tokens":5,"output_tokens":2}}\n',
    ]

    async def fake_exec(*args, **kwargs):
        assert kwargs.get("cwd") in (None, str(tmp_path))
        return _FakeProc(lines)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def collect():
        out = []
        async for evt in runner.run_turn("sX", "hello", str(tmp_path), "acceptEdits"):
            out.append(evt)
        return out

    events = asyncio.run(collect())
    types = [e.get("type") for e in events]
    assert types[0] == "system"
    assert "assistant" in types
    assert "result" in types
    assert events[-1] == {"type": "done", "ok": True}
    assert registry.get_web_mtime("sX") is not None  # web turn recorded its mtime


def test_run_turn_invalid_mode_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "_PATH", str(tmp_path / "adopted.json"))
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _FakeProc([])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def run():
        async for _ in runner.run_turn("sY", "x", None, "garbage-mode"):
            pass

    asyncio.run(run())
    assert "acceptEdits" in captured["args"]  # bad mode fell back to default
