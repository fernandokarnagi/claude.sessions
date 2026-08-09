"""Tests for server.tmuxio.error_line — the REPL error / retry banner detector.

The banner shares its leading glyph with the spinner line but has no "… (", so
_SPINNER_RE never saw it. These tests pin down what counts as an outage and,
just as importantly, what doesn't: the pane also holds conversation text, and a
false alarm on prose the user typed is worse than no alarm at all.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import tmuxio  # noqa: E402


def _pane(line):
    """`line` embedded in a plausible pane (content above and below)."""
    return "some earlier output\n" + line + "\n\n\n"


ERRORS = [
    "✻ Unable to connect to API (ConnectionRefused) · Retrying in 0s · attempt 5/10",
    "│ ✻ API Error (429) · Retrying in 12s · attempt 2/10 │",
    "⧉ Overloaded (529)",
    "· Retrying in 8s",
    "✻ Request timed out",
]

HEALTHY = [
    "✻ Crunched for 14m 12s",                            # completed turn marker
    "✽ Extracting all document text… (4m 11s)",          # active spinner
    "❯ how do I handle a rate limit in my client?",      # what you typed
    "> Explain the retrying in 5s backoff design",        # ditto, plain prompt
    "  the API error you saw is a rate limit issue",      # assistant prose
    "│ ❯ 1. Yes                     │",                  # permission gate option
]


def test_detects_banners():
    for line in ERRORS:
        assert tmuxio.error_line(_pane(line)), line


def test_ignores_healthy_lines():
    for line in HEALTHY:
        assert tmuxio.error_line(_pane(line)) is None, line


def test_returns_the_line_stripped_of_borders():
    got = tmuxio.error_line(_pane("│ ✻ API Error (429) · Retrying in 12s · attempt 2/10 │"))
    assert got == "✻ API Error (429) · Retrying in 12s · attempt 2/10"


def test_newest_banner_wins():
    """Scanned bottom-up — the lowest banner is the current one."""
    screen = "✻ API Error (500)\n✻ Unable to connect to API · Retrying in 3s · attempt 2/10"
    assert "attempt 2/10" in tmuxio.error_line(screen)


def test_no_screen_is_no_error():
    assert tmuxio.error_line(None) is None
    assert tmuxio.error_line("") is None


def test_idle_input_box_is_healthy():
    """A recovered session shows its input box again → the alert clears itself."""
    assert tmuxio.error_line("──────────\n❯ \n──────────") is None


def test_banner_is_capped():
    long = "✻ API Error " + "x" * 500
    assert len(tmuxio.error_line(_pane(long))) <= tmuxio._ERR_MAX
