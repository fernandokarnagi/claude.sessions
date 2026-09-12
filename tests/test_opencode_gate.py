"""
Reading opencode's permission gate off a tmux pane.

The gate is a three-stage dialog, not one screen. "Allow always" and "Reject"
don't answer the request — each swaps the dialog for a follow-up under its own
header ("Always allow" → Confirm/Cancel, "Reject permission" → a reason box).
Both frames have to be recognised too, or the board calls a blocked session idle.

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


# Stage 2 of "Allow always": the request is *not* answered yet — opencode wants
# a Confirm before it writes the pattern into the always-list.
#
# Unlike GATE above, this frame and REJECT are reconstructed from opencode's own
# dialog code (header text, option labels and body copy taken verbatim from it)
# rather than captured, so the layout around them is indicative — the parser
# keys on the header and the option row, both of which are exact.
CONFIRM = """\
  ┃  △ Always allow
  ┃
  ┃  This will allow the following patterns until OpenCode is restarted.
  ┃
  ┃  - /tmp/*
  ┃
  ┃
  ┃   Confirm   Cancel  ⇆ select  enter confirm  esc can
  ┃
"""

# Stage 2 of "Reject": a free-text box for the reason. No option row at all —
# the actions are the footer's key hints.
REJECT = """\
  ┃  △ Reject permission
  ┃ Tell OpenCode what to do differently
  ┃ ╭──────────────────╮
  ┃ │                  │
  ┃ ╰──────────────────╯
  ┃  enter confirm  esc cancel
"""


def _ansi_confirm(selected):
    """CONFIRM's option row with SGR runs, same shape as _ansi()."""
    row = "  ┃  "
    for label in ("Confirm", "Cancel"):
        sgr = "\x1b[38;2;10;10;10;48;2;245;167;66m" if label == selected \
            else f"\x1b[{DIM}m"
        row += f" {sgr}{label}\x1b[0m  "
    row += "⇆ select  enter confirm  esc can"
    lines = CONFIRM.splitlines()
    lines[7] = row
    return "\n".join(lines) + "\n"


def test_the_allow_always_confirmation_is_a_gate():
    """The bug: only "Permission required" was recognised, so the pane sitting
    at this frame read as idle and the session hung on an unanswered dialog."""
    gate = tmuxio.parse_opencode_gate(CONFIRM)
    assert gate is not None
    assert gate["stage"] == "always"
    assert [(o["num"], o["label"]) for o in gate["options"]] == [
        (1, "Confirm"), (2, "Cancel")]


def test_the_confirmation_says_what_it_will_allow():
    gate = tmuxio.parse_opencode_gate(CONFIRM)
    assert gate["question"] == (
        "This will allow the following patterns until OpenCode is restarted. "
        "— - /tmp/*")


def test_the_confirmation_footer_hints_are_not_options():
    """Its own hints read "enter confirm  esc cancel" — lowercase, which is the
    only thing keeping them out of a row that also holds Confirm and Cancel."""
    gate = tmuxio.parse_opencode_gate(CONFIRM)
    assert [o["label"] for o in gate["options"]] == ["Confirm", "Cancel"]


def test_the_confirmation_highlight_also_comes_from_colour():
    for want in ("Confirm", "Cancel"):
        gate = tmuxio.parse_opencode_gate(CONFIRM, _ansi_confirm(want))
        assert [o["label"] for o in gate["options"] if o["selected"]] == [want]


def test_the_reject_reason_box_is_a_gate():
    """No option row here, so the row-based path can't see it; the header has
    to carry it. Its actions are the two key hints."""
    gate = tmuxio.parse_opencode_gate(REJECT)
    assert gate is not None
    assert gate["stage"] == "reject"
    assert [(o["num"], o["label"]) for o in gate["options"]] == [
        (1, "Confirm"), (2, "Cancel")]
    assert gate["question"] == "Tell OpenCode what to do differently"


def test_the_first_stage_is_still_named():
    assert tmuxio.parse_opencode_gate(GATE)["stage"] == "permission"


def test_a_follow_up_stage_outranks_the_busy_footer_too():
    for frame in (CONFIRM, REJECT):
        assert tmuxio._OPENCODE_GATE_HEAD_RE.search(frame)


# The `question` wizard's closing Review page, captured from a live opencode
# 1.18.30 on a 120-column pane. A wizard of more than one page ends here rather
# than committing on the last answer; a lone single-select question submits on
# the digit and never shows it, which is why it appears only sometimes.
#
# It is what the ask parser cannot see: no numbered rows, and a footer reading
# "enter submit" where the question pages read "enter confirm" (single select)
# or "enter toggle" (multi). Both of opencode_pending's old anchors missed it,
# so a session blocked on it was reported idle and nobody ever pressed Enter.
REVIEW = """\
  ┃
  ┃   Favourite colour   Favourite pet   Favourite drink   Confirm
  ┃
  ┃  Review
  ┃
  ┃  Favourite colour: Red
  ┃
  ┃  Favourite pet: Cat
  ┃
  ┃  Favourite drink: Tea
  ┃
  ┃  ⇆ tab  enter submit  esc dismiss
  ┃
"""

# The same page after a one-question wizard. The dialog covers opencode's own
# footer, so there is no "esc interrupt" left on screen to read either way.
REVIEW_ONE = """\
  ┃
  ┃   Languages   Confirm
  ┃
  ┃  Review
  ┃
  ┃  Languages: Python
  ┃
  ┃  ⇆ tab  enter submit  esc dismiss
  ┃
"""

# The question page the wizard sits on *before* the review, for contrast: it
# has the numbered rows and the "↑↓ select" footer the ask parser keys on.
ASK = """\
  ┃
  ┃   Favourite colour   Favourite pet   Favourite drink   Confirm
  ┃
  ┃  What is your favourite colour?
  ┃
  ┃  1. Red
  ┃     Red is bold and energetic.
  ┃  2. Blue
  ┃     Blue is calm and steady.
  ┃  3. Type your own answer
  ┃
  ┃  ⇆ tab  ↑↓ select  enter confirm  esc dismiss
  ┃
"""


def test_the_review_page_is_a_pending_card():
    """The bug: the wizard's last page answered to neither anchor, so a session
    blocked on it read as idle and the confirmation never reached the board."""
    for frame in (REVIEW, REVIEW_ONE):
        card = tmuxio.parse_opencode_review(frame)
        assert card is not None
        assert card["stage"] == "review"


def test_the_review_page_offers_submit_and_dismiss():
    """Enter sends the answers, Escape drops the question. Numbered 1..N like
    every other card so the board's approval UI needs no special case."""
    card = tmuxio.parse_opencode_review(REVIEW)
    assert [(o["num"], o["label"]) for o in card["options"]] == [
        (1, "Submit"), (2, "Dismiss")]
    assert card["options"][0]["selected"]      # Enter is the default


def test_the_review_page_carries_the_answers_being_submitted():
    """Confirming blind is the whole failure mode this replaces, so the answers
    have to travel with the card."""
    assert tmuxio.parse_opencode_review(REVIEW)["question"] == (
        "Review — Favourite colour: Red — Favourite pet: Cat "
        "— Favourite drink: Tea")


def test_the_tab_strip_is_not_mistaken_for_an_answer():
    """It sits above the heading, which is why the body is read downwards from
    there rather than upwards from the footer."""
    assert "Confirm" not in tmuxio.parse_opencode_review(REVIEW)["question"]


def test_a_question_page_is_not_a_review_page():
    """"enter confirm" is not "enter submit" — the ask parser still owns the
    numbered pages, and the review parser must not claim them."""
    assert tmuxio.parse_opencode_review(ASK) is None
    assert tmuxio.parse_opencode_ask(ASK) is not None


def test_neither_gate_stage_is_a_review_page():
    for frame in (GATE, CONFIRM, REJECT):
        assert tmuxio.parse_opencode_review(frame) is None


def test_an_idle_or_busy_screen_is_not_a_review_page():
    assert tmuxio.parse_opencode_review(IDLE) is None
    assert tmuxio.parse_opencode_review(BUSY) is None
    assert tmuxio.parse_opencode_review(None) is None


def test_the_review_page_outranks_the_busy_footer():
    """Its dialog covers opencode's footer, but a stale spinner line above it
    would otherwise leave the session reading THINKING while it is blocked."""
    assert tmuxio._OPENCODE_REVIEW_FOOT_RE.search(REVIEW)
    assert not tmuxio._OPENCODE_REVIEW_FOOT_RE.search(BUSY)
    assert not tmuxio._OPENCODE_REVIEW_FOOT_RE.search(IDLE)


# A multiSelect question. Same numbered dialog as ASK, but every row carries a
# box and the footer says "enter toggle" instead of "enter confirm" — a digit
# ticks a box and the page stays up, so the digit is not the answer. Real
# captures from a 120-column pane; MULTI_TICKED is the same page after 2 and 4
# were pressed.
MULTI = """\
  ┃
  ┃   Languages   Confirm
  ┃
  ┃  Which languages should I use? (select all that apply)
  ┃
  ┃  1. [ ] Python
  ┃     Python
  ┃  2. [ ] TypeScript
  ┃     TypeScript
  ┃  3. [ ] Go
  ┃     Go
  ┃  4. [ ] Rust
  ┃     Rust
  ┃  5. [ ] Type your own answer
  ┃
  ┃  ⇆ tab  ↑↓ select  enter toggle  esc dismiss
  ┃
"""

MULTI_TICKED = """\
  ┃
  ┃   Langs   Confirm
  ┃
  ┃  Pick languages (select all that apply)
  ┃
  ┃  1. [ ] Python
  ┃     Python
  ┃  2. [✓] TypeScript
  ┃     TypeScript
  ┃  3. [ ] Go
  ┃     Go
  ┃  4. [✓] Rust
  ┃     Rust
  ┃  5. [ ] Type your own answer
  ┃
  ┃  ⇆ tab  ↑↓ select  enter toggle  esc dismiss
  ┃
"""


def test_a_checkbox_question_is_flagged_as_multi():
    """The board draws a tick-many panel off this flag. Without it the page got
    the pick-one buttons, whose digit only ticks a box."""
    assert tmuxio.parse_opencode_ask(MULTI)["multi"]
    assert not tmuxio.parse_opencode_ask(ASK).get("multi")


def test_the_box_is_split_off_the_label():
    """The label reaches the board as the option's text, not "[ ] Python"."""
    card = tmuxio.parse_opencode_ask(MULTI)
    assert [(o["num"], o["label"]) for o in card["options"]] == [
        (1, "Python"), (2, "TypeScript"), (3, "Go"), (4, "Rust")]


def test_a_ticked_box_comes_back_checked():
    """The board pre-ticks from this, and sends back only what changed — so a
    box read the wrong way round un-ticks an answer the user meant to keep."""
    checked = {o["label"]: o["checked"]
               for o in tmuxio.parse_opencode_ask(MULTI_TICKED)["options"]}
    assert checked == {"Python": False, "TypeScript": True,
                       "Go": False, "Rust": True}
    assert not any(o["checked"]
                   for o in tmuxio.parse_opencode_ask(MULTI)["options"])


def test_the_free_text_row_is_not_a_checkbox():
    """"Type your own answer" wears a box too, but it opens a textarea rather
    than ticking, so it stays out of the list and keeps its own number."""
    card = tmuxio.parse_opencode_ask(MULTI)
    assert card["custom"] == 5
    assert all(o["num"] != 5 for o in card["options"])


def test_each_row_keeps_its_description():
    card = tmuxio.parse_opencode_ask(MULTI)
    assert [o["desc"] for o in card["options"]] == [
        "Python", "TypeScript", "Go", "Rust"]


def test_a_checkbox_question_refuses_a_pick_one_answer():
    """Sending the digit would tick a box and report the question answered,
    which is how the wizard used to stall on the same page."""
    out = tmuxio._opencode_ask_answer("nope", tmuxio.parse_opencode_ask(MULTI),
                                      1, None)
    assert not out["ok"]
    assert "answer-multi" in out["error"]
