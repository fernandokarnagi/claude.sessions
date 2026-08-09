"""
Moving a session's dashboard state onto a new id.

/clear gives the same live REPL a new session id (see tmuxio.reset). Everything
the dashboard knows about that session is keyed by the old one, so each store
has to hand its entry over or the reset session comes back nameless, with no
to-dos and out of its project — the exact loss the Reset button exists to stop.

Every test points the store at a tmp file, so the real state under server/ is
never touched.
"""

import importlib

import pytest

from server import attention, autonomy, descriptions, overrides, projects, tasks

OLD = "11111111-1111-1111-1111-111111111111"
NEW = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point a module's _PATH at a scratch file and hand the module back."""
    def _use(mod):
        monkeypatch.setattr(mod, "_PATH", str(tmp_path / f"{mod.__name__}.json"))
        return mod
    return _use


def test_title_moves(store):
    o = store(overrides)
    o.set_title(OLD, "Payments refactor")
    o.rekey(OLD, NEW)
    assert o.get_title(NEW) == "Payments refactor"
    assert o.get_title(OLD) is None      # not copied — the old id is a dead stub


def test_description_moves(store):
    d = store(descriptions)
    d.set_description(OLD, "Spike on the new gateway")
    d.rekey(OLD, NEW)
    assert d.get(NEW) == "Spike on the new gateway"
    assert d.get(OLD) is None


def test_tasks_move(store):
    t = store(tasks)
    t.add_task(OLD, "write the migration")
    t.add_task(OLD, "run it on staging")
    t.rekey(OLD, NEW)
    assert [x["text"] for x in t.list_tasks(NEW)] == [
        "write the migration", "run it on staging"]
    assert t.list_tasks(OLD) == []


def test_only_unfinished_tasks_move(store):
    # An asked task is done being a to-do; carrying it over would re-queue
    # finished work against the fresh conversation and ask it a second time.
    # It stays with the transcript that handled it.
    t = store(tasks)
    t.add_task(OLD, "already sent", asked=True)
    t.add_task(OLD, "still to ask")
    t.rekey(OLD, NEW)
    assert [x["text"] for x in t.list_tasks(NEW)] == ["still to ask"]
    assert [x["text"] for x in t.list_tasks(OLD)] == ["already sent"]


def test_archived_tasks_stay_behind(store):
    t = store(tasks)
    tid = t.add_task(OLD, "dropped this one")["id"]
    t.set_archived(OLD, tid, True)
    t.rekey(OLD, NEW)
    assert t.list_tasks(NEW, archived=True) == []
    assert [x["text"] for x in t.list_tasks(OLD, archived=True)] == ["dropped this one"]


def test_tasks_append_rather_than_clobber(store):
    t = store(tasks)
    t.add_task(NEW, "already here")
    t.add_task(OLD, "carried over")
    t.rekey(OLD, NEW)
    assert [x["text"] for x in t.list_tasks(NEW)] == ["already here", "carried over"]


def test_project_tags_move(store):
    p = store(projects)
    pid = p.create_project("Gateway")["id"]
    p.tag(OLD, pid)
    p.rekey(OLD, NEW)
    assert p.sessions_for(pid) == [NEW]


def test_project_tags_dont_duplicate(store):
    p = store(projects)
    pid = p.create_project("Gateway")["id"]
    p.tag(OLD, pid)
    p.tag(NEW, pid)
    p.rekey(OLD, NEW)
    assert [x["id"] for x in p.projects_for(NEW)] == [pid]


def test_autonomy_level_moves(store):
    a = store(autonomy)
    a.set(OLD, "auto-safe")
    a.rekey(OLD, NEW)
    assert a.get(NEW) == "auto-safe"
    assert a.get(OLD) == a.DEFAULT


def test_todo_pin_moves(store):
    # Reset shouldn't drop a pinned session out of the To-do inbox, and the
    # dead id shouldn't stay in it — the whole point of the inbox is that
    # everything in it is something you can still act on.
    a = store(attention)
    a.set_marked(OLD, True)
    a.rekey(OLD, NEW)
    assert a.is_marked(NEW)
    assert not a.is_marked(OLD)


def test_unpinned_session_stays_unpinned(store):
    a = store(attention)
    a.rekey(OLD, NEW)
    assert not a.is_marked(NEW)


def test_nothing_to_move_is_not_an_error(store):
    for mod in (overrides, descriptions, tasks, projects, autonomy, attention):
        store(mod).rekey(OLD, NEW)   # no entry for OLD anywhere


def test_same_id_is_a_no_op(store):
    t = store(tasks)
    t.add_task(OLD, "keep me")
    t.rekey(OLD, OLD)
    assert [x["text"] for x in t.list_tasks(OLD)] == ["keep me"]


# --- the endpoint: what happens to the id we just left behind ----------------

@pytest.fixture
def reset_env(tmp_path, monkeypatch):
    """api_reset with tmux stubbed out and every store on scratch files."""
    from server import app as appmod
    from server import archives

    for mod in (overrides, descriptions, tasks, projects, autonomy, attention,
                archives):
        monkeypatch.setattr(mod, "_PATH", str(tmp_path / f"{mod.__name__}.json"))
    monkeypatch.setattr(appmod.grokparser, "has_session", lambda sid: False)
    monkeypatch.setattr(appmod.agyparser, "has_conversation", lambda sid: False)
    monkeypatch.setattr(appmod.parser, "session_path",
                        lambda sid: str(tmp_path / (sid + ".jsonl")))
    killed = []
    monkeypatch.setattr(appmod.tmuxio, "kill",
                        lambda sid: killed.append(sid) or {"ok": True})

    def _use(result, grok=False):
        which = "grok_reset" if grok else "reset"
        if grok:
            monkeypatch.setattr(appmod.grokparser, "has_session", lambda sid: True)
            monkeypatch.setattr(appmod.grokparser, "session_dir",
                                lambda sid: str(tmp_path / "proj" / sid))
        monkeypatch.setattr(appmod.tmuxio, which, lambda sid, d: result)
        return appmod, archives, killed
    return _use


def test_reset_retires_the_old_session(reset_env):
    appmod, archives, killed = reset_env(
        {"ok": True, "session_id": NEW, "old_id": OLD})
    attention.set_marked(OLD, True)

    out = appmod.api_reset(OLD)

    assert out["session_id"] == NEW
    assert killed == [OLD]                    # anything still on that name is stale
    assert archives.is_archived(OLD)
    assert not archives.is_archived(NEW)
    assert attention.is_marked(NEW) and not attention.is_marked(OLD)


def test_grok_resets_through_new_and_retires_the_same_way(reset_env):
    # grok's /new is Claude's /clear: fresh conversation, new session id, same
    # pane. Everything downstream of "we got a new id" is provider-agnostic.
    appmod, archives, killed = reset_env(
        {"ok": True, "session_id": NEW, "old_id": OLD}, grok=True)
    tasks.add_task(OLD, "still to ask")

    out = appmod.api_reset(OLD)

    assert out["session_id"] == NEW
    assert [x["text"] for x in tasks.list_tasks(NEW)] == ["still to ask"]
    assert killed == [OLD]
    assert archives.is_archived(OLD)


def test_agy_has_no_reset(reset_env):
    appmod, _archives, _killed = reset_env({"ok": True})
    from unittest.mock import patch
    with patch.object(appmod.agyparser, "has_conversation", lambda sid: True):
        with pytest.raises(Exception):
            appmod.api_reset(OLD)


def test_a_failed_rename_leaves_the_old_session_alone(reset_env):
    # The rename is what moves the live REPL onto the new name. Without it the
    # REPL still answers to the old id — kill or archive it and you've just
    # taken a working session away from the user.
    appmod, archives, killed = reset_env(
        {"ok": False, "session_id": NEW, "old_id": OLD, "error": "rename failed"})

    with pytest.raises(Exception):
        appmod.api_reset(OLD)

    assert killed == []
    assert not archives.is_archived(OLD)
