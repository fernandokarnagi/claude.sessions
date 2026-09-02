"""
Pinned messages — the store and its endpoints.

A pin is a copy of one message you want to keep at hand. Nothing is ever sent
from it, so the store's whole contract is: add, list newest-first, delete one,
delete all, survive a /clear rekey. Pinning through the endpoint does one more
thing — it queues the task to send about the pinned message, and the two records
point at each other.

Every test points the store at a tmp file, so the real state under server/ is
never touched.
"""

import pytest
from fastapi.testclient import TestClient

from server import pins, summarizer, tasks
from server.app import app

SID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(pins, "_PATH", str(tmp_path / "pins.json"))
    monkeypatch.setattr(tasks, "_PATH", str(tmp_path / "tasks.json"))

    # Pinning writes a task through the summarizer; the real one shells out to
    # `claude`. Stubbed here so the endpoint tests stay offline and fast.
    async def _fake(text, kind="assistant"):
        return f"[{kind}] follow up on: {text}"

    monkeypatch.setattr(summarizer, "as_pin_task", _fake)


@pytest.fixture
def client():
    return TestClient(app)


def test_add_and_list():
    rec = pins.add_pin(SID, "  ship the migration  ", kind="user", ts="2026-08-22T01:00:00Z")
    assert rec["text"] == "ship the migration"      # stored trimmed
    assert rec["kind"] == "user"
    assert rec["ts"] == "2026-08-22T01:00:00Z"
    assert pins.list_pins(SID) == [rec]
    assert pins.count(SID) == 1


def test_newest_first():
    a = pins.add_pin(SID, "first")
    b = pins.add_pin(SID, "second")
    assert [p["id"] for p in pins.list_pins(SID)] == [b["id"], a["id"]]


def test_same_text_pins_once():
    """The button sits on every message and survives a repaint, so a double
    click must not stack duplicates."""
    a = pins.add_pin(SID, "same text")
    b = pins.add_pin(SID, "same text")
    assert a["id"] == b["id"]
    assert pins.count(SID) == 1


def test_unknown_kind_falls_back():
    assert pins.add_pin(SID, "x", kind="tool")["kind"] == "assistant"


def test_delete_one():
    a = pins.add_pin(SID, "keep")
    b = pins.add_pin(SID, "drop")
    assert pins.delete_pin(SID, b["id"]) is True
    assert [p["id"] for p in pins.list_pins(SID)] == [a["id"]]
    assert pins.delete_pin(SID, b["id"]) is False   # already gone


def test_delete_all_is_per_session():
    pins.add_pin(SID, "one")
    pins.add_pin(SID, "two")
    pins.add_pin(OTHER, "theirs")
    assert pins.delete_all(SID) == 2
    assert pins.list_pins(SID) == []
    assert len(pins.list_pins(OTHER)) == 1
    assert pins.delete_all(SID) == 0


def test_counts_by_session():
    pins.add_pin(SID, "one")
    pins.add_pin(OTHER, "a")
    pins.add_pin(OTHER, "b")
    assert pins.counts_by_session() == {SID: 1, OTHER: 2}


def test_rekey_moves_every_pin():
    """Unlike tasks, nothing is 'already dealt with' — a pin is a note about the
    work, so all of it follows the session onto its new id."""
    pins.add_pin(SID, "one")
    pins.add_pin(SID, "two")
    pins.rekey(SID, OTHER)
    assert len(pins.list_pins(OTHER)) == 2
    assert pins.list_pins(SID) == []


def test_rekey_appends():
    pins.add_pin(OTHER, "already there")
    pins.add_pin(SID, "moving")
    pins.rekey(SID, OTHER)
    assert {p["text"] for p in pins.list_pins(OTHER)} == {"already there", "moving"}


def test_rekey_same_id_is_a_noop():
    pins.add_pin(SID, "one")
    pins.rekey(SID, SID)
    assert len(pins.list_pins(SID)) == 1


# ---- endpoints ------------------------------------------------------------

def test_api_add_list_delete(client):
    r = client.post(f"/api/sessions/{SID}/pins",
                    json={"text": "remember this", "kind": "assistant"})
    assert r.status_code == 200
    pid = r.json()["id"]

    r = client.get(f"/api/sessions/{SID}/pins")
    assert [p["text"] for p in r.json()["pins"]] == ["remember this"]

    assert client.delete(f"/api/sessions/{SID}/pins/{pid}").status_code == 200
    assert client.get(f"/api/sessions/{SID}/pins").json()["pins"] == []


def test_api_pinning_queues_a_linked_task(client):
    """The point of the pin button: the message is kept verbatim and the task to
    send about it is queued alongside, each pointing at the other."""
    pin = client.post(f"/api/sessions/{SID}/pins",
                      json={"text": "the migration is blocked", "kind": "assistant"}).json()
    queued = tasks.list_tasks(SID)
    assert len(queued) == 1
    task = queued[0]
    assert pin["task_id"] == task["id"]
    assert task["pin_id"] == pin["id"]
    assert task["text"] == "[assistant] follow up on: the migration is blocked"
    # only the task is rewritten — the pin keeps the message as it was
    assert pin["text"] == "the migration is blocked"


def test_api_task_falls_back_to_the_pinned_text(client, monkeypatch):
    """A summarizer that is missing or times out must not swallow the pin: the
    task is queued with the pinned text, which you can edit."""
    async def _none(text, kind="assistant"):
        return None

    monkeypatch.setattr(summarizer, "as_pin_task", _none)
    client.post(f"/api/sessions/{SID}/pins", json={"text": "raw text"})
    assert [t["text"] for t in tasks.list_tasks(SID)] == ["raw text"]


def test_api_re_pinning_does_not_queue_a_second_task(client):
    """The button survives a repaint, so a double click must stack neither a
    pin nor a task."""
    a = client.post(f"/api/sessions/{SID}/pins", json={"text": "same"}).json()
    b = client.post(f"/api/sessions/{SID}/pins", json={"text": "same"}).json()
    assert a["id"] == b["id"] and a["task_id"] == b["task_id"]
    assert len(tasks.list_tasks(SID)) == 1


def test_api_re_pinning_after_the_task_was_deleted_queues_again(client):
    """Deleting the task is how you say you're done with it. Pinning again is
    then a fresh ask, not a no-op."""
    pin = client.post(f"/api/sessions/{SID}/pins", json={"text": "same"}).json()
    tasks.delete_task(SID, pin["task_id"])
    again = client.post(f"/api/sessions/{SID}/pins", json={"text": "same"}).json()
    assert again["id"] == pin["id"]                 # still one pin
    assert again["task_id"] != pin["task_id"]
    assert len(tasks.list_tasks(SID)) == 1


def test_api_an_archived_task_is_not_re_queued(client):
    """Archived is not gone — the pin still has its task."""
    pin = client.post(f"/api/sessions/{SID}/pins", json={"text": "same"}).json()
    tasks.set_archived(SID, pin["task_id"], True)
    again = client.post(f"/api/sessions/{SID}/pins", json={"text": "same"}).json()
    assert again["task_id"] == pin["task_id"]
    assert tasks.list_tasks(SID) == []


def test_api_unpinning_leaves_the_task(client):
    """By the time you unpin, the task may be edited or already asked — the two
    are linked, not owned."""
    pin = client.post(f"/api/sessions/{SID}/pins", json={"text": "one"}).json()
    client.delete(f"/api/sessions/{SID}/pins/{pin['id']}")
    assert [t["id"] for t in tasks.list_tasks(SID)] == [pin["task_id"]]


def test_api_empty_text_rejected(client):
    assert client.post(f"/api/sessions/{SID}/pins", json={"text": "   "}).status_code == 400


def test_api_delete_missing_is_404(client):
    assert client.delete(f"/api/sessions/{SID}/pins/nope").status_code == 404


def test_api_delete_all_route_beats_the_id_route(client):
    """/pins/all must not be read as a pin whose id is "all"."""
    client.post(f"/api/sessions/{SID}/pins", json={"text": "one"})
    client.post(f"/api/sessions/{SID}/pins", json={"text": "two"})
    r = client.delete(f"/api/sessions/{SID}/pins/all")
    assert r.status_code == 200 and r.json()["deleted"] == 2
    assert client.get(f"/api/sessions/{SID}/pins").json()["pins"] == []
