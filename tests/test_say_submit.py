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
