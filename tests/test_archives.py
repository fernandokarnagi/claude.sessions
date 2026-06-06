"""Tests for server.archives and list_sessions archive filtering."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import archives, parser  # noqa: E402


@pytest.fixture
def arch(tmp_path, monkeypatch):
    monkeypatch.setattr(archives, "_PATH", str(tmp_path / "archived.json"))
    return archives


def test_archive_set_and_clear(arch):
    assert arch.is_archived("s1") is False
    arch.set_archived("s1", True)
    assert arch.is_archived("s1") is True
    assert "s1" in arch.archived_ids()
    arch.set_archived("s1", False)
    assert arch.is_archived("s1") is False


def test_archive_persists(arch):
    arch.set_archived("s2", True)
    assert "s2" in arch.archived_ids()  # re-read from disk


@pytest.fixture
def two_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(parser, "PROJECTS_DIR", str(tmp_path))
    parser._summary_cache.clear()
    for name in ("aaa", "bbb"):
        d = tmp_path / f"-{name}"; d.mkdir()
        (d / f"{name}.jsonl").write_text(json.dumps({
            "type": "user", "timestamp": "2026-05-31T10:00:00.000Z", "cwd": f"/{name}",
            "message": {"role": "user", "content": "hi"}}) + "\n")
    return tmp_path


def test_list_excludes_archived(two_sessions):
    ids = {"aaa"}
    out = parser.list_sessions(limit=None, archived_ids=ids, archived_mode="exclude")
    got = [s["session_id"] for s in out["sessions"]]
    assert "aaa" not in got and "bbb" in got
    assert out["total"] == 1


def test_list_only_archived(two_sessions):
    ids = {"aaa"}
    out = parser.list_sessions(limit=None, archived_ids=ids, archived_mode="only")
    got = [s["session_id"] for s in out["sessions"]]
    assert got == ["aaa"]


def test_list_all_includes_archived(two_sessions):
    ids = {"aaa"}
    out = parser.list_sessions(limit=None, archived_ids=ids, archived_mode="all")
    assert out["total"] == 2
