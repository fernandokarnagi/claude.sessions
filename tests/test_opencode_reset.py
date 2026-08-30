"""
Resetting an opencode session has to know the new id before it tears anything
down.

Claude Code and grok give theirs up: /clear and /new make the store write a new
session immediately, so their reset types the command and follows the id home.
opencode does not — it registers the conversation only when its first turn
lands, so a reset that typed /new would leave the tmux named after the old
session for as long as the new one sat idle, and the dashboard's one invariant
(tmux name == session id) would be false the whole time.

So tmuxio mints the session first, through opencode's headless server, and
relaunches the pane onto it. These drive tmuxio through fakes; nothing starts a
server and nothing touches tmux.
"""

import subprocess

import pytest

from server import tmuxio

OLD = "ses_fbold000000000000000000000"
NEW = "ses_fbnew000000000000000000000"
CWD = "/proj/app"
MODEL = "opencode-go/glm-5.3-flash"

READY = "  /proj/app        1.2K (0%) · $0.00  ctrl+p commands\n"
BOOTING = "  starting…\n"


@pytest.fixture
def pane(monkeypatch):
    """Script the pane as a list of frames; the last one repeats."""
    def _use(*frames):
        seen = list(frames)

        def _cap(session_id, history=None):
            return seen[0] if len(seen) == 1 else seen.pop(0)
        monkeypatch.setattr(tmuxio, "capture_pane", _cap)
    return _use


@pytest.fixture
def rig(monkeypatch):
    """Record the tmux calls and key sends a reset makes, and fake the mint."""
    log = {"runs": [], "keys": [], "killed": [], "minted": None}

    def _run(argv, **kw):
        log["runs"].append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(tmuxio.subprocess, "run", _run)
    monkeypatch.setattr(tmuxio, "_send_keys",
                        lambda sid, *k: log["keys"].append((sid, k)))
    monkeypatch.setattr(tmuxio, "kill",
                        lambda sid: log["killed"].append(sid) or {"ok": True})
    monkeypatch.setattr(tmuxio.time, "sleep", lambda *_: None)

    def mint(result):
        log["minted"] = result
        monkeypatch.setattr(tmuxio, "opencode_new_session",
                            lambda cwd, **kw: dict(result, _cwd=cwd))
    log["mint"] = mint
    return log


# --- the guards -------------------------------------------------------------

def test_no_live_session_is_refused(pane, rig):
    pane(None)
    rig["mint"]({"ok": True, "session_id": NEW})
    r = tmuxio.opencode_reset(OLD, CWD, MODEL)
    assert r["ok"] is False and "no live tmux session" in r["error"]
    assert rig["killed"] == [] and rig["runs"] == []


def test_a_session_with_no_model_is_refused(pane, rig):
    pane(READY)
    rig["mint"]({"ok": True, "session_id": NEW})
    r = tmuxio.opencode_reset(OLD, CWD, None)
    assert r["ok"] is False and "no model" in r["error"]
    assert rig["killed"] == []


def test_a_failed_mint_leaves_the_old_session_alone(pane, rig):
    # The whole point of minting first: nothing is torn down until there is an
    # id to move to.
    pane(READY)
    rig["mint"]({"ok": False, "error": "opencode serve exited (1)"})
    r = tmuxio.opencode_reset(OLD, CWD, MODEL)
    assert r["ok"] is False
    assert r["old_id"] == OLD and "serve exited" in r["error"]
    assert rig["killed"] == [] and rig["runs"] == []


# --- the happy path ---------------------------------------------------------

def test_reset_relaunches_the_pane_under_the_new_id(pane, rig):
    pane(READY)
    rig["mint"]({"ok": True, "session_id": NEW})
    r = tmuxio.opencode_reset(OLD, CWD, MODEL)

    assert r == {"ok": True, "old_id": OLD, "session_id": NEW}
    assert rig["killed"] == [OLD]                       # old REPL is gone
    assert rig["runs"] == [["tmux", "new-session", "-d", "-s", NEW, "-c", CWD]]

    sid, keys = rig["keys"][0]
    assert sid == NEW                                   # keys go to the new pane
    cmd = keys[-1]
    assert f"--session {NEW}" in cmd                    # resumes what we minted
    assert f"--model {MODEL}" in cmd
    assert cmd.startswith(f"cd {CWD} && ")              # profile can't cd away
    assert rig["keys"][1] == (NEW, ("Enter",))


def test_a_slow_repl_still_counts_as_reset(pane, rig):
    # The id is real and the pane is up; opencode was only slow to paint. A
    # failure here would report a move that already happened as not having.
    pane(BOOTING)
    rig["mint"]({"ok": True, "session_id": NEW})
    r = tmuxio.opencode_reset(OLD, CWD, MODEL, ready_timeout=2)
    assert r["ok"] is True and r["ready"] is False
    assert r["session_id"] == NEW


def test_tmux_refusing_the_new_session_is_reported(pane, rig, monkeypatch):
    pane(READY)
    rig["mint"]({"ok": True, "session_id": NEW})
    monkeypatch.setattr(
        tmuxio.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "duplicate session"))
    r = tmuxio.opencode_reset(OLD, CWD, MODEL)
    assert r["ok"] is False and r["error"] == "duplicate session"
    # The id was still minted, so the caller can carry state over to it.
    assert r["session_id"] == NEW and r["old_id"] == OLD


def test_the_mint_is_rooted_at_the_session_directory(pane, rig, monkeypatch):
    # The server files the session under its own cwd, so the wrong directory
    # here puts the new conversation in the wrong project.
    pane(READY)
    seen = []
    monkeypatch.setattr(tmuxio, "opencode_new_session",
                        lambda cwd, **kw: (seen.append(cwd) or
                                           {"ok": True, "session_id": NEW}))
    tmuxio.opencode_reset(OLD, CWD, MODEL)
    assert seen == [CWD]


# --- minting ----------------------------------------------------------------

def test_new_session_needs_a_real_directory():
    r = tmuxio.opencode_new_session("/no/such/dir/anywhere")
    assert r["ok"] is False and "project dir not found" in r["error"]
