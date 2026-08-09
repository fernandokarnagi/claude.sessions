"""
An answer only counts once the gate actually goes away.

A keypress into a TUI is not a promise: if the digit lands while the permission
menu is mid-repaint, the REPL swallows it. answer() used to report success
regardless, which is what let a yolo session sit on an unanswered gate forever
— the watcher recorded it as handled and never tried again.

These drive tmuxio through fake panes, so nothing here touches tmux.
"""

import pytest

from server import autonomy, tmuxio

SID = "33333333-3333-3333-3333-333333333333"

GATE = """\
⏺ Bash command

  curl -s https://example.test/hook

Do you want to proceed?

❯ 1. Yes
  2. No
"""

# Same shape, different command — a *new* gate, not the old one lingering.
OTHER_GATE = GATE.replace("curl -s https://example.test/hook", "rm -rf ./build") \
                 .replace("Do you want to proceed?", "Do you want to run this?")

DONE = """\
⏺ Bash command

  curl -s https://example.test/hook
  ⎿ (no output)

❯
"""


@pytest.fixture
def pane(monkeypatch):
    """Script the pane: each capture pops the next frame (last one repeats)."""
    def _use(*frames):
        seen = []
        state = {"frames": list(frames)}

        def fake_capture(session_id, history=None):
            return state["frames"][0] if len(state["frames"]) == 1 \
                else state["frames"].pop(0)

        monkeypatch.setattr(tmuxio, "capture_pane", fake_capture)
        monkeypatch.setattr(tmuxio, "_send_keys",
                            lambda sid, *keys: seen.append(keys))
        monkeypatch.setattr(tmuxio.time, "sleep", lambda s: None)
        return seen
    return _use


def test_gate_clearing_is_success(pane):
    keys = pane(GATE, DONE)
    assert tmuxio.answer(SID, 1) == {"ok": True}
    assert ("--", "1") in keys and ("Enter",) in keys


def test_a_different_gate_replacing_it_is_success(pane):
    # The answer landed and Claude immediately asked something else.
    pane(GATE, OTHER_GATE)
    assert tmuxio.answer(SID, 1)["ok"] is True


def test_gate_that_wont_budge_is_a_failure(pane):
    pane(GATE)                     # every capture shows the same gate
    r = tmuxio.answer(SID, 1, verify=1.2)
    assert r["ok"] is False
    assert "didn't take" in r["error"]


def test_verify_zero_skips_the_wait(pane):
    pane(GATE)
    assert tmuxio.answer(SID, 1, verify=0)["ok"] is True


def test_no_live_session_is_a_failure(monkeypatch):
    monkeypatch.setattr(tmuxio, "capture_pane", lambda sid, history=None: None)
    assert tmuxio.answer(SID, 1)["ok"] is False


def test_free_text_followup_is_not_verified(pane):
    # Choosing "No, tell Claude what to do differently" opens an input box, not
    # another gate — there's nothing to watch clear.
    keys = pane(GATE)
    assert tmuxio.answer(SID, 2, text="use the API instead")["ok"] is True
    assert ("--", "use the API instead") in keys


def test_sig_survives_a_rewrapped_repaint():
    a = tmuxio.parse_prompt(GATE)
    b = tmuxio.parse_prompt(GATE.replace("curl -s", "curl  -s"))
    assert tmuxio.prompt_sig(a) == tmuxio.prompt_sig(b)   # whitespace only
    assert tmuxio.prompt_sig(a) != tmuxio.prompt_sig(tmuxio.parse_prompt(OTHER_GATE))
    assert tmuxio.prompt_sig(None) == ""


def test_two_bash_gates_are_told_apart_by_their_command():
    # Every Bash gate asks the same question over the same two options, so
    # question+options alone fingerprinted a whole run of curls identically:
    # answer() read the *next* gate as the old one refusing to budge, and the
    # watcher read it as one it had already handled. Both then stopped pressing.
    one = GATE
    two = GATE.replace("https://example.test/hook", "https://example.test/other")
    assert tmuxio.parse_prompt(one)["question"] == tmuxio.parse_prompt(two)["question"]
    assert tmuxio.prompt_sig(tmuxio.parse_prompt(one)) \
        != tmuxio.prompt_sig(tmuxio.parse_prompt(two))


def test_a_second_identical_gate_is_answered_once_the_memory_expires(monkeypatch):
    # Same command twice in a row is one signature twice. Skipping it forever
    # parks the session; skipping it briefly just avoids double-pressing the
    # gate still fading off screen.
    autonomy._answered.clear()
    now = [1000.0]
    monkeypatch.setattr(autonomy.time, "time", lambda: now[0])

    autonomy._mark_answered(SID, "same-gate")
    assert autonomy._just_answered(SID, "same-gate")
    now[0] += autonomy.ANSWERED_TTL + 1
    assert not autonomy._just_answered(SID, "same-gate")
    autonomy._answered.clear()


# --- the watcher's half of the contract -------------------------------------

def test_watcher_gives_up_after_max_attempts():
    autonomy._fails.clear()
    sig = "some-gate"
    for _ in range(autonomy.MAX_ATTEMPTS):
        assert not autonomy._too_many_tries(SID, sig)
        autonomy._note_failure(SID, sig, "yolo", 1, "didn't take")
    assert autonomy._too_many_tries(SID, sig)
    # A *different* gate starts its own count — one stuck prompt shouldn't
    # disable auto-approve for everything that follows.
    assert not autonomy._too_many_tries(SID, "a-different-gate")
    autonomy._fails.clear()
