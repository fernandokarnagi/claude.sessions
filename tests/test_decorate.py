"""Tests for app._decorate — origin (cli/web) and live flags."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server.app import _decorate  # noqa: E402


def base(**kw):
    s = {"session_id": "s1", "title": "Auto", "entrypoint": "cli",
         "status": "WAITING", "mtime": 100.0}
    s.update(kw)
    return s


def test_currently_running_is_web():
    s = _decorate(base(), web_mtimes={}, running={"s1"})
    assert s["origin"] == "web" and s["live_web"] is True


def test_web_wrote_last_is_web():
    # file mtime (100) <= recorded web mtime (100) -> nothing wrote since web
    s = _decorate(base(mtime=100.0), web_mtimes={"s1": 100.0}, running=set())
    assert s["origin"] == "web" and s["live_web"] is False


def test_cli_wrote_after_web_flips_back_to_cli():
    # file mtime (150) > recorded web mtime (100) -> CLI wrote after our turn
    s = _decorate(base(mtime=150.0), web_mtimes={"s1": 100.0}, running=set())
    assert s["origin"] == "cli"


def test_never_web_uses_entrypoint():
    s = _decorate(base(entrypoint="claude-vscode"), web_mtimes={}, running=set())
    assert s["origin"] == "claude-vscode"


def test_cli_thinking_is_live():
    s = _decorate(base(status="THINKING", mtime=150.0), web_mtimes={"s1": 100.0}, running=set())
    assert s["origin"] == "cli" and s["live"] is True


def test_within_eps_still_web():
    # mtime a hair above recorded, within the 1s slop -> still web
    s = _decorate(base(mtime=100.5), web_mtimes={"s1": 100.0}, running=set())
    assert s["origin"] == "web"
