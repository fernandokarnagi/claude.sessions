"""
Reading opencode's *question* dialog off a tmux pane.

This is a different widget from the permission gate: the `question` tool draws a
numbered list with a description under each row, and a footer reading
"↑↓ select  enter submit  esc dismiss". Nothing in tmuxio recognised it, so a
session sitting on one read as idle and the dashboard only ever showed the raw
tool JSON from the transcript.

Unlike the gate it takes digits — opencode's key handler maps 1..9 to
"select this row and act on it" — so answering needs no ⇆ dance.

The frames are reconstructed from a screenshot of a live dialog plus opencode's
own render code (footer, numbering and the "Type your own answer" row are taken
verbatim from it). Nothing here runs tmux.
"""

import pytest

from server import tmuxio

SID = "ses_fake0000000000000000000000"

ASK = """\
  ┃  The most interesting question this graph can answer: Why does `User` connect
  ┃  `FastAPI Application` to `Graphify Tooling`? Want me to trace it?
  ┃
  ┃  1. Yes, trace it
  ┃     Walk through the graph structure showing how User bridges communities
  ┃  2. No, just show the report
  ┃     Keep the session to the report summary
  ┃  3. Type your own answer
  ┃
  ┃  ↑↓ select  enter submit  esc dismiss
"""

# A transcript can hold a numbered list of its own; without the footer it is
# not a dialog.
NOT_ASK = """\
  ┃  Two options here:
  ┃  1. Yes, trace it
  ┃  2. No, just show the report
  ┃
   tab agents  ctrl+p commands
"""

SURFACE = "48;2;24;24;28"      # the dialog's own background, on every row
LINE = "48;2;60;60;72"         # the highlighted row's background


def _ansi(selected):
    """ASK with backgrounds: every row carries the dialog surface, the selected
    one carries theme.line instead — which is the only difference on screen."""
    out = []
    n = 0
    for line in ASK.splitlines():
        m = tmuxio._OPENCODE_ASK_OPTION_RE.match(tmuxio._opencode_unbox(line))
        if m:
            n += 1
            bg = LINE if n == selected else SURFACE
            out.append(f"  ┃  \x1b[{bg}m{n}. {m.group(2)}\x1b[0m")
        else:
            out.append(f"\x1b[{SURFACE}m{line}\x1b[0m")
    return "\n".join(out) + "\n"


def test_the_question_dialog_is_a_pending_prompt():
    ask = tmuxio.parse_opencode_ask(ASK)
    assert ask is not None
    assert ask["stage"] == "ask"


def test_the_options_are_numbered_as_opencode_numbers_them():
    ask = tmuxio.parse_opencode_ask(ASK)
    assert [(o["num"], o["label"]) for o in ask["options"]] == [
        (1, "Yes, trace it"),
        (2, "No, just show the report"),
        (3, "Type your own answer")]


def test_the_wrapped_question_comes_back_as_one_line():
    ask = tmuxio.parse_opencode_ask(ASK)
    assert ask["question"] == (
        "The most interesting question this graph can answer: Why does `User` "
        "connect `FastAPI Application` to `Graphify Tooling`? Want me to trace it?")


def test_the_option_descriptions_are_not_mistaken_for_options():
    """They sit between the numbered rows and carry no number of their own."""
    ask = tmuxio.parse_opencode_ask(ASK)
    assert len(ask["options"]) == 3


def test_the_free_text_row_is_flagged():
    """Picking it opens a textarea instead of replying, so answering it needs
    text — the caller has to know which row that is."""
    assert tmuxio.parse_opencode_ask(ASK)["custom"] == 3


def test_a_numbered_list_in_the_transcript_is_not_a_dialog():
    assert tmuxio.parse_opencode_ask(NOT_ASK) is None
    assert tmuxio.parse_opencode_ask(None) is None


def test_the_highlight_is_the_row_whose_background_differs():
    """Every row is painted with the dialog's surface colour, so "has a
    background" says nothing — the odd one out is the selected one."""
    for want in (1, 2, 3):
        ask = tmuxio.parse_opencode_ask(ASK, _ansi(want))
        assert [o["num"] for o in ask["options"] if o["selected"]] == [want]


def test_without_a_colour_capture_nothing_claims_to_be_selected():
    ask = tmuxio.parse_opencode_ask(ASK)
    assert not any(o["selected"] for o in ask["options"])


def test_a_dialog_outranks_the_busy_footer():
    assert tmuxio._OPENCODE_ASK_FOOT_RE.search(ASK)
    assert not tmuxio._OPENCODE_ASK_FOOT_RE.search(NOT_ASK)


@pytest.fixture
def pane(monkeypatch):
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
        monkeypatch.setattr(tmuxio, "capture_pane_ansi", lambda sid: _cur(True)[1])
        monkeypatch.setattr(tmuxio, "_send_keys",
                            lambda sid, *keys: seen.append(keys))
        monkeypatch.setattr(tmuxio.time, "sleep", lambda s: None)
        return seen
    return _use


def test_the_dialog_shows_up_as_a_pending_gate(pane):
    pane((ASK, None))
    p = tmuxio.opencode_pending(SID)
    assert p is not None and p["stage"] == "ask"


def test_answering_presses_the_digit(pane):
    """opencode maps 1..9 to select-and-act, so one keypress is the answer."""
    keys = pane((ASK, None))
    r = tmuxio.opencode_answer(SID, 2)
    assert r["ok"] and r["label"] == "No, just show the report"
    assert keys == [("2",)]


def test_the_free_text_row_types_the_answer(pane):
    keys = pane((ASK, None))
    r = tmuxio.opencode_answer(SID, 3, text="trace it but skip the report")
    assert r["ok"]
    assert keys == [("3",), ("-l", "--", "trace it but skip the report"),
                    ("Enter",)]


def test_the_free_text_row_without_text_is_refused(pane):
    """Pressing it and walking away parks opencode in an editor nobody fills."""
    keys = pane((ASK, None))
    r = tmuxio.opencode_answer(SID, 3)
    assert r["ok"] is False and "text" in r["error"]
    assert keys == []
