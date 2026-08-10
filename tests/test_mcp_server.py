"""Tests for the read-only MCP fleet server.

Everything below the HTTP call is pure dict-shaping, so these run against a
fake opener — no live dashboard, no tmux, no mcp package needed.
"""

import io
import json
import os
import urllib.error

import pytest

from server import mcp_server as m


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    """Stands in for urllib's opener. Maps path -> payload (or an exception)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def open(self, url, timeout=None):
        path = url.split("8765", 1)[-1] if "8765" in url else url
        self.calls.append(path)
        val = self.routes.get(path)
        if val is None:
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)
        if isinstance(val, Exception):
            raise val
        return _Resp(json.dumps(val).encode())


def sess(sid, **kw):
    """A triage row with the fields the shaper reads."""
    base = {
        "session_id": sid,
        "title": f"session {sid}",
        "status": "WAITING",
        "live_tmux": True,
        "model": "claude-opus-5",
        "pending_approval": False,
        "autonomy": "manual",
        "cwd": "/repo",
        "project": "repo",
        "projects": [],
        "task_count": 0,
        # Heavy fields that must never survive shaping.
        "last_activities": [{"kind": "assistant", "text": "x" * 5000}],
        "prompt": {"question": "Do the thing?", "options": ["1. Yes"]},
        "tokens": {"total": 1234},
        "description": "y" * 5000,
    }
    base.update(kw)
    return base


def triage(*sessions, paused=False):
    return {"sessions": list(sessions), "total": len(sessions),
            "autonomy_paused": paused}


# ---------------------------------------------------------------------------
# fleet_status
# ---------------------------------------------------------------------------

def test_fleet_status_shapes_and_stamps():
    op = FakeOpener({"/api/triage": triage(sess("a"), sess("b"))})
    out = m.fleet_status(_open=op)

    assert out["total"] == 2
    assert [s["id"] for s in out["sessions"]] == ["a", "b"]
    assert "as_of" in out
    assert out["autonomy_paused"] is False


def test_fleet_status_drops_transcript_shaped_fields():
    """The whole point of the cap: no activities, no gate text, no description
    from another agent leaking into this agent's context."""
    op = FakeOpener({"/api/triage": triage(sess("a"))})
    row = m.fleet_status(_open=op)["sessions"][0]

    for banned in ("last_activities", "prompt", "description", "tokens"):
        assert banned not in row
    assert set(row) == {
        "id", "title", "status", "live", "model", "gated", "autonomy",
        "cwd", "project", "projects", "open_tasks",
    }


def test_fleet_status_live_only_filters_and_can_be_disabled():
    op = FakeOpener({"/api/triage": triage(sess("a"), sess("b", live_tmux=False))})

    assert m.fleet_status(_open=op)["total"] == 1
    assert m.fleet_status(live_only=False, _open=op)["total"] == 2


def test_fleet_status_truncates_and_reports_the_overflow():
    rows = [sess(str(i)) for i in range(5)]
    op = FakeOpener({"/api/triage": triage(*rows)})
    out = m.fleet_status(limit=2, _open=op)

    assert len(out["sessions"]) == 2
    assert out["total"] == 5
    assert out["truncated"] == 3


def test_fleet_status_limit_is_clamped_to_the_hard_cap():
    rows = [sess(str(i)) for i in range(m.MAX_SESSIONS + 10)]
    op = FakeOpener({"/api/triage": triage(*rows)})

    assert len(m.fleet_status(limit=9999, _open=op)["sessions"]) == m.MAX_SESSIONS
    assert len(m.fleet_status(limit=0, _open=op)["sessions"]) == 1


def test_fleet_status_projects_flatten_to_titles():
    op = FakeOpener({"/api/triage": triage(
        sess("a", projects=[{"id": "p1", "title": "Agent OS"}]))})

    assert m.fleet_status(_open=op)["sessions"][0]["projects"] == ["Agent OS"]


# ---------------------------------------------------------------------------
# who_owns
# ---------------------------------------------------------------------------

def test_who_owns_matches_a_file_under_a_session_cwd(tmp_path):
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    op = FakeOpener({"/api/triage": triage(
        sess("a", cwd=repo), sess("b", cwd="/somewhere/else"))})

    out = m.who_owns(f"{repo}/server/app.py", _open=op)
    assert [o["id"] for o in out["owners"]] == ["a"]
    assert out["owners"][0]["relation"] == "inside"


def test_who_owns_reports_same_and_contains(tmp_path):
    root = str(tmp_path / "work")
    sub = os.path.join(root, "sub")
    os.makedirs(sub)
    op = FakeOpener({"/api/triage": triage(
        sess("deep", cwd=sub), sess("exact", cwd=root))})

    out = m.who_owns(root, _open=op)
    rel = {o["id"]: o["relation"] for o in out["owners"]}
    assert rel == {"exact": "same", "deep": "contains"}
    # Closest claim first.
    assert out["owners"][0]["id"] == "exact"


def test_who_owns_relative_path_resolves_against_cwd(tmp_path, monkeypatch):
    """Claude Code launches the server as a child of the session, so the
    process cwd is the caller's cwd — a bare filename must Just Work."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    op = FakeOpener({"/api/triage": triage(sess("a", cwd=str(repo)))})

    assert m.who_owns("app.js", _open=op)["total"] == 1


def test_who_owns_does_not_match_a_sibling_prefix(tmp_path):
    """/repo-backup must not count as being inside /repo."""
    base = str(tmp_path)
    os.makedirs(os.path.join(base, "repo"))
    os.makedirs(os.path.join(base, "repo-backup"))
    op = FakeOpener({"/api/triage": triage(
        sess("a", cwd=os.path.join(base, "repo-backup")))})

    assert m.who_owns(os.path.join(base, "repo", "x.py"), _open=op)["total"] == 0


def test_who_owns_ignores_sessions_with_no_cwd():
    op = FakeOpener({"/api/triage": triage(sess("a", cwd=None))})
    assert m.who_owns("/repo/x.py", _open=op)["total"] == 0


def test_who_owns_requires_a_path():
    assert "error" in m.who_owns("  ")


# ---------------------------------------------------------------------------
# session_info
# ---------------------------------------------------------------------------

def test_session_info_merges_status_and_task_board():
    op = FakeOpener({
        "/api/sessions/a/status": sess("a", description="short note"),
        "/api/sessions/a/tasks": {"tasks": [{"text": "one"}, {"text": "two"}]},
    })
    out = m.session_info("a", _open=op)

    assert out["id"] == "a"
    assert out["open_task_list"] == ["one", "two"]
    assert out["tokens_total"] == 1234
    assert out["description"] == "short note"


def test_session_info_never_returns_transcript_activities():
    op = FakeOpener({
        "/api/sessions/a/status": sess("a"),
        "/api/sessions/a/tasks": {"tasks": []},
    })
    out = m.session_info("a", _open=op)

    assert "last_activities" not in out
    assert "prompt" not in out
    assert len(out["description"]) <= m.MAX_DESC


def test_session_info_caps_the_task_list():
    tasks = [{"text": f"t{i}"} for i in range(m.MAX_TASKS + 5)]
    op = FakeOpener({
        "/api/sessions/a/status": sess("a"),
        "/api/sessions/a/tasks": {"tasks": tasks},
    })
    out = m.session_info("a", _open=op)

    assert len(out["open_task_list"]) == m.MAX_TASKS
    assert out["open_task_list_truncated"] == 5


def test_session_info_survives_a_task_lookup_failure():
    """Session facts are still worth returning if only the task call breaks."""
    op = FakeOpener({
        "/api/sessions/a/status": sess("a"),
        "/api/sessions/a/tasks": TimeoutError("slow"),
    })
    out = m.session_info("a", _open=op)

    assert out["id"] == "a"
    assert "open_task_list" not in out


def test_session_info_unknown_id_is_a_clean_not_found():
    op = FakeOpener({})
    out = m.session_info("nope", _open=op)

    assert out["error"] == "session not found"
    assert out["id"] == "nope"


def test_session_info_requires_an_id():
    assert "error" in m.session_info("")


# ---------------------------------------------------------------------------
# degradation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("call", [
    lambda op: m.fleet_status(_open=op),
    lambda op: m.who_owns("/repo", _open=op),
    lambda op: m.session_info("a", _open=op),
])
def test_dashboard_down_degrades_cleanly(call):
    """An agent must get a sentence it can act on, not a traceback it will try
    to debug for ten minutes."""
    op = FakeOpener({"/api/triage": ConnectionRefusedError("no listener"),
                     "/api/sessions/a/status": ConnectionRefusedError("no listener")})
    out = call(op)

    assert out["error"] == "fleet unavailable"
    assert "serve.sh" in out["hint"]


def test_loopback_calls_bypass_any_proxy(monkeypatch):
    """This machine has a corporate proxy in the environment; urllib honours
    http_proxy by default, which would route 127.0.0.1 out to it and hang.

    Passing ProxyHandler({}) makes build_opener drop the default proxy handler
    entirely — an empty ProxyHandler registers no *_open methods, so it isn't
    even kept in the handler list. "No proxy handler at all" is the win here,
    which is why this asserts against the stdlib default rather than for a
    handler of our own.
    """
    import urllib.request as u
    monkeypatch.setenv("http_proxy", "http://corporate.proxy:8080")

    def proxied(opener):
        return [h.proxies for h in opener.handlers if isinstance(h, u.ProxyHandler)]

    assert proxied(u.build_opener()), "stdlib default should pick the env proxy up"
    assert proxied(m._opener()) == []


# ---------------------------------------------------------------------------
# MCP registration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_registers_exactly_the_three_read_tools():
    pytest.importorskip("mcp")
    tools = await m.build().list_tools()

    assert {t.name for t in tools} == {"fleet_status", "who_owns", "session_info"}
    # Every tool needs a description — it is the only thing telling the agent
    # when to reach for it.
    assert all((t.description or "").strip() for t in tools)
