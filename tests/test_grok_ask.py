"""
Reading grok's question card (`ask_user_question`) off a tmux pane.

This is not a permission gate. The agent stops and asks the human to *choose*,
and nothing in grok's transcript says it is waiting — the card lives on the
pane. Before this, a grok session parked on one read as WAITING like any other
idle session and never reached the To-do inbox.

grok keys its rows rather than numbering them: `1`-`9`, then `a`-`f`, with `z`
reserved for the free-text row (from grok's own key table, shipped in the
binary). The frame below is reconstructed from a screenshot of a live card.
Nothing here runs tmux.
"""

import json

import pytest

from server import autonomy, tmuxio

SID = "6e7b8da7-f3de-4f2d-9e60-03dd1642db86"

CARD = """\
  Read Terms and Privacy Policy.

 │
 │  When a workflow node fails (agent 429, swarm error, missing agent), what happens?
 │
 │
 │  1 (○) Fail-fast (Recommended)      Job status failed. Remaining nodes skipped.
 │  2 (○) Skip and continue            Failed node records an error; downstream nodes still run.
 │  3 (○) Retry once, then fail-fast   One automatic retry per node, then stop the workflow.
 │  z (○) Type your answer here
 │
 │  ↑/↓ navigate · y copy

Tab/Space:question  │  Ctrl+e:expand thinking  │  Ctrl+c:cancel  │  Ctrl+x:shortcuts
"""

# The same session with the card answered and gone.
IDLE = """\
  ╭──────────────────────────────────────────────────────────╮
  │ ❯                                                        │
  ╰──────────────────────── Grok 4.6 (high) · always-approve ─╯

  Shift+Tab:mode  │  Ctrl+x:shortcuts
"""


def test_parses_the_question_and_every_row():
    ask = tmuxio.parse_grok_ask(CARD)
    assert ask["question"].startswith("When a workflow node fails")
    assert [o["num"] for o in ask["options"]] == [1, 2, 3, 4]
    assert [o["label"] for o in ask["options"]] == [
        "Fail-fast (Recommended)", "Skip and continue",
        "Retry once, then fail-fast", "Type your answer here"]
    assert ask["stage"] == "ask"


def test_the_description_column_is_kept():
    """The label alone doesn't say what you're picking — "Skip and continue"
    means nothing without the sentence beside it."""
    ask = tmuxio.parse_grok_ask(CARD)
    assert ask["options"][1]["desc"].startswith("Failed node records an error")
    assert ask["options"][3]["desc"] == ""      # the free-text row has none


def test_rows_carry_the_key_grok_actually_binds():
    """`num` is 1..N for the UI; `press` is the keystroke. They part company on
    the free-text row, which grok keys `z` however far down the list it sits."""
    ask = tmuxio.parse_grok_ask(CARD)
    assert [o["press"] for o in ask["options"]] == ["1", "2", "3", "z"]
    assert ask["custom"] == 4


def test_the_free_text_row_is_not_reported_as_agys_key():
    """agy's gate puts its own option ids in `key`, and the dashboard routes an
    answer on that field — so grok's keystroke must not land there."""
    ask = tmuxio.parse_grok_ask(CARD)
    assert all("key" not in o for o in ask["options"])


def test_an_idle_pane_has_no_card():
    assert tmuxio.parse_grok_ask(IDLE) is None
    assert tmuxio.parse_grok_ask("") is None
    assert tmuxio.parse_grok_ask(None) is None


def test_a_selected_row_reads_as_selected():
    ask = tmuxio.parse_grok_ask(CARD.replace("2 (○)", "2 (●)"))
    assert [o["selected"] for o in ask["options"]] == [False, True, False, False]


def test_ascii_fallback_glyphs_still_parse():
    """grok degrades its glyphs on terminals that can't draw them; the card is
    the same card."""
    ask = tmuxio.parse_grok_ask(CARD.replace("(○)", "( )").replace("1 ( )", "1 (*)"))
    assert len(ask["options"]) == 4
    assert ask["options"][0]["selected"] is True


def test_a_multi_select_card_uses_checkboxes():
    ask = tmuxio.parse_grok_ask(CARD.replace("(○)", "[ ]").replace("2 [ ]", "2 [x]"))
    assert [o["selected"] for o in ask["options"]] == [False, True, False, False]


def test_prose_that_merely_looks_keyed_is_not_a_card():
    """Without the card's footer there is no card — a transcript can hold
    anything."""
    assert tmuxio.parse_grok_ask(CARD.replace("↑/↓ navigate · y copy", "")
                                     .replace("Tab/Space:question", "Shift+Tab:mode")) is None


def test_a_single_row_is_not_a_card():
    one = CARD.replace(" │  2 (○) Skip and continue            "
                       "Failed node records an error; downstream nodes still run.\n", "") \
              .replace(" │  3 (○) Retry once, then fail-fast   "
                       "One automatic retry per node, then stop the workflow.\n", "") \
              .replace(" │  z (○) Type your answer here\n", "")
    assert tmuxio.parse_grok_ask(one) is None


def test_a_keyed_line_further_up_the_scrollback_does_not_join_the_run():
    """The rows are read bottom-up and stop at the first line that isn't one,
    so an echoed row above the card cannot extend it."""
    noisy = CARD.replace("  Read Terms and Privacy Policy.",
                         "  9 (○) An old row from the last card")
    ask = tmuxio.parse_grok_ask(noisy)
    assert len(ask["options"]) == 4


def test_a_wrapped_question_is_joined():
    wrapped = CARD.replace(
        " │  When a workflow node fails (agent 429, swarm error, missing agent), what happens?",
        " │  When a workflow node fails (agent 429, swarm error,\n │  missing agent), what happens?")
    ask = tmuxio.parse_grok_ask(wrapped)
    assert ask["question"] == (
        "When a workflow node fails (agent 429, swarm error, missing agent), what happens?")


def test_a_wrapped_description_stays_with_its_row():
    """grok wraps a description too wide for its column onto the next line.
    The wrap carries no key, and read bottom-up it comes before its own row."""
    wrapped = CARD.replace(
        " │  1 (○) Fail-fast (Recommended)      Job status failed. Remaining nodes skipped.",
        " │  1 (○) Fail-fast (Recommended)      Job status failed. Remaining nodes\n"
        " │                                     skipped.")
    ask = tmuxio.parse_grok_ask(wrapped)
    assert ask["question"].startswith("When a workflow node fails")
    assert [o["num"] for o in ask["options"]] == [1, 2, 3, 4]
    assert ask["options"][0]["label"] == "Fail-fast (Recommended)"
    assert ask["options"][0]["desc"] == "Job status failed. Remaining nodes skipped."


def test_a_wrap_does_not_shrink_a_two_row_card_out_of_existence():
    """Two rows is the whole card. A wrap under the first one used to cut the
    run to one row, and the card vanished from the dashboard."""
    two = CARD.replace(" │  2 (○) Skip and continue            "
                       "Failed node records an error; downstream nodes still run.\n", "") \
              .replace(" │  3 (○) Retry once, then fail-fast   "
                       "One automatic retry per node, then stop the workflow.\n", "") \
              .replace(" │  1 (○) Fail-fast (Recommended)      "
                       "Job status failed. Remaining nodes skipped.",
                       " │  1 (○) Fail-fast (Recommended)      Job status failed. Remaining\n"
                       " │                                     nodes skipped.")
    ask = tmuxio.parse_grok_ask(two)
    assert [o["press"] for o in ask["options"]] == ["1", "z"]
    assert ask["options"][0]["desc"] == "Job status failed. Remaining nodes skipped."


# ---- autonomy ---------------------------------------------------------------

def test_autonomy_never_answers_a_question_card():
    """"Which of these three failure policies do you want?" is a decision about
    the work. No trust level implies an answer to it, yolo included."""
    ask = tmuxio.parse_grok_ask(CARD)
    assert autonomy.is_choice(ask) is True
    for level in ("manual", "auto-safe", "yolo"):
        assert autonomy.decide(level, ask) is None


# ---- answering --------------------------------------------------------------

@pytest.fixture
def pane(monkeypatch):
    """Script the pane and record every keystroke sent to it."""
    def _use(*frames):
        sent = []
        state = {"frames": list(frames)}

        def fake_capture(session_id, history=None):
            f = state["frames"]
            return f.pop(0) if len(f) > 1 else f[0]

        monkeypatch.setattr(tmuxio, "capture_pane", fake_capture)
        monkeypatch.setattr(tmuxio, "_send_keys",
                            lambda sid, *args: sent.append(args))
        monkeypatch.setattr(tmuxio.time, "sleep", lambda s: None)
        return sent
    return _use


def test_answering_presses_the_row_key(pane):
    sent = pane(CARD, IDLE)
    r = tmuxio.grok_answer(SID, 2)
    assert r["ok"] is True and r["label"] == "Skip and continue"
    assert sent[0] == ("--", "2")


def test_a_card_that_only_selects_gets_an_enter(pane):
    """grok's key picks the row; Enter is what submits the last question. Which
    of the two a card needs isn't knowable from the pane, so the Enter follows
    only when the card is still up."""
    sent = pane(CARD, CARD, IDLE)
    r = tmuxio.grok_answer(SID, 1)
    assert r["ok"] is True
    assert sent == [("--", "1"), ("Enter",)]


def test_a_card_that_never_goes_away_is_reported_honestly(pane):
    """A keypress into a TUI is not a promise. Claiming success here is what
    lets the watcher record an unanswered card as handled."""
    sent = pane(CARD)
    r = tmuxio.grok_answer(SID, 1, verify=1.2)
    assert r["ok"] is False and "didn't take" in r["error"]


def test_the_free_text_row_needs_text(pane):
    pane(CARD)
    r = tmuxio.grok_answer(SID, 4)
    assert r["ok"] is False and "free-text" in r["error"]


def test_the_free_text_row_types_the_answer(pane):
    sent = pane(CARD, IDLE)
    r = tmuxio.grok_answer(SID, 4, "retry twice, then skip")
    assert r["ok"] is True
    assert sent == [("--", "z"), ("-l", "--", "retry twice, then skip"), ("Enter",)]


def test_answering_an_option_that_is_not_there(pane):
    pane(CARD)
    r = tmuxio.grok_answer(SID, 9)
    assert r["ok"] is False and "not on this card" in r["error"]


def test_answering_with_no_card_up(pane):
    pane(IDLE)
    r = tmuxio.grok_answer(SID, 1)
    assert r["ok"] is False and "no question card" in r["error"]


def test_answering_a_dead_pane(monkeypatch):
    monkeypatch.setattr(tmuxio, "capture_pane", lambda sid, history=None: None)
    r = tmuxio.grok_answer(SID, 1)
    assert r["ok"] is False and "no live tmux session" in r["error"]


# ---- over HTTP ---------------------------------------------------------------

@pytest.fixture
def grok_session(tmp_path, monkeypatch):
    """One grok session on disk, live in tmux, sitting on the card."""
    from server import grokparser

    root = tmp_path / "sessions" / "%2Fproj" / SID
    root.mkdir(parents=True)
    (root / "summary.json").write_text(json.dumps({
        "info": {"id": SID, "cwd": "/proj"},
        "created_at": "2026-08-27T13:00:00.000000Z",
        "updated_at": "2026-08-27T13:20:00.000000Z",
        "generated_title": "Phase 3 shape",
        "num_messages": 2,
        "current_model_id": "grok-4.6",
    }), encoding="utf-8")
    (root / "chat_history.jsonl").write_text(
        json.dumps({"type": "user", "content": "plan phase 3", "prompt_index": 0}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(grokparser, "SESS_ROOT", str(tmp_path / "sessions"))
    grokparser._DIR_CACHE.clear()
    grokparser._SUMM_CACHE.clear()
    grokparser._TS_CACHE.clear()
    monkeypatch.setattr(tmuxio, "tmux_sessions", lambda: {SID})
    monkeypatch.setattr(tmuxio, "capture_pane", lambda sid, history=None: CARD)
    monkeypatch.setattr(tmuxio, "capture_pane_ansi", lambda sid: None)
    monkeypatch.setattr(tmuxio, "error_lines", lambda: {})
    return SID


def test_a_parked_card_makes_the_session_need_you(grok_session, monkeypatch):
    """The whole point: grok writes nothing while it waits, so without the pane
    read this session sat in the board looking merely idle."""
    from server import app as app_module

    rows = app_module._grok_summaries({}, set(), set(), "exclude", {SID})
    row = next(r for r in rows if r["session_id"] == SID)
    assert row["pending_approval"] is True
    assert row["status"] == "WAITING"


def test_a_working_pane_is_not_a_card(grok_session, monkeypatch):
    from server import app as app_module

    monkeypatch.setattr(tmuxio, "capture_pane", lambda sid, history=None: IDLE)
    monkeypatch.setattr(tmuxio, "grok_working", lambda sid: True)
    rows = app_module._grok_summaries({}, set(), set(), "exclude", {SID})
    row = next(r for r in rows if r["session_id"] == SID)
    assert row["pending_approval"] is False and row["status"] == "THINKING"


def test_the_tmux_endpoint_serves_the_card(grok_session):
    from fastapi.testclient import TestClient
    from server.app import app

    body = TestClient(app).get(f"/api/sessions/{SID}/tmux").json()
    assert body["prompt"]["stage"] == "ask"
    assert body["prompt"]["options"][0]["label"] == "Fail-fast (Recommended)"


def test_answering_a_grok_session_goes_through_groks_key_map(grok_session, monkeypatch):
    """Routing matters: the shared answer() presses the option *number*, and on
    grok's card the free-text row is `z`, not 4."""
    from fastapi.testclient import TestClient
    from server.app import app

    calls = []
    monkeypatch.setattr(tmuxio, "grok_answer",
                        lambda sid, choice, text="": calls.append((sid, choice, text))
                        or {"ok": True})
    r = TestClient(app).post(f"/api/sessions/{SID}/answer", json={"choice": 4,
                                                                 "text": "skip it"})
    assert r.status_code == 200
    assert calls == [(SID, 4, "skip it")]
