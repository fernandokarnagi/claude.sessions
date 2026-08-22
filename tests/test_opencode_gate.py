"""
Reading opencode's permission gate off a tmux pane.

opencode doesn't render a numbered menu like Claude Code — it draws a horizontal
row of options and marks the selected one by *colour alone*. A plain
`capture-pane` strips colour, so the highlight has to come from a second capture
taken with -e.

The frames here are real captures from an 80-column pane, which is what makes
them worth keeping: the row's key hints run off the right edge ("enter confirm"
arrives as "enter con"), so the gate has to be recognised by its header instead.
Nothing here runs tmux.
"""

from server import tmuxio


GATE = """\
  ┃  △ Permission required
  ┃    ← Access external directory /tmp
  ┃
  ┃  Patterns
  ┃
  ┃  - /tmp/*
  ┃
  ┃
  ┃   Allow once   Allow always   Reject  ctrl+f fullscreen  ⇆ select  enter con
  ┃
"""

IDLE = """\
  ┃  > how do I add a route?
  ┃
  ┃  Build · deepseek-v4-flash (cloud via ollama) Ollama (local)
   tab agents  ctrl+p commands
"""

BUSY = """\
  ┃  reading app/api.py
  ┃  ⣾  working
  ┃
   esc interrupt
"""

# A transcript line that happens to say "Reject" — not the widget.
NOT_A_GATE = """\
  ┃  I'll Reject that approach and use a hook instead.
  ┃
   tab agents  ctrl+p commands
"""

DIM = tmuxio._OPENCODE_DIM_FG


def _ansi(selected):
    """GATE with SGR runs on the option row: dark-on-accent for `selected`,
    grey for the rest — the shape `capture-pane -e` hands back."""
    row = "  ┃  "
    for label in ("Allow once", "Allow always", "Reject"):
        sgr = "\x1b[38;2;10;10;10;48;2;245;167;66m" if label == selected \
            else f"\x1b[{DIM}m"
        row += f" {sgr}{label}\x1b[0m  "
    row += "ctrl+f fullscreen  ⇆ select  enter con"
    lines = GATE.splitlines()
    lines[8] = row
    return "\n".join(lines) + "\n"


def test_no_gate_on_an_idle_or_busy_screen():
    assert tmuxio.parse_opencode_gate(IDLE) is None
    assert tmuxio.parse_opencode_gate(BUSY) is None
    assert tmuxio.parse_opencode_gate(None) is None


def test_a_stray_option_word_in_the_transcript_is_not_a_gate():
    """The header is the anchor precisely so this can't false-positive."""
    assert tmuxio.parse_opencode_gate(NOT_A_GATE) is None


def test_the_gate_survives_a_truncated_footer():
    """An 80-column pane cuts "enter confirm" to "enter con" — keying on that
    hint would miss the gate on most panes."""
    assert "enter confirm" not in GATE
    assert tmuxio.parse_opencode_gate(GATE) is not None


def test_options_are_numbered_left_to_right():
    """parse_prompt's contract is 1..N, so the approval UI needs no special
    case — even though opencode itself selects with ⇆, not digits."""
    gate = tmuxio.parse_opencode_gate(GATE)
    assert [(o["num"], o["label"]) for o in gate["options"]] == [
        (1, "Allow once"), (2, "Allow always"), (3, "Reject")]


def test_the_question_comes_out_without_the_box_borders():
    gate = tmuxio.parse_opencode_gate(GATE)
    assert gate["question"] == "Access external directory /tmp — Patterns — - /tmp/*"


def test_without_a_colour_capture_nothing_claims_to_be_selected():
    """Honest rather than wrong: the highlight simply isn't in a plain capture."""
    gate = tmuxio.parse_opencode_gate(GATE)
    assert not any(o["selected"] for o in gate["options"])


def test_the_highlight_comes_from_the_colour_capture():
    for want, idx in (("Allow once", 0), ("Allow always", 1), ("Reject", 2)):
        gate = tmuxio.parse_opencode_gate(GATE, _ansi(want))
        assert [o["label"] for o in gate["options"] if o["selected"]] == [want]
        assert gate["options"][idx]["selected"]


def test_working_is_read_off_the_footer():
    assert tmuxio._OPENCODE_BUSY_RE.search(BUSY)
    assert not tmuxio._OPENCODE_BUSY_RE.search(IDLE)


def test_a_gate_outranks_the_busy_footer():
    """The pane at a gate can still carry a stale spinner line; "waiting on you"
    has to win, or the board shows THINKING for a session that's blocked."""
    assert tmuxio._OPENCODE_GATE_HEAD_RE.search(GATE)
    assert not tmuxio._OPENCODE_GATE_HEAD_RE.search(BUSY)
