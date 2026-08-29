"""
Shared fixtures.

The agent roster is a folder of markdown files on the machine, so every test
that touches stages points it at a scratch folder of its own. Nothing reads
the operator's real ~/.claude/agents.
"""

import pytest

from server import agents

# What a Claude Code agent file actually looks like: YAML frontmatter naming
# the agent and saying when to use it, then the system prompt as the body.
ROSTER = {
    "researcher.md": (
        "---\nname: Researcher\ndescription: Find prior art\n---\n"
        "You research thoroughly.\n"),
    "builder.md": (
        "---\nname: Builder\ndescription: Write the code\n---\n"
        "You write the code.\n"),
    "reviewer.md": (
        "---\nname: Reviewer\ndescription: Check the work\n---\n"
        "You review.\n"),
}


@pytest.fixture(autouse=True)
def roster(tmp_path, monkeypatch):
    """A three-agent folder, fresh per test. Returns the path so a test can
    add, edit, or delete a file and watch the roster follow."""
    box = tmp_path / "agents"
    box.mkdir()
    for name, text in ROSTER.items():
        (box / name).write_text(text, encoding="utf-8")
    monkeypatch.setattr(agents, "AGENTS_DIR", str(box))
    # The cache is keyed on the folder's mtimes, and a tmp folder made in the
    # same second as the last one can fingerprint identically — so clear it.
    monkeypatch.setattr(agents, "_cache", {"at": 0.0, "sig": None, "agents": []})
    return box
