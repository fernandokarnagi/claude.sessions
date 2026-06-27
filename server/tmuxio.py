"""
tmuxio.py — read live Claude Code REPL screens out of tmux and answer their
permission prompts.

Each Claude session runs in a detached tmux session whose *name == the Claude
session id* (see ccoe/runclaude_base.sh). So a session id is also a tmux
target. We can:

  * capture_pane(id)   -> the current terminal screen as plain text
  * parse_prompt(text) -> the pending Yes/No/... approval prompt, if any
  * pending(id)        -> capture + parse in one call
  * answer(id, n, txt) -> select option `n` (and type `txt` for a "tell Claude
                          what to do differently" style option) in the live pane

These talk to the *live* REPL (unlike runner.py, which spawns a separate
headless `claude --print --resume`). Answering a permission gate has to happen
in the live pane, so this module is the path for that.
"""

from __future__ import annotations

import re
import subprocess
import time
from typing import Optional

# A numbered menu row, optionally pointed at by the ❯ selector and wrapped in
# box-drawing borders, e.g. "│ ❯ 1. Yes                     │".
_OPTION_RE = re.compile(r"^[\s│|>]*?(❯)?\s*(\d+)\.\s+(.*)$")

# Phrases Claude uses to open a permission prompt. Used to disambiguate a real
# gate from numbered text that happens to appear in output.
_QUESTION_HINTS = (
    "do you want",
    "would you like",
    "do you trust",
    "proceed",
)

_BORDER_CHARS = "╭╮╰╯─│|"


def _strip(s: str) -> str:
    return s.strip().strip(_BORDER_CHARS).strip()


def tmux_sessions() -> set[str]:
    """Names of all live tmux sessions (== Claude session ids for ours)."""
    try:
        out = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return set()
    if out.returncode != 0:
        return set()
    return {ln.strip() for ln in out.stdout.splitlines() if ln.strip()}


def capture_pane(session_id: str) -> Optional[str]:
    """Current screen of the session's tmux pane, or None if no such session."""
    try:
        out = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", session_id],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def parse_prompt(screen: str) -> Optional[dict]:
    """Extract a pending permission prompt from a captured screen.

    Returns {question, options:[{num, label, selected}], raw} or None if the
    session is not currently sitting at a Yes/No/... gate.
    """
    if not screen:
        return None
    lines = screen.splitlines()

    options: list[dict] = []
    first_opt_idx = None
    for i, ln in enumerate(lines):
        m = _OPTION_RE.match(ln)
        if not m:
            # Allow blank/gap lines between options without breaking the run.
            if options and not _strip(ln):
                continue
            if options:
                # A non-blank, non-option line ends the option block.
                break
            continue
        ptr, num, label = m.group(1), int(m.group(2)), _strip(m.group(3))
        if not label:
            continue
        if first_opt_idx is None:
            first_opt_idx = i
        options.append({"num": num, "label": label, "selected": bool(ptr)})

    # Need a real menu: at least two options, numbered 1..N in order.
    if len(options) < 2:
        return None
    if [o["num"] for o in options] != list(range(1, len(options) + 1)):
        return None

    # Question = nearest non-empty, non-option line above the block.
    question = ""
    q_idx = first_opt_idx
    for j in range(first_opt_idx - 1, -1, -1):
        cand = _strip(lines[j])
        if cand:
            question, q_idx = cand, j
            break

    # Context = the tool/command preview rendered above the question, e.g. the
    # "Bash command" block + "This command requires approval". Walk up from the
    # question, collecting until a box top-border or a previous REPL message.
    context = _extract_context(lines, q_idx)

    blob = (question + " " + " ".join(o["label"] for o in options)).lower()
    has_pointer = any(o["selected"] for o in options)
    looks_like_gate = (
        any(h in blob for h in _QUESTION_HINTS)
        or ("yes" in blob and "no" in blob)
    )
    if not (has_pointer or looks_like_gate):
        return None

    return {
        "question": question,
        "context": context,
        "options": options,
        "raw": screen,
    }


# Glyphs that mark the start of a *previous* REPL message (not part of the box).
_STOP_GLYPHS = ("⏺", "✻", "✽", "●", "❯", "⎿", ">")


def _unbox(line: str) -> str:
    """Strip a leading/trailing box border but keep inner indentation."""
    s = line.rstrip()
    s = re.sub(r"^\s*[│|]\s?", "", s)
    s = re.sub(r"\s*[│|]\s*$", "", s)
    return s


def _extract_context(lines: list[str], q_idx: int, max_lines: int = 40) -> str:
    """The command/tool preview block sitting above the question line."""
    collected: list[str] = []
    for j in range(q_idx - 1, -1, -1):
        raw = lines[j]
        if "╭" in raw or "─" * 6 in raw:        # box top / horizontal rule
            break
        if any(_strip(raw).startswith(g) for g in _STOP_GLYPHS):
            break
        collected.append(_unbox(raw))
        if len(collected) >= max_lines:
            break
    collected.reverse()
    return "\n".join(collected).strip("\n")


def pending(session_id: str) -> Optional[dict]:
    """The pending approval prompt for a live session, or None."""
    screen = capture_pane(session_id)
    if screen is None:
        return None
    return parse_prompt(screen)


# Short-TTL cache so the sessions list (polled ~every 1.5s) doesn't shell out to
# tmux once per session on every request.
_CACHE: dict[str, object] = {"at": 0.0, "ids": set()}


def pending_ids(ttl: float = 1.0) -> set[str]:
    """Set of live session ids currently sitting at a permission gate.

    Captures every live tmux pane and parses it; result cached for `ttl`s.
    """
    now = time.monotonic()
    if now - float(_CACHE["at"]) < ttl:
        return set(_CACHE["ids"])  # type: ignore[arg-type]
    gated = {sid for sid in tmux_sessions() if pending(sid) is not None}
    _CACHE["at"] = now
    _CACHE["ids"] = gated
    return set(gated)


def _send_keys(session_id: str, *keys: str) -> None:
    subprocess.run(
        ["tmux", "send-keys", "-t", session_id, *keys],
        capture_output=True, text=True, timeout=5,
    )


def say(session_id: str, text: str) -> dict:
    """Type `text` into the live REPL prompt and submit it.

    Drives the *live* tmux session (one continuous conversation, visible in
    tmux) — unlike runner.run_turn which forks a separate headless resume.
    """
    if not text.strip():
        return {"ok": False, "error": "empty message"}
    if capture_pane(session_id) is None:
        return {"ok": False, "error": "no live tmux session"}
    # -l = literal, so a word like "Enter" inside the text isn't taken as a key.
    _send_keys(session_id, "-l", "--", text)
    _send_keys(session_id, "Enter")
    return {"ok": True}


def answer(session_id: str, choice: int, text: str = "") -> dict:
    """Answer a live permission prompt by selecting option `choice`.

    Sends the digit then Enter into the live pane (matches Claude Code's menu).
    For a "No, and tell Claude what to do differently" style option, pass `text`
    to type the follow-up message after selecting it.
    """
    if capture_pane(session_id) is None:
        return {"ok": False, "error": "no live tmux session"}
    # Select the numbered option and confirm.
    _send_keys(session_id, "--", str(choice))
    _send_keys(session_id, "Enter")
    if text:
        # The option opened a free-text field; type the guidance and submit.
        _send_keys(session_id, "--", text)
        _send_keys(session_id, "Enter")
    return {"ok": True}
