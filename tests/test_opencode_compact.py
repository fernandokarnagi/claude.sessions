"""
Compacting an opencode session has to avoid turning into a prompt.

opencode does compact, but its slash command is a trap for a driver. It is
registered only once the session has a turn behind it, and its composer popup
fuzzy-matches descriptions as well as names, so typing `/compact` lists
`/review` under it. Submit early, or before the command exists, and the text
lands on the model as an ordinary prompt — which grows the context the call was
meant to shrink. It also takes no focus instructions: `/compact keep the auth
details` matches nothing and is likewise sent as a prompt.

So tmuxio drives the key binding (ctrl+x c) instead of the composer, refuses
when the session has no turns, and confirms the footer's context figure
actually dropped. These drive tmuxio through fake panes; nothing touches tmux.
"""

import pytest

from server import tmuxio

SID = "ses_fake0000000000000000000000"

# Footer before the first turn: no context figure at all.
EMPTY = """\
   tab agents  ctrl+p commands
  ~/App/ccoe                                                           1.18.25
"""

FULL = """\
   tab agents  ctrl+p commands
   /Users/f/App/ccoe        33.4K (3%) · $0.00  ctrl+p commands
"""

WORKING = """\
   tab agents  ctrl+p commands
   ⬝⬝⬝⬝  esc interrupt              34.0K (3%) · $0.00  ctrl+p commands
"""

COMPACTED = """\
   tab agents  ctrl+p commands
   /Users/f/App/ccoe          605 (0%) · $0.00  ctrl+p commands
"""


@pytest.fixture
def pane(monkeypatch):
    """Script the pane as a list of frames; the last one repeats."""
    def _use(*frames):
        # opencode_compact reads the pane once for its liveness check before it
        # reads the footer, so frame one is consumed by that check.
        seen = list(frames)

        def _cap(session_id, history=None):
            return seen[0] if len(seen) == 1 else seen.pop(0)
        monkeypatch.setattr(tmuxio, "capture_pane", _cap)
    return _use


@pytest.fixture
def keys(monkeypatch):
    """Record every _send_keys call instead of driving tmux."""
    sent = []
    monkeypatch.setattr(tmuxio, "_send_keys",
                        lambda sid, *k: sent.append(k))
    monkeypatch.setattr(tmuxio.time, "sleep", lambda *_: None)
    return sent


# --- reading the footer -----------------------------------------------------

@pytest.mark.parametrize("footer, want", [
    (FULL, 33_400),
    (COMPACTED, 605),
    (WORKING, 34_000),      # mid-turn, figure still present
    (EMPTY, None),          # no turns yet
])
def test_context_tokens_read_off_the_footer(pane, footer, want):
    pane(footer)
    assert tmuxio.opencode_context_tokens(SID) == want


# --- the guards -------------------------------------------------------------

def test_no_live_session_is_refused(pane, keys):
    pane(None)
    r = tmuxio.opencode_compact(SID)
    assert r["ok"] is False
    assert "no live tmux session" in r["error"]
    assert keys == []


def test_a_session_with_no_turns_is_refused_without_pressing_anything(pane, keys):
    """The dangerous case: nothing to compact, so send nothing.

    Pressing on regardless is how a compact request becomes a stray prompt.
    """
    pane(EMPTY)
    r = tmuxio.opencode_compact(SID)
    assert r["ok"] is False
    assert "no turns yet" in r["error"]
    assert keys == []


# --- the happy path ---------------------------------------------------------

def test_compaction_uses_the_key_binding_not_the_composer(pane, keys):
    pane(FULL, FULL, COMPACTED)
    assert tmuxio.opencode_compact(SID)["ok"] is True
    assert keys == [("C-x", "c")], "must not type /compact into the composer"


def test_reports_the_context_it_reclaimed(pane, keys):
    pane(FULL, FULL, COMPACTED)
    assert tmuxio.opencode_compact(SID) == {
        "ok": True, "before": 33_400, "after": 605}


def test_waits_through_frames_where_the_context_has_not_dropped_yet(pane, keys):
    pane(FULL, FULL, FULL, WORKING, COMPACTED)
    r = tmuxio.opencode_compact(SID)
    assert r["ok"] is True and r["after"] == 605


def test_a_context_that_never_shrinks_is_a_failure(pane, keys):
    """Key sent, footer unchanged — say so rather than claiming success."""
    pane(FULL)
    r = tmuxio.opencode_compact(SID, timeout=2.0)
    assert r["ok"] is False
    assert "never shrank" in r["error"]
    assert r["before"] == 33_400
