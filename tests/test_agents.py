"""
The agent roster — read straight out of Claude Code's own agents folder.

Nothing here writes. An agent is added, edited, or removed by editing the
files, which is the point: one definition of any agent on the machine.
"""

from server import agents


def write(box, name, text):
    (box / name).write_text(text, encoding="utf-8")
    agents.list_agents(force=True)          # the folder changed; re-read now


def test_lists_the_folder_sorted_by_name(roster):
    assert [a["id"] for a in agents.list_agents()] == ["Builder", "Researcher", "Reviewer"]


def test_frontmatter_becomes_the_record(roster):
    a = agents.by_id()["Builder"]
    assert a["name"] == "Builder"
    assert a["description"] == "Write the code"
    assert a["prompt"] == "You write the code."
    assert a["declared"] is True
    assert a["file"].endswith("builder.md")


def test_the_frontmatter_name_wins_over_the_filename(roster):
    write(roster, "some-file.md", "---\nname: Archivist\n---\nYou archive.\n")
    assert "Archivist" in agents.by_id()
    assert "some-file" not in agents.by_id()


def test_a_file_without_frontmatter_is_still_listed(roster):
    """Claude Code itself skips it, but the operator plainly meant it as an
    agent by putting it there — dropping it would look like a lost file."""
    write(roster, "notes.md", "# Scratch\n\nYou take notes.\n")
    a = agents.by_id()["notes"]
    assert a["declared"] is False
    assert a["description"] == "Scratch"          # first prose line
    assert a["prompt"].startswith("# Scratch")


def test_malformed_frontmatter_reads_as_a_plain_file(roster):
    write(roster, "broken.md", "---\nname: [unclosed\n---\nYou try.\n")
    assert agents.by_id()["broken"]["prompt"] == "You try."


def test_a_tools_list_is_flattened(roster):
    write(roster, "tooled.md",
          "---\nname: Tooled\ntools:\n  - Read\n  - Grep\n---\nYou read.\n")
    assert agents.by_id()["Tooled"]["tools"] == "Read, Grep"


def test_editing_a_file_shows_up_without_a_restart(roster):
    write(roster, "builder.md", "---\nname: Builder\ndescription: Ship it\n---\nGo.\n")
    assert agents.by_id()["Builder"]["description"] == "Ship it"


def test_deleting_a_file_removes_the_agent(roster):
    (roster / "reviewer.md").unlink()
    agents.list_agents(force=True)
    assert "Reviewer" not in agents.by_id()


def test_an_oversized_file_is_skipped(roster):
    """An agent file is a system prompt, not a document store."""
    write(roster, "huge.md", "---\nname: Huge\n---\n" + "x" * (agents._MAX_BYTES + 1))
    assert "Huge" not in agents.by_id()


def test_two_files_claiming_one_name_keep_the_first(roster):
    write(roster, "aa-builder.md", "---\nname: Builder\ndescription: First\n---\na\n")
    assert agents.by_id()["Builder"]["description"] == "First"
    assert len([a for a in agents.list_agents() if a["id"] == "Builder"]) == 1


def test_a_missing_folder_is_an_empty_roster(monkeypatch, tmp_path):
    monkeypatch.setattr(agents, "AGENTS_DIR", str(tmp_path / "nope"))
    assert agents.list_agents(force=True) == []


def test_get_many_keeps_the_asked_for_order_and_names_the_missing(roster):
    found, missing = agents.get_many(["Reviewer", "ghost", "Builder"])
    assert [a["id"] for a in found] == ["Reviewer", "Builder"]
    assert missing == ["ghost"]


def test_the_api_serves_the_roster_and_says_where_it_read_it(roster):
    """The editor shows the folder path, so an operator who wants a new agent
    knows where to put the file."""
    from fastapi.testclient import TestClient

    from server.app import app

    body = TestClient(app).get("/api/agents").json()
    assert [a["id"] for a in body["agents"]] == ["Builder", "Researcher", "Reviewer"]
    assert body["dir"] == str(roster)
