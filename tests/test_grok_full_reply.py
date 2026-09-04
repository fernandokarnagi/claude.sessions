"""
A long grok reply reaches the history view whole.

Every turn used to be cut at 2000 characters on its way out of the store. The
board card only shows three lines and clips them in CSS, so nothing was missing
there — but the detail view read the same list, and long answers arrived with
their ending gone and nothing on screen to say so.

Everything here is a fake ~/.grok tree; nothing reads the real one.
"""

import json

import pytest

from server import grokparser

LONG = "x" * 7000


@pytest.fixture
def session(tmp_path, monkeypatch):
    """One grok session whose single answer runs well past the preview cap."""
    root = tmp_path / "sessions" / "%2Fproj" / "sid-long"
    root.mkdir(parents=True)
    monkeypatch.setattr(grokparser, "SESS_ROOT", str(tmp_path / "sessions"))
    grokparser._DIR_CACHE.clear()
    grokparser._SUMM_CACHE.clear()
    grokparser._TS_CACHE.clear()

    (root / "summary.json").write_text(json.dumps({
        "info": {"id": "sid-long", "cwd": "/proj"},
        "created_at": "2026-08-18T15:13:21.015882Z",
        "updated_at": "2026-08-18T15:20:00.000000Z",
        "generated_title": "a long answer",
        "num_messages": 2,
        "current_model_id": "grok-4.6",
    }), encoding="utf-8")

    (root / "chat_history.jsonl").write_text("".join(
        json.dumps(r) + "\n" for r in [
            {"type": "user", "content": "explain everything", "prompt_index": 0},
            {"type": "assistant", "content": LONG},
        ]), encoding="utf-8")
    return root


def test_the_detail_view_keeps_the_whole_reply(session):
    detail = grokparser.get_session("sid-long")
    reply = next(a for a in detail["activities"] if a["role"] == "assistant")
    assert reply["text"] == LONG


def test_the_board_preview_is_not_trimmed_either(session):
    acts = grokparser._activities(str(session), grokparser._MAX_ACT)
    reply = next(a for a in acts if a["role"] == "assistant")
    assert reply["text"] == LONG
