"""
Answering opencode's gate has to survive its follow-up stage.

"Allow once" answers the request outright. "Allow always" and "Reject" do not —
each swaps the dialog for a second one (Confirm/Cancel, or a box asking what to
do instead). Pressing Enter on the first row and reporting success left the
session parked on that second dialog with the board calling it idle.

These drive tmuxio through fake panes, so nothing here touches tmux.
"""

import pytest

from server import tmuxio
from tests.test_opencode_gate import GATE, CONFIRM, REJECT, _ansi, _ansi_confirm

SID = "ses_fake0000000000000000000000"

DONE = """\
  ┃  ran /tmp/build.sh
  ┃
   tab agents  ctrl+p commands
"""


@pytest.fixture
def pane(monkeypatch):
    """Script the pane as (plain, ansi) frames; the last one repeats."""
    def _use(*frames):
        seen = []
        state = {"frames": [f if isinstance(f, tuple) else (f, None)
                            for f in frames]}

        def _cur(pop):
            if pop and len(state["frames"]) > 1:
                return state["frames"].pop(0)
            return state["frames"][0]

        monkeypatch.setattr(tmuxio, "capture_pane",
                            lambda sid, history=None: _cur(False)[0])
        # opencode_pending captures plain then colour; the pop happens on the
        # colour read so both halves of one poll see the same frame.
        monkeypatch.setattr(tmuxio, "capture_pane_ansi",
                            lambda sid: _cur(True)[1])
        monkeypatch.setattr(tmuxio, "_send_keys",
                            lambda sid, *keys: seen.append(keys))
        monkeypatch.setattr(tmuxio.time, "sleep", lambda s: None)
        return seen
    return _use


def test_allow_once_needs_no_follow_up(pane):
    keys = pane((GATE, _ansi("Allow once")), (DONE, None))
    r = tmuxio.opencode_answer(SID, 1)
    assert r["ok"] and r["label"] == "Allow once"
    assert keys == [("Enter",)]


def test_allow_always_is_carried_through_its_confirmation(pane):
    """The bug: this used to stop after the first Enter, leaving opencode on
    the "Always allow" dialog with the request still unanswered."""
    keys = pane((GATE, _ansi("Allow once")),
                (GATE, _ansi("Allow always")),      # after the ⇆ step
                (CONFIRM, _ansi_confirm("Confirm")),
                (DONE, None))
    r = tmuxio.opencode_answer(SID, 2)
    assert r["ok"] and r["label"] == "Allow always"
    assert r["followed_up"] == "always"
    # Right onto "Allow always", Enter, then Enter again on the pre-selected
    # "Confirm" of the second dialog.
    assert keys == [("Right",), ("Enter",), ("Enter",)]


def test_the_confirmation_can_need_a_step_of_its_own(pane):
    keys = pane((GATE, _ansi("Allow once")),
                (GATE, _ansi("Allow always")),
                (CONFIRM, _ansi_confirm("Cancel")),  # opencode kept Cancel lit
                (CONFIRM, _ansi_confirm("Confirm")),
                (DONE, None))
    assert tmuxio.opencode_answer(SID, 2)["ok"]
    assert keys == [("Right",), ("Enter",), ("Left",), ("Enter",)]


def test_reject_submits_the_reason_box(pane):
    keys = pane((GATE, _ansi("Allow once")), (GATE, _ansi("Reject")),
                (REJECT, None), (DONE, None))
    r = tmuxio.opencode_answer(SID, 3, text="use the existing helper")
    assert r["ok"] and r["followed_up"] == "reject"
    assert keys == [("Right",), ("Right",), ("Enter",),
                    ("-l", "--", "use the existing helper"), ("Enter",)]


def test_reject_without_a_reason_still_submits(pane):
    keys = pane((GATE, _ansi("Allow once")), (GATE, _ansi("Reject")),
                (REJECT, None), (DONE, None))
    assert tmuxio.opencode_answer(SID, 3)["ok"]
    assert keys == [("Right",), ("Right",), ("Enter",), ("Enter",)]


def test_a_confirmation_already_on_screen_is_answerable_directly(pane):
    """The dashboard polls; it can meet the gate at its second stage."""
    keys = pane((CONFIRM, _ansi_confirm("Confirm")),
                (CONFIRM, _ansi_confirm("Cancel")), (DONE, None))
    r = tmuxio.opencode_answer(SID, 2)          # Cancel
    assert r["ok"] and r["label"] == "Cancel"
    assert keys == [("Right",), ("Enter",)]


def test_cancelling_the_reason_box_backs_out(pane):
    keys = pane((REJECT, None), (GATE, _ansi("Allow once")))
    r = tmuxio.opencode_answer(SID, 2)
    assert r["ok"] and r["label"] == "Cancel"
    assert keys == [("Escape",)]


def test_no_gate_is_a_failure(pane):
    pane((DONE, None))
    assert tmuxio.opencode_answer(SID, 1)["ok"] is False


def test_a_choice_off_the_end_is_a_failure(pane):
    pane((GATE, _ansi("Allow once")))
    r = tmuxio.opencode_answer(SID, 4)
    assert r["ok"] is False and "out of range" in r["error"]
