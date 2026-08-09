"""
Following a grok session to the id /new gives it.

grok keys sessions by directory, not by a transcript file, so a reset has to
watch ~/.grok/sessions/<enc-cwd>/ for the folder that appears and rename the
tmux onto its name. Until that rename lands the dashboard is driving a pane
named after a conversation grok has already left.

tmux and the REPL are stubbed — nothing here shells out.
"""

import pytest

from server import tmuxio

OLD = "old-grok-session"
NEW = "new-grok-session"


def _make_session(root, sid):
    d = root / sid
    d.mkdir(parents=True)
    (d / "summary.json").write_text("{}", encoding="utf-8")
    return d


@pytest.fixture
def grok(tmp_path, monkeypatch):
    """A project folder holding OLD, with /new stubbed to create `creates`."""
    root = tmp_path / "proj"
    _make_session(root, OLD)
    renamed = []
    monkeypatch.setattr(tmuxio, "capture_pane", lambda sid, history=None: "pane")
    monkeypatch.setattr(tmuxio, "_rename_onto",
                        lambda old, new: renamed.append((old, new)) or
                        {"ok": True, "old_id": old, "session_id": new})
    monkeypatch.setattr(tmuxio.time, "sleep", lambda s: None)

    def _use(creates=None, sent=None):
        def fake_say(sid, text):
            assert text == "/new"
            if creates:
                _make_session(root, creates)
            return sent or {"ok": True}
        monkeypatch.setattr(tmuxio, "grok_say", fake_say)
        return root, renamed
    return _use


def test_the_tmux_follows_the_new_session(grok):
    root, renamed = grok(creates=NEW)
    r = tmuxio.grok_reset(OLD, str(root))
    assert r == {"ok": True, "old_id": OLD, "session_id": NEW}
    assert renamed == [(OLD, NEW)]


def test_the_old_session_is_not_mistaken_for_the_new_one(grok):
    # OLD's own directory is still there and always will be — only a folder
    # that wasn't there before counts.
    root, renamed = grok(creates=None)
    r = tmuxio.grok_reset(OLD, str(root), timeout=1.0)
    assert r["ok"] is False
    assert "no new grok session appeared" in r["error"]
    assert renamed == []


def test_a_half_written_session_directory_is_ignored(grok):
    # grok creates the folder before summary.json lands in it. Taking the name
    # too early would rename the tmux onto a session that may never exist.
    root, renamed = grok(creates=None)
    (root / "not-a-session-yet").mkdir()
    r = tmuxio.grok_reset(OLD, str(root), timeout=1.0)
    assert r["ok"] is False
    assert renamed == []


def test_new_that_never_submits_is_not_a_reset(grok):
    # grok's editor debounces: if the Enter was swallowed, /new is still sitting
    # in the composer and nothing has happened. Say so rather than waiting out
    # the timeout on a command that was never run.
    root, renamed = grok(sent={"ok": False, "error": "no live tmux session"})
    r = tmuxio.grok_reset(OLD, str(root))
    assert r["ok"] is False
    assert renamed == []


def test_no_live_pane_and_no_such_directory_are_both_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(tmuxio, "capture_pane", lambda sid, history=None: None)
    assert tmuxio.grok_reset(OLD, str(tmp_path))["ok"] is False
    monkeypatch.setattr(tmuxio, "capture_pane", lambda sid, history=None: "pane")
    assert tmuxio.grok_reset(OLD, str(tmp_path / "nope"))["ok"] is False
