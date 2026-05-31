"""Tests for server.overrides and app._decorate title application."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import overrides  # noqa: E402
from server.app import _decorate  # noqa: E402


@pytest.fixture
def ov(tmp_path, monkeypatch):
    monkeypatch.setattr(overrides, "_PATH", str(tmp_path / "titles.json"))
    return overrides


def test_set_get_clear(ov):
    assert ov.get_title("s1") is None
    ov.set_title("s1", "My Name")
    assert ov.get_title("s1") == "My Name"
    ov.clear_title("s1")
    assert ov.get_title("s1") is None


def test_empty_title_reverts(ov):
    ov.set_title("s1", "X")
    ov.set_title("s1", "   ")          # whitespace -> revert
    assert ov.get_title("s1") is None


def test_persists(ov):
    ov.set_title("s2", "Persisted")
    assert ov.all_titles()["s2"] == "Persisted"  # re-read from disk


def test_decorate_applies_override():
    s = {"session_id": "s1", "title": "Auto Title", "entrypoint": "cli",
         "status": "WAITING", "mtime": 100.0}
    out = _decorate(s, web_mtimes={}, running=set(), titles={"s1": "Renamed"})
    assert out["title"] == "Renamed"
    assert out["default_title"] == "Auto Title"
    assert out["renamed"] is True


def test_decorate_no_override_keeps_original():
    s = {"session_id": "s1", "title": "Auto Title", "entrypoint": "cli",
         "status": "WAITING", "mtime": 100.0}
    out = _decorate(s, web_mtimes={}, running=set(), titles={})
    assert out["title"] == "Auto Title"
    assert out["renamed"] is False
