"""
say() has to *confirm* the message actually left the composer.

The REPL's editor debounces keystrokes, so an Enter sent right after a literal
paste can land mid-render and be eaten as a newline — the message then sits in
the composer looking sent when it never was. say() therefore sends Enter, checks
whether the composer cleared (or the turn started), and retries until it takes.

tmux and the REPL are stubbed — nothing here shells out.
"""

import pytest

from server import tmuxio

TEXT = "run the migration and report back"

# What the pane looks like with TEXT still sitting unsent in the composer.
# Claude frames the composer between two rule lines, with no side borders.
COMPOSER = (
    "  ⏺ Done.\n"
    "────────────────────────────────\n"
    f"❯ {TEXT}\n"
    "────────────────────────────────\n"
    "  ? for shortcuts\n"
)
# Same pane after a successful submit: composer empty, text moved to transcript.
SUBMITTED = (
    f"  ⏺ {TEXT}\n"
    "────────────────────────────────\n"
    "❯ \n"
    "────────────────────────────────\n"
    "  ? for shortcuts\n"
)
# Composer still holds the text, but the turn has visibly started.
WORKING = COMPOSER + "✻ Actualizing… (4s · esc to interrupt)\n"

# grok/agy box their composer; _strip drops the border glyphs so one check
# covers every REPL we drive.
BOXED = (
    "╭──────────────────────────────╮\n"
    f"│ ❯ {TEXT}                     │\n"
    "╰──────────────────────────────╯\n"
)


@pytest.fixture
def pane(monkeypatch):
    """Drive say() against a scripted sequence of pane frames.

    `_use(frames)` makes capture_pane return each frame in turn (the last one
    repeats), and records every send-keys call.
    """
    sent = []
    monkeypatch.setattr(tmuxio.time, "sleep", lambda s: None)

    def _use(frames):
        seq = list(frames)
        state = {"i": 0}

        def fake_capture(sid, history=None):
            i = min(state["i"], len(seq) - 1)
            return seq[i]

        def fake_send(sid, *keys):
            sent.append(keys)
            if keys == ("Enter",):
                state["i"] += 1

        monkeypatch.setattr(tmuxio, "capture_pane", fake_capture)
        monkeypatch.setattr(tmuxio, "_send_keys", fake_send)
        return sent

    return _use


def _enters(sent):
    return [k for k in sent if k == ("Enter",)]


def test_submits_first_try_when_composer_clears(pane):
    sent = pane([COMPOSER, SUBMITTED])
    assert tmuxio.say("s", TEXT) == {"ok": True, "attempts": 1}
    assert len(_enters(sent)) == 1


def test_text_is_typed_literally_before_enter(pane):
    """-l keeps a word like "Enter" inside the message from becoming a keypress."""
    sent = pane([COMPOSER, SUBMITTED])
    tmuxio.say("s", TEXT)
    assert sent[0] == ("-l", "--", TEXT)
    assert sent[1] == ("Enter",)


def test_retries_enter_until_the_composer_clears(pane):
    """The swallowed-Enter case: two Enters land as newlines, the third submits."""
    sent = pane([COMPOSER, COMPOSER, COMPOSER, SUBMITTED])
    assert tmuxio.say("s", TEXT) == {"ok": True, "attempts": 3}
    assert len(_enters(sent)) == 3


def test_a_started_turn_counts_as_submitted(pane):
    """Some REPLs keep the text on screen while generating — the spinner wins."""
    sent = pane([COMPOSER, WORKING])
    assert tmuxio.say("s", TEXT) == {"ok": True, "attempts": 1}
    assert len(_enters(sent)) == 1


def test_boxed_composer_is_recognised(pane):
    """grok/agy border glyphs must not hide the pending text."""
    sent = pane([BOXED, BOXED, SUBMITTED])
    assert tmuxio.say("s", TEXT) == {"ok": True, "attempts": 2}


def test_gives_up_after_tries_but_reports_it(pane):
    """Never silently claim success — an unconfirmed submit says so."""
    sent = pane([COMPOSER])
    r = tmuxio.say("s", TEXT, tries=3)
    assert r == {"ok": True, "attempts": 3, "warning": "submit unconfirmed"}
    assert len(_enters(sent)) == 3


def test_empty_message_is_rejected_without_touching_tmux(pane):
    sent = pane([COMPOSER])
    assert tmuxio.say("s", "   ")["ok"] is False
    assert sent == []


def test_no_live_session_is_rejected(monkeypatch):
    monkeypatch.setattr(tmuxio, "capture_pane", lambda sid, history=None: None)
    assert tmuxio.say("s", TEXT) == {"ok": False, "error": "no live tmux session"}


# ---- multi-line messages go through a bracketed paste -----------------------
#
# send-keys -l writes raw bytes with no paste wrapper, and tmux drains them to
# the pty in 1022-byte chunks. Every newline arriving outside a paste bracket is
# an Enter, so a multi-line prompt submits itself in pieces and only the tail
# lands as the intended turn. _paste wraps the whole payload instead.

LINES = "# Workflow: Ship it\n\n## Stage: Build\nDo the thing.\n"

# What Claude shows once a multi-line block has been pasted: a placeholder, not
# the text — so there is no snippet left for _input_pending to look for.
PASTED = (
    "  ⏺ Done.\n"
    "────────────────────────────────\n"
    "❯ [Pasted text #1 +4 lines]\n"
    "────────────────────────────────\n"
)


@pytest.fixture
def paned(monkeypatch):
    """Like `pane`, but also records _paste calls."""
    calls = {"sent": [], "pasted": []}
    monkeypatch.setattr(tmuxio.time, "sleep", lambda s: None)

    def _use(frames):
        seq, state = list(frames), {"i": 0}

        def fake_capture(sid, history=None):
            return seq[min(state["i"], len(seq) - 1)]

        def fake_send(sid, *keys):
            calls["sent"].append(keys)
            if keys == ("Enter",):
                state["i"] += 1

        monkeypatch.setattr(tmuxio, "capture_pane", fake_capture)
        monkeypatch.setattr(tmuxio, "_send_keys", fake_send)
        monkeypatch.setattr(tmuxio, "_paste",
                            lambda sid, text: calls["pasted"].append(text))
        return calls

    return _use


def test_multiline_is_pasted_not_typed(paned):
    calls = paned([PASTED, SUBMITTED])
    tmuxio.say("s", LINES)
    assert calls["pasted"] == [LINES.rstrip("\n")]   # one atomic paste
    assert ("-l", "--", LINES) not in calls["sent"]  # never typed literally
    assert calls["sent"] == [("Enter",)]             # Enter is the only keypress


def test_single_line_still_types_literally(paned):
    calls = paned([COMPOSER, SUBMITTED])
    tmuxio.say("s", TEXT)
    assert calls["pasted"] == []
    assert calls["sent"][0] == ("-l", "--", TEXT)


def test_pasted_message_is_verified_by_an_empty_composer(paned):
    """The placeholder hides the text, so submission is judged by whether the
    composer still holds anything — retrying Enter until it clears."""
    calls = paned([PASTED, PASTED, SUBMITTED])
    assert tmuxio.say("s", LINES) == {"ok": True, "attempts": 2}
    assert len(_enters(calls["sent"])) == 2


def test_grok_multiline_is_pasted_too(paned, monkeypatch):
    calls = paned([PASTED, SUBMITTED])
    monkeypatch.setattr(tmuxio, "grok_working", lambda sid: False)
    tmuxio.grok_say("s", LINES)
    assert calls["pasted"] == [LINES.rstrip("\n")]
