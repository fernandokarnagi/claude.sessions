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

import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from typing import Optional

CLAUDE_BIN = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
READY_MARKER = "? for shortcuts"   # REPL idle-input footer (see runclaude_base.sh)

# The file-based message bus used for structured session-to-session relay.
# Overridable so the path isn't hard-wired to one machine.
SEND_MESSAGE_SH = os.environ.get(
    "SEND_MESSAGE_SH", os.path.expanduser("~/App/ccoe/send-message.sh"))

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


def has_session(session_id: str) -> bool:
    return capture_pane(session_id) is not None


def spawn(session_id: str, cwd: Optional[str], ready_timeout: int = 60) -> dict:
    """Start a live tmux session that resumes this Claude session.

    Mirrors ccoe/runclaude_base.sh: a detached tmux session named == the Claude
    id, rooted at the project cwd, running `claude --resume <id>`. The session's
    own model/settings are restored by --resume. Inherits the dashboard's env
    (so e.g. ANTHROPIC_BASE_URL for non-default backends carries through).

    Returns {ok, has_tmux} (ok False with `error` on failure).
    """
    if has_session(session_id):
        return {"ok": True, "has_tmux": True, "already": True}
    if not cwd or not os.path.isdir(cwd):
        return {"ok": False, "error": f"project dir not found: {cwd}"}
    try:
        r = subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_id, "-c", cwd],
            capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return {"ok": False, "error": r.stderr.strip() or "tmux new-session failed"}
        time.sleep(1.0)   # let the shell prompt settle before send-keys
        _send_keys(session_id, "-l", "--", f"{CLAUDE_BIN} --resume {session_id}")
        _send_keys(session_id, "Enter")
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": str(e)}

    # Wait for the REPL to come up (idle-input footer marker).
    waited = 0.0
    while waited < ready_timeout:
        screen = capture_pane(session_id) or ""
        if READY_MARKER in screen or parse_prompt(screen):
            return {"ok": True, "has_tmux": True}
        time.sleep(1.0)
        waited += 1.0
    # Session exists but didn't show the marker in time — still usable.
    return {"ok": True, "has_tmux": True, "ready": False}


def dispatch(cwd: str, prompt: str, model: str = "opus",
             ready_timeout: int = 90) -> dict:
    """Start a *brand-new* Claude session for a task and seed it with `prompt`.

    Mirrors ccoe/runclaude_base.sh's new-session path: generate a uuid, create a
    detached tmux session named == that id, run `claude --model M --session-id
    <id>` (so tmux name == Claude session id, keeping the rest of this module's
    machinery valid), wait for the REPL, then type the task prompt and submit.

    Returns {ok, session_id, has_tmux} (ok False with `error` on failure).
    """
    if not cwd or not os.path.isdir(cwd):
        return {"ok": False, "error": f"project dir not found: {cwd}"}
    if not prompt or not prompt.strip():
        return {"ok": False, "error": "empty task prompt"}
    sid = str(uuid.uuid4())
    try:
        r = subprocess.run(
            ["tmux", "new-session", "-d", "-s", sid, "-c", cwd],
            capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return {"ok": False, "error": r.stderr.strip() or "tmux new-session failed"}
        time.sleep(1.0)   # let the shell prompt settle before send-keys
        cmd = f"{shlex.quote(CLAUDE_BIN)} --model {shlex.quote(model)} --session-id {sid}"
        _send_keys(sid, "-l", "--", cmd)
        _send_keys(sid, "Enter")
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": str(e)}

    waited = 0.0
    while waited < ready_timeout:
        screen = capture_pane(sid) or ""
        if READY_MARKER in screen or parse_prompt(screen):
            break
        time.sleep(1.0)
        waited += 1.0

    say(sid, prompt)
    return {"ok": True, "session_id": sid, "has_tmux": True}


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


def kill(session_id: str) -> dict:
    """Terminate the live tmux session (ends its Claude REPL). Irreversible.

    No-op success if nothing is live. Returns {ok} (ok False with `error`).
    """
    if capture_pane(session_id) is None:
        return {"ok": True, "already": True}
    try:
        r = subprocess.run(
            ["tmux", "kill-session", "-t", session_id],
            capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": str(e)}
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr.strip() or "kill-session failed"}
    return {"ok": True}


def relay(from_id: str, to_id: str, message: str) -> dict:
    """Relay `message` from one live session to another via the file message bus.

    Runs ccoe/send-message.sh with TMUX_SESSIONID=from_id, which persists the
    payload under <to>/<from>/<msg_id>/ and nudges the target's REPL with a
    `### TMUX_SESSION_QUESTION - <from>/<msg_id> ###` line. The target can then
    use its tmux-reply skill to read the payload and reply back into from_id's
    pane. Unlike say() (a raw one-way prompt), this is the structured,
    reply-routable path; both sessions should be live tmux sessions.

    Returns {ok, message_id, from, to} (ok False with `error` on failure).
    """
    if not message or not message.strip():
        return {"ok": False, "error": "empty message"}
    if from_id == to_id:
        return {"ok": False, "error": "cannot relay a session to itself"}
    if not os.path.isfile(SEND_MESSAGE_SH):
        return {"ok": False, "error": f"message bus script not found: {SEND_MESSAGE_SH}"}
    if capture_pane(to_id) is None:
        return {"ok": False, "error": "target has no live tmux session"}
    env = dict(os.environ, TMUX_SESSIONID=from_id)
    try:
        r = subprocess.run(
            [SEND_MESSAGE_SH, to_id, message],
            capture_output=True, text=True, timeout=15, env=env)
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": str(e)}
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr.strip() or "send-message failed"}
    return {"ok": True, "message_id": r.stdout.strip(), "from": from_id, "to": to_id}
