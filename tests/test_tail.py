"""Tests for parser.tail_activities — incremental transcript reading."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import parser  # noqa: E402


def _evt(text):
    return json.dumps({
        "type": "assistant", "timestamp": "2026-05-31T10:00:00.000Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    })


@pytest.fixture
def jsonl(tmp_path):
    return tmp_path / "s.jsonl"


def test_tail_from_zero_reads_all(jsonl):
    jsonl.write_text(_evt("one") + "\n" + _evt("two") + "\n")
    acts, off = parser.tail_activities(str(jsonl), 0)
    assert [a["text"] for a in acts] == ["one", "two"]   # chronological
    assert off == jsonl.stat().st_size


def test_tail_incremental(jsonl):
    jsonl.write_text(_evt("one") + "\n")
    acts1, off1 = parser.tail_activities(str(jsonl), 0)
    assert [a["text"] for a in acts1] == ["one"]
    # append a new event; tail from the previous offset returns only the new one
    with open(jsonl, "a") as fh:
        fh.write(_evt("two") + "\n")
    acts2, off2 = parser.tail_activities(str(jsonl), off1)
    assert [a["text"] for a in acts2] == ["two"]
    assert off2 == jsonl.stat().st_size


def test_tail_no_new_data(jsonl):
    jsonl.write_text(_evt("one") + "\n")
    _, off = parser.tail_activities(str(jsonl), 0)
    acts, off2 = parser.tail_activities(str(jsonl), off)
    assert acts == [] and off2 == off


def test_tail_ignores_partial_trailing_line(jsonl):
    jsonl.write_text(_evt("one") + "\n")
    _, off = parser.tail_activities(str(jsonl), 0)
    # write a partial line (no trailing newline yet) — must NOT be consumed
    with open(jsonl, "a") as fh:
        fh.write('{"type":"assistant","message":{"content":[{"type":"text","text":"par')
    acts, off2 = parser.tail_activities(str(jsonl), off)
    assert acts == []
    assert off2 == off  # offset unchanged until the line completes


def test_tail_resync_on_shrink(jsonl):
    jsonl.write_text(_evt("one") + "\n")
    size = jsonl.stat().st_size
    # offset beyond EOF (file rotated/shrank) -> resync pointer, no content
    acts, off = parser.tail_activities(str(jsonl), size + 999)
    assert acts == [] and off == size
