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

# A multiSelect option renders its state as a checkbox: "1. [ ] Apple" /
# "1. [✔] Apple". Two or more of these mean the gate is a checkbox widget, which
# is driven by toggles + Submit rather than a single numbered pick.
_CHECKBOX_RE = re.compile(r"^\[([ xX✔✓])\]\s*(.*)$")
# The widget's free-text row — it opens an input we can't fill from here, so it
# is not offered as a checkbox.
_TYPE_SOMETHING_RE = re.compile(r"^type something\b", re.I)

# Phrases Claude uses to open a permission prompt. Used to disambiguate a real
# gate from numbered text that happens to appear in output.
_QUESTION_HINTS = (
    "do you want",
    "would you like",
    "do you trust",
    "proceed",
)

_BORDER_CHARS = "╭╮╰╯─│|"

# A horizontal rule line — the REPL frames its input box between two of these.
_RULE_RE = re.compile(r"─{10,}")

# The active spinner status LINE, e.g. "✻ Actualizing… (1m 44s · ↓ 5.1k tokens)"
# or "· Leavening… (1m 13s · esc to interrupt)". Matched by structure, anchored
# at line start: a spinner glyph, a gerund, then "… (<elapsed>…". A *completed*
# marker reads "✻ Baked for 2m 17s" (no "… ("), and this deliberately does NOT
# match ordinary prose containing "… (" mid-line (which isn't glyph-anchored).
_SPINNER_RE = re.compile(r"^[ \t]*[✻✽✶✳✷✵⚹✢·∴][^\n(]*…[^\n]*\(", re.MULTILINE)

# --- error / retry banners -------------------------------------------------
# When the REPL can't reach the API it replaces the spinner with a banner like
#   "✻ Unable to connect to API (ConnectionRefused) · Retrying in 0s · attempt 5/10"
# It carries a spinner glyph but no "… (", so _SPINNER_RE never matched it —
# which is why the dashboard showed nothing at all while a session sat stuck
# retrying. This detects it separately.
#
# The structural tell of a retry banner. "attempt 5/10" is specific enough to
# stand alone; "Retrying in 5s" is not (it reads fine in prose), so that half
# only counts on a glyph-anchored line — see error_line.
_RETRY_RE = re.compile(r"retrying in \d+\s*s|attempt \d+\s*/\s*\d+", re.I)
_ATTEMPT_RE = re.compile(r"attempt \d+\s*/\s*\d+", re.I)

# Failure phrases. On their own these are too weak (an assistant reply can
# discuss "rate limit"), so they only count on a glyph-anchored status line.
_ERROR_PHRASES = (
    "unable to connect to api",
    "connection error",
    "api error",
    "request timed out",
    "stream error",
    "overloaded",
    "internal server error",
    "service unavailable",
    "oauth token expired",
    "invalid api key",
    "credit balance is too low",
    "usage limit reached",
)

# Glyphs the REPL puts at the head of a status/error line.
_ERR_GLYPHS = "✻✽✶✳✷✵⚹✢·∴⧉✗✘⚠!"

# Longest banner we keep — enough for the message plus its retry tail.
_ERR_MAX = 240


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


def pane_cwd(session_id: str) -> Optional[str]:
    """Current working directory of the session's (first) pane, or None."""
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "-t", session_id, "#{pane_current_path}"],
            capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    cwd = out.stdout.strip()
    return cwd or None


def rename_session(old: str, new: str) -> bool:
    """Rename a tmux session. True on success."""
    try:
        r = subprocess.run(["tmux", "rename-session", "-t", old, new],
                           capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def capture_pane(session_id: str, history: int = 0) -> Optional[str]:
    """Current screen of the session's tmux pane, or None if no such session.

    history > 0 also captures that many lines of scrollback above the visible
    screen (`-S -<n>`), so callers can show the full conversation, not just the
    last frame. history=0 (default) is the visible screen only — what the
    gate/spinner/status detectors want.
    """
    cmd = ["tmux", "capture-pane", "-p", "-t", session_id]
    if history:
        cmd[2:2] = ["-S", f"-{int(history)}"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
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

    # Collect EVERY run of numbered option lines (a "run" is options with the
    # same 1..N sequence, separated from other content by a blank line). A pane
    # often contains a decoy run — e.g. the assistant's own prose enumeration
    # ("1. …\n2. …") above the real prompt — so we can't just take the first;
    # we scan them all and pick the true gate near the bottom.
    runs: list[dict] = []
    cur: Optional[dict] = None
    for i, ln in enumerate(lines):
        m = _OPTION_RE.match(ln)
        if m:
            ptr, num, label = m.group(1), int(m.group(2)), _strip(m.group(3))
            if cur is None:
                cur = {"start": i, "options": []}
            # `first` keeps the option's own line before any wrapped
            # continuation is folded into `label` (a checkbox lives there).
            cur["options"].append({"num": num, "label": label,
                                   "first": label, "selected": bool(ptr)})
            continue
        if cur is not None:
            if not _strip(ln):
                runs.append(cur)          # blank line closes the run
                cur = None
            elif cur["options"]:
                # A wrapped option label spills onto an indented continuation
                # line; fold it back into the current option rather than break.
                cur["options"][-1]["label"] += " " + _strip(ln)
    if cur is not None:
        runs.append(cur)

    # A real menu has >= 2 options numbered exactly 1..N in order.
    valid = [r for r in runs
             if len(r["options"]) >= 2
             and [o["num"] for o in r["options"]] == list(range(1, len(r["options"]) + 1))]
    if not valid:
        return None

    # Pick the bottom-most run that is actually an interactive menu. A live
    # permission menu always renders the ❯ selector on one of its options; an
    # assistant's *prose* enumeration ("Options: 1. … 2. …") never does. We also
    # accept a gate-keyword question ("do you want", "proceed", …) as a backstop.
    # (Deliberately NOT a loose yes/no substring test — that mis-fires on prose:
    # "Yes — misleading" + "Snowflake" would read as a Yes/No menu.)
    chosen = None
    for r in valid:
        opts = r["options"]
        question, q_idx = "", r["start"]
        for j in range(r["start"] - 1, -1, -1):
            cand = _strip(lines[j])
            if cand:
                question, q_idx = cand, j
                break
        has_pointer = any(o["selected"] for o in opts)
        has_hint = any(h in question.lower() for h in _QUESTION_HINTS)
        if has_pointer or has_hint:
            chosen = (r, question, q_idx)
    if chosen is None:
        return None

    r, question, q_idx = chosen
    # Context = the tool/command preview rendered above the question, e.g. the
    # "Bash command" block + "This command requires approval". Walk up from the
    # question, collecting until a box top-border or a previous REPL message.
    context = _extract_context(lines, q_idx)
    out = {
        "question": question,
        "context": context,
        "options": r["options"],
        "raw": screen,
    }

    # A checkbox (multiSelect) widget: tick any number, then Submit. Split the
    # checkbox state off each label and keep the wrapped remainder as its
    # description, so the UI can render real checkboxes instead of pick-one
    # buttons (a digit here toggles — it does not answer).
    boxed = [o for o in r["options"] if _CHECKBOX_RE.match(o.get("first", ""))]
    if len(boxed) >= 2:
        opts = []
        for o in boxed:
            m = _CHECKBOX_RE.match(o["first"])
            label = _strip(m.group(2))
            if _TYPE_SOMETHING_RE.match(label):
                continue          # free-text row — can't be driven from here
            desc = o["label"][len(o["first"]):].strip()
            opts.append({"num": o["num"], "label": label, "desc": desc,
                         "checked": m.group(1) != " ", "selected": o["selected"]})
        if opts:
            out["multi"] = True
            out["options"] = opts
    return out


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


def spinner_line(screen: Optional[str]) -> Optional[str]:
    """The current active spinner status line (e.g. "✽ Extracting all document
    text… (4m 11s)"), or None if the REPL isn't generating. Tells you what the
    session is working on."""
    if not screen:
        return None
    m = _SPINNER_RE.search(screen)
    if not m:
        return None
    # Return the whole matched line, tidied.
    line = screen[m.start():].splitlines()[0]
    return line.strip() or None


def error_line(screen: Optional[str]) -> Optional[str]:
    """The REPL's current error / retry banner, or None if it looks healthy.

    Read off the *visible* pane, never scrollback — so once the session
    reconnects and the banner scrolls away, the next capture returns None and
    the alert clears itself. That's the whole self-healing story: there is no
    error state stored anywhere, only what's on screen right now.

    A line qualifies if it carries "attempt 5/10", or if it's a glyph-anchored
    status line with either the retry structure or a known failure phrase.
    Prompt lines are skipped outright so text you typed ("why is it retrying in
    5s?") can never raise a false alarm. Scanned bottom-up: newest banner sits
    lowest.
    """
    if not screen:
        return None
    for raw in reversed(screen.splitlines()):
        line = _strip(raw)
        if not line or line[0] in "❯>":
            continue                     # user input, not REPL status
        if _ATTEMPT_RE.search(line):
            return line[:_ERR_MAX]
        if line[0] in _ERR_GLYPHS:
            low = line.lower()
            if _RETRY_RE.search(low) or any(p in low for p in _ERROR_PHRASES):
                return line[:_ERR_MAX]
    return None


def _at_input_box(screen: str) -> bool:
    """True when the REPL is sitting at its empty/ready input box.

    The live input prompt renders as a `❯` line framed by two horizontal rules:
        ───────────
        ❯  (maybe half-typed text)
        ───────────
    That box is present when the agent is idle / waiting for input, and is
    replaced by a spinner while it's actively generating (and by a menu when a
    permission gate is up). We require the rule frame so a `❯ …` line from
    scrollback (a past user turn) doesn't count.
    """
    lines = screen.splitlines()
    for i, ln in enumerate(lines):
        s = _strip(ln)
        if not s.startswith("❯"):
            continue
        if re.match(r"\d+\.", s[1:].strip()):   # "❯ 1. Yes" is a menu option
            continue
        above = lines[i - 1] if i > 0 else ""
        below = lines[i + 1] if i + 1 < len(lines) else ""
        if _RULE_RE.search(above) and _RULE_RE.search(below):
            return True
    return False


# Short-TTL cache of what one sweep of the live panes found.
_WORK_CACHE: dict[str, object] = {"at": 0.0, "ids": set(), "errors": {}}


def _scan_live_panes(ttl: float = 1.0) -> tuple[set[str], dict[str, str]]:
    """One sweep of every live pane → (working ids, {sid: error banner}).

    Both answers come out of the same capture so the sessions list — polled
    every ~1.5s — shells out to tmux once per session, not twice. Cached `ttl`s.
    """
    now = time.monotonic()
    if now - float(_WORK_CACHE["at"]) < ttl:
        return set(_WORK_CACHE["ids"]), dict(_WORK_CACHE["errors"])  # type: ignore[arg-type]
    working: set[str] = set()
    errors: dict[str, str] = {}
    for sid in tmux_sessions():
        screen = capture_pane(sid)
        if screen is None:
            continue
        err = error_line(screen)
        if err:
            errors[sid] = err
        if parse_prompt(screen) is not None:
            continue                     # a permission gate is up → not "working"
        # The empty input box renders in BOTH idle and generating states, so it
        # isn't a reliable idle signal. The glyph-anchored active spinner line is
        # — and a completed turn overwrites it with a "… for Xs" marker, so a
        # stale one won't linger in scrollback.
        if _SPINNER_RE.search(screen):
            working.add(sid)
    _WORK_CACHE["at"] = now
    _WORK_CACHE["ids"] = working
    _WORK_CACHE["errors"] = errors
    return set(working), dict(errors)


def working_ids(ttl: float = 1.0) -> set[str]:
    """Live session ids whose REPL is actively generating (THINKING).

    A live session is "working" when its pane is neither at the ready input box
    (idle) nor at a permission gate — i.e. a spinner is running. This is the
    ground truth for THINKING, more reliable than the transcript (which can end
    on a queued tool_result or an injected "no visible output" nudge while the
    REPL has already gone idle).
    """
    return _scan_live_panes(ttl)[0]


def error_lines(ttl: float = 1.0) -> dict[str, str]:
    """{live session id: its current error / retry banner} for every live pane.

    Only sessions currently showing a banner appear. Nothing is remembered
    between sweeps, so a session that reconnects drops out on its own (see
    error_line).
    """
    return _scan_live_panes(ttl)[1]


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


def _paste(session_id: str, text: str) -> None:
    """Deliver `text` to the pane as ONE bracketed paste.

    `send-keys -l` writes the raw bytes with no paste wrapper, and tmux drains
    them to the pty in 1022-byte writes. Every newline that arrives outside a
    paste bracket is an Enter, so a multi-line message submits itself in pieces
    and only the tail survives as the "real" turn. Going through a buffer and
    pasting with -p wraps the payload in ESC[200~ … ESC[201~, so the REPL takes
    it as one paste and the newlines stay newlines wherever the chunk
    boundaries happen to fall.
    """
    buf = f"agentos-{uuid.uuid4().hex[:8]}"
    subprocess.run(["tmux", "load-buffer", "-b", buf, "-"],
                   input=text.encode(), capture_output=True, timeout=5)
    subprocess.run(["tmux", "paste-buffer", "-d", "-p", "-b", buf, "-t", session_id],
                   capture_output=True, text=True, timeout=5)


def _composer_has_text(session_id: str) -> bool:
    """True if the REPL composer still holds anything at all.

    The pasted-message counterpart to _input_pending: after a bracketed paste
    the REPL shows a "[Pasted text +N lines]" placeholder instead of the text,
    so there is no snippet to look for. What still holds is that a submitted
    composer is empty.
    """
    screen = capture_pane(session_id, history=0)
    if not screen:
        return False
    for line in screen.splitlines()[-8:]:
        stripped = _strip(line)
        if stripped.startswith(("❯", ">")) and stripped[1:].strip():
            return True
    return False


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
        # cd explicitly: `-c cwd` only sets the shell's *initial* dir; a login
        # profile can cd away before claude launches, which would resume the
        # session in the wrong project (wrong .claude settings/model default).
        _send_keys(session_id, "-l", "--",
                   f"cd {shlex.quote(cwd)} && {shlex.quote(CLAUDE_BIN)} --resume {session_id}")
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
        # cd explicitly so a login profile can't drop us in the wrong project.
        cmd = (f"cd {shlex.quote(cwd)} && {shlex.quote(CLAUDE_BIN)} "
               f"--model {shlex.quote(model)} --session-id {sid}")
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


def _input_pending(session_id: str, snippet: str) -> bool:
    """True if `snippet` still sits on the REPL composer line (not yet submitted).

    Claude's composer is a bare "❯ <text>" between two rule lines; grok/agy wrap
    theirs in a box ("│ ❯ <text> │"). _strip drops the border glyphs, so one
    check covers all three. On submit the composer clears and the text moves up
    into the transcript, so finding it still there means Enter didn't take.
    """
    screen = capture_pane(session_id, history=0)
    if not screen:
        return False
    key = snippet.strip()[:24]
    if not key:
        return False
    return any(key in line for line in screen.splitlines()[-8:]
               if _strip(line).startswith(("❯", ">")))


def say(session_id: str, text: str, tries: int = 4) -> dict:
    """Type `text` into the live REPL prompt and *reliably* submit it.

    Drives the *live* tmux session (one continuous conversation, visible in
    tmux) — unlike runner.run_turn which forks a separate headless resume.

    The REPL's editor (Ink) debounces keystrokes: an Enter fired immediately
    after a literal paste lands mid-render and is swallowed as a newline, so the
    message just sits in the composer unsent (the reported "sometimes ⌘↵ doesn't
    submit"). Fix: let the paste settle, send Enter, then verify the turn
    actually started / the composer cleared — resending Enter until it takes.
    This mirrors grok_say, which already had to solve exactly this.
    """
    if not text.strip():
        return {"ok": False, "error": "empty message"}
    if capture_pane(session_id) is None:
        return {"ok": False, "error": "no live tmux session"}
    multiline = "\n" in text.strip()
    if multiline:
        _paste(session_id, text.rstrip("\n"))
        time.sleep(1.0)                   # a big paste takes longer to render
    else:
        # -l = literal, so a word like "Enter" inside the text isn't a keypress.
        _send_keys(session_id, "-l", "--", text)
        time.sleep(0.5)                   # let the editor ingest the paste
    for attempt in range(1, tries + 1):
        _send_keys(session_id, "Enter")
        time.sleep(0.6)
        # Submitted if the turn started OR the composer no longer holds it.
        pending = (_composer_has_text(session_id) if multiline
                   else _input_pending(session_id, text))
        if spinner_line(capture_pane(session_id, history=0)) or not pending:
            return {"ok": True, "attempts": attempt}
        time.sleep(0.4)                   # editor still settling — retry Enter
    return {"ok": True, "attempts": tries, "warning": "submit unconfirmed"}


def agy_set_model(session_id: str, model: str, timeout: float = 6.0) -> dict:
    """Switch a live agy session's model via its "/model" picker (↑/↓ + Enter).

    Opens the picker, moves the cursor to `model` (exact list label), selects it.
    Returns {ok, model} or {ok False, error}. Saved by agy as the session default.
    """
    from . import agyparser
    model = (model or "").strip()
    if not model:
        return {"ok": False, "error": "empty model"}
    if capture_pane(session_id) is None:
        return {"ok": False, "error": "no live tmux session"}

    _send_keys(session_id, "-l", "--", "/model")
    _send_keys(session_id, "Enter")

    picker, waited = None, 0.0
    while waited < timeout:
        time.sleep(0.4)
        waited += 0.4
        picker = agyparser.parse_model_picker(capture_pane(session_id))
        if picker:
            break
    else:
        _send_keys(session_id, "Escape")
        return {"ok": False, "error": "model picker did not open"}

    try:
        target = picker["options"].index(model)
    except ValueError:
        _send_keys(session_id, "Escape")
        return {"ok": False, "error": f"'{model}' not in agy model picker"}

    delta = target - picker["cursor_idx"]
    key = "Down" if delta > 0 else "Up"
    for _ in range(abs(delta)):
        _send_keys(session_id, key)
        time.sleep(0.08)
    _send_keys(session_id, "Enter")
    return {"ok": True, "model": model}


# A rendered progress-bar block glyph — appears ONLY in the /usage panels, never
# in normal chat/prose, so it's a reliable "the panel actually opened" signal.
_BAR_RE = re.compile(r"[█▉▊▋▌▍▎▏░]")
# Footer/close-hint line that ends the panel region.
_USAGE_FOOTER_RE = re.compile(r"Esc to cancel|esc\s+Close|↑/↓ Scroll|d to day · w to week")
# While Claude's /usage is settling it shows its own CACHED numbers and a
# "Refreshing…" line, then updates to the live figures. Capturing before this
# clears returns the stale cached values — so we wait it out.
_USAGE_REFRESH_RE = re.compile(r"Refreshing")


def _capture_usage(session_id: str, header_re, timeout: float) -> dict:
    """Open /usage, wait for the panel to render AND settle (its header is up, a
    progress bar is drawn, and "Refreshing…" has cleared), then capture from its
    header to the footer, and close it (Esc)."""
    if capture_pane(session_id) is None:
        return {"ok": False, "error": "no live tmux session"}
    _send_keys(session_id, "-l", "--", "/usage")
    _send_keys(session_id, "Enter")
    screen, waited, settled = "", 0.0, False
    while waited < timeout:
        time.sleep(0.4)
        waited += 0.4
        # Visible pane only (no scrollback): the /usage panel is an in-place
        # overlay, so the current screen holds exactly one panel — avoids
        # latching onto a stale panel left in scrollback.
        screen = capture_pane(session_id, history=0) or ""
        # The right panel is up once its header + a bar are present.
        if not (header_re.search(screen) and _BAR_RE.search(screen)):
            continue
        # Panel is up: hold until Claude's own refresh finishes so we read the
        # live numbers, not the cached ones it paints first.
        if _USAGE_REFRESH_RE.search(screen):
            continue
        settled = True
        break
    if not settled:
        # Timed out. If a panel is at least up, fall through and capture what we
        # have (may still be refreshing); otherwise report it never opened.
        if not (header_re.search(screen) and _BAR_RE.search(screen)):
            _send_keys(session_id, "Escape")
            return {"ok": False, "error": "/usage didn't open — is the session idle?"}

    lines = screen.splitlines()
    heads = [i for i, l in enumerate(lines) if header_re.search(l)]
    # Visible-only capture holds a single panel, so its top is the FIRST header
    # (e.g. "Total cost"/"Current session") — not the last, which would drop the
    # panel's upper sections when it has several headers.
    start = heads[0] if heads else 0
    # Footer = first close-hint AFTER the header.
    footer = min((i for i, l in enumerate(lines)
                  if i > start and _USAGE_FOOTER_RE.search(l)), default=len(lines))
    if not heads:
        start = max(0, footer - 44)
    out = [l.rstrip().lstrip("│ ").rstrip() for l in lines[start:footer] if l.strip()]
    _send_keys(session_id, "Escape")
    return {"ok": True, "text": "\n".join(out).strip()}


def usage(session_id: str, timeout: float = 10.0) -> dict:
    """Claude Code's /usage cost & limits panel."""
    return _capture_usage(session_id, re.compile(
        r"Total cost|Current session|Current week|Usage by model|Manage subscription"), timeout)


def agy_usage(session_id: str, timeout: float = 6.0) -> dict:
    """agy's /usage Models & Quota panel."""
    return _capture_usage(session_id, re.compile(r"Models & Quota"), timeout)


_GROK_USAGE_HEAD = re.compile(r"Session usage \(since start or last resume\)")
_GROK_USAGE_FOOT = re.compile(r"Next reset:")

# While a turn generates, grok's frame shows a braille spinner + a status line
# ("Waiting for response…" / "Thinking…" / "Worked for Ns"), a "[stop]" hint,
# and an "Esc:cancel" footer. At idle the footer is just "…Ctrl+x:shortcuts".
_GROK_BUSY_RE = re.compile(
    r"Esc:cancel|\[stop\]|Waiting for response|Cancelling|[⠀-⣿]\s*(Thinking|Worked for|Waiting)",
    re.I)


def grok_working(session_id: str) -> bool:
    """True when the live grok REPL is mid-turn (generating), read from its
    pane's visible frame. Used to surface THINKING for grok sessions."""
    screen = capture_pane(session_id, history=0)
    if not screen:
        return False
    tail = "\n".join(screen.splitlines()[-8:])
    return bool(_GROK_BUSY_RE.search(tail))


def _grok_input_pending(session_id: str, snippet: str) -> bool:
    """True if `snippet` still sits on grok's composer line (not yet submitted).

    grok's editor (Ink) keeps typed text after the `❯` prompt until Enter
    submits it; on submit the composer clears and the text moves into history.
    """
    screen = capture_pane(session_id, history=0)
    if not screen:
        return False
    tail = screen.splitlines()[-6:]
    key = snippet.strip()[:24]
    if not key:
        return False
    return any(key in l for l in tail if l.lstrip().startswith(("❯", ">", "│")))


def grok_say(session_id: str, text: str, tries: int = 4) -> dict:
    """Type `text` into a live grok REPL and *reliably* submit it.

    grok's Ink editor debounces keystrokes: an Enter fired immediately after a
    literal paste lands mid-render and is swallowed as a newline (the reported
    "sometimes just makes a line break, doesn't send"). Fix: let the paste
    settle, send Enter, then verify the composer actually cleared / the turn
    started — resending Enter until it takes.
    """
    if not text.strip():
        return {"ok": False, "error": "empty message"}
    if capture_pane(session_id) is None:
        return {"ok": False, "error": "no live tmux session"}
    multiline = "\n" in text.strip()
    if multiline:
        _paste(session_id, text.rstrip("\n"))   # see _paste: newlines would submit
        time.sleep(1.0)
    else:
        _send_keys(session_id, "-l", "--", text)
        time.sleep(0.5)                   # let the editor ingest the paste
    for attempt in range(1, tries + 1):
        _send_keys(session_id, "Enter")
        time.sleep(0.6)
        # Submitted if the turn started OR the composer no longer holds the text.
        pending = (_composer_has_text(session_id) if multiline
                   else _grok_input_pending(session_id, text))
        if grok_working(session_id) or not pending:
            return {"ok": True, "attempts": attempt}
        time.sleep(0.4)                   # editor still settling — retry Enter
    return {"ok": True, "attempts": tries, "warning": "submit unconfirmed"}


def grok_usage(session_id: str, timeout: float = 8.0) -> dict:
    """grok's /usage output — an inline block (not an overlay), so send /usage
    and slice the LAST "Session usage … / … Next reset" block from scrollback."""
    if capture_pane(session_id) is None:
        return {"ok": False, "error": "no live tmux session"}
    _send_keys(session_id, "-l", "--", "/usage")
    _send_keys(session_id, "Enter")
    screen, waited = "", 0.0
    while waited < timeout:
        time.sleep(0.4)
        waited += 0.4
        screen = capture_pane(session_id, history=2000) or ""
        if _GROK_USAGE_HEAD.search(screen) and _GROK_USAGE_FOOT.search(screen):
            break
    lines = screen.splitlines()
    heads = [i for i, l in enumerate(lines) if _GROK_USAGE_HEAD.search(l)]
    if not heads:
        return {"ok": False, "error": "/usage didn't render — is the session idle?"}
    start = heads[-1]                       # last (freshest) block
    foot = next((i for i, l in enumerate(lines)
                 if i > start and _GROK_USAGE_FOOT.search(l)), len(lines) - 1)
    # Strip the TUI's left gutter (box-drawing) and the right scrollbar column
    # ("█") so the captured block is clean text.
    cleaned = []
    for l in lines[start:foot + 1]:
        l = l.rstrip("█ ").lstrip("│┃ ").rstrip()
        if l:
            cleaned.append(l)
    return {"ok": True, "text": "\n".join(cleaned).strip()}


# ---- grok's question card (`ask_user_question`) --------------------------
# A different widget from a permission gate: the agent stops and asks the human
# to *choose*, and the answer is a decision about the work rather than about
# risk. grok draws it as keyed radio rows with the description in a second
# column, and a free-text row keyed `z`:
#
#     When a workflow node fails, what happens?
#
#     1 (○) Fail-fast (Recommended)      Job status failed. Remaining nodes skipped.
#     2 (○) Skip and continue            Failed node records an error; downstream …
#     z (○) Type your answer here
#
#     ↑/↓ navigate · y copy
#
# Rows are keyed, not numbered: 1-9 then a-f, and `z` is always the free-text
# row (grok's own key table). The key is the keystroke that picks the row, so
# it is carried alongside a 1..N `num` the dashboard can count on.
_GROK_ASK_ROW_RE = re.compile(
    r"^([0-9a-z])\s+([(\[])\s*([^)\]\s]?)\s*([)\]])\s+(\S.*)$")
# The card's own footer. grok renders the same pair of hints under every
# question card, and nothing else in its TUI draws keyed radio rows under it.
_GROK_ASK_FOOT_RE = re.compile(r"navigate\b.{0,12}\bcopy\b|Tab/Space:\s*question")
_GROK_ASK_CUSTOM_RE = re.compile(r"^type your (own )?answer( here)?$", re.I)
# A ticked radio or checkbox. grok degrades these to ASCII on terminals that
# cannot draw them (its own glyph fallback table), so both spellings count.
_GROK_FILLED = "●◉◎x✕✓✔*"
# Label and description share a row, separated by the gap that aligns the
# description column.
_GROK_ASK_GAP_RE = re.compile(r"\s{2,}")


# How far above a footer the answer rows may start. The card's own footer sits
# one blank line under the last row; the shortcuts bar sits a couple of lines
# further down again, and either one can be the match we found first.
_GROK_ASK_LEAD = 4


def _grok_ask_rows(lines: list[str], foot: int) -> list[tuple[int, str, str, bool]]:
    """The card's answer rows above `foot`, as (line index, key, rest, filled).

    Read bottom-up and stopped at the first line that is not a row, so a keyed
    line sitting in the scrollback above the card cannot join the run.

    A description too wide for its row wraps onto the next line, indented under
    the description column and carrying no key. Read bottom-up it arrives
    before the row it belongs to, so it is held and folded into the next row
    matched. Without that the run stopped at the wrap: the row above it was
    dropped, its text was read as the question, and a card left with fewer than
    two rows disappeared from the dashboard entirely.
    """
    out: list[tuple[int, str, str, bool]] = []
    wrapped: list[str] = []
    indent = 0
    for i in range(foot - 1, -1, -1):
        text = _unbox(lines[i])
        m = _GROK_ASK_ROW_RE.match(text.strip())
        if m:
            tick = m.group(3)
            indent = len(text) - len(text.lstrip())
            # An empty box is not a ticked one; `in` on "" says otherwise.
            out.append((i, m.group(1), " ".join([m.group(5), *wrapped]),
                        bool(tick) and tick in _GROK_FILLED))
            wrapped = []
            continue
        if out and text.strip() and len(text) - len(text.lstrip()) > indent:
            wrapped.insert(0, text.strip())
            continue
        if out or foot - i > _GROK_ASK_LEAD:
            break
    out.reverse()
    return out


def parse_grok_ask(screen: Optional[str]) -> Optional[dict]:
    """A pending grok question card from a captured screen, or None.

    Same shape as the other gates — question, numbered options, `stage` and
    `custom` — so the dashboard's answer UI and autonomy both read it without
    knowing which CLI drew it. `custom` marks the free-text row, which is what
    makes autonomy treat the whole card as a choice it must not answer.

    Each option also carries `press`: the literal keystroke grok binds to it,
    which is what actually gets sent. `num` stays a plain 1..N so the UI can
    number the rows the way every other gate is numbered. (Not `key` — agy's
    gate already uses that field for its own option ids, and the dashboard
    routes an answer on it.)
    """
    if not screen:
        return None
    lines = screen.splitlines()
    # grok shows two footers under a live card — the card's own "↑/↓ navigate"
    # hints and the shortcuts bar below them. Either can be the lower match, so
    # try them bottom-up and keep the first that actually has rows above it.
    foot, rows = None, []
    for i in range(len(lines) - 1, -1, -1):
        if not _GROK_ASK_FOOT_RE.search(lines[i]):
            continue
        found = _grok_ask_rows(lines, i)
        if len(found) >= 2:
            foot, rows = i, found
            break
    if foot is None:
        return None

    # The question sits above the rows, past one or more blank lines, and wraps
    # across as many lines as it needs.
    head = rows[0][0] - 1
    while head >= 0 and not _unbox(lines[head]).strip():
        head -= 1
    top = head
    while top >= 0 and _unbox(lines[top]).strip():
        top -= 1
    question = " ".join(_unbox(l).strip() for l in lines[top + 1:head + 1]).strip()

    options, custom = [], None
    for n, (_, key, rest, filled) in enumerate(rows, 1):
        parts = _GROK_ASK_GAP_RE.split(rest, 1)
        label = parts[0].strip()
        desc = parts[1].strip() if len(parts) > 1 else ""
        if _GROK_ASK_CUSTOM_RE.match(label):
            custom = n
        options.append({"num": n, "press": key, "label": label, "desc": desc,
                        "selected": filled})
    return {
        "question": question or "Question",
        "context": "",
        "options": options,
        "stage": "ask",
        "custom": custom,
        "raw": "\n".join(lines[top + 1:foot + 1]),
    }


def grok_pending(session_id: str) -> Optional[dict]:
    """The pending grok question card for a live session, or None."""
    screen = capture_pane(session_id, history=0)
    if screen is None:
        return None
    return parse_grok_ask(screen)


def grok_answer(session_id: str, choice: int, text: str = "",
                verify: float = 4.0) -> dict:
    """Answer a live grok question card by picking option `choice`.

    grok binds one key per row (1-9, then a-f, `z` for free text), so the key
    is the whole answer — except that picking a row selects it and only Enter
    submits. Which of the two a given card needs depends on how many questions
    it is carrying, so this presses the key, looks, and only sends Enter if the
    card is still up. Reporting honestly matters here: the autonomy watcher
    records an answered card and never comes back to it.
    """
    screen = capture_pane(session_id)
    if screen is None:
        return {"ok": False, "error": "no live tmux session"}
    ask = parse_grok_ask(screen)
    if ask is None:
        return {"ok": False, "error": "no question card on screen"}
    opt = next((o for o in ask["options"] if o["num"] == choice), None)
    if opt is None:
        return {"ok": False, "error": f"option {choice} is not on this card"}
    if choice == ask.get("custom") and not text.strip():
        return {"ok": False,
                "error": f"option {choice} is the free-text row — pass `text`"}

    before = prompt_sig(ask)
    _send_keys(session_id, "--", opt["press"])
    if choice == ask.get("custom"):
        time.sleep(0.3)                   # let the text field take focus
        _send_keys(session_id, "-l", "--", text)
        time.sleep(0.3)
        _send_keys(session_id, "Enter")
        return {"ok": True, "choice": choice, "label": opt["label"]}

    if verify <= 0:
        return {"ok": True, "choice": choice, "label": opt["label"]}

    waited, pressed_enter = 0.0, False
    while waited < verify:
        time.sleep(0.4)
        waited += 0.4
        now = prompt_sig(parse_grok_ask(capture_pane(session_id, history=0) or ""))
        if now != before:
            return {"ok": True, "choice": choice, "label": opt["label"]}
        if not pressed_enter:
            # The key selected the row without submitting — that is what Enter
            # is for on the last (or only) question of a card.
            _send_keys(session_id, "Enter")
            pressed_enter = True
    return {"ok": False,
            "error": "the question card is still on screen — the keypress didn't take"}


# ---- opencode -----------------------------------------------------------
# opencode's TUI swaps its footer hint while a turn runs: at idle it reads
# "ctrl+p commands", mid-turn a dot spinner and "esc interrupt" appear beside it.
_OPENCODE_BUSY_RE = re.compile(r"esc\s+interrupt", re.I)

# Its permission gate is a horizontal option row ("Allow once  Allow always
# Reject") under a "△ Permission required" header — no numbered menu, so
# parse_prompt (which wants "1. Yes") never sees it.
#
# The header is the marker to key on. The row's own key hints ("⇆ select  enter
# confirm") get truncated at the pane's right edge — in an 80-column pane it
# arrives as "enter con" — so keying on those would miss the gate on any narrow
# pane, which is most of them.
#
# The dialog is also a three-stage machine, not one screen: "Allow once" answers
# the request, but "Allow always" and "Reject" only swap it for a follow-up with
# its own header — Confirm/Cancel for the always-list, a free-text reason box for
# the rejection. Keying on the first header alone left the pane at stage two
# reading as an idle session while it was in fact blocked.
_OPENCODE_STAGE_RES = (
    ("permission", re.compile(r"Permission required", re.I)),
    ("always", re.compile(r"Always allow", re.I)),
    ("reject", re.compile(r"Reject permission", re.I)),
)
_OPENCODE_GATE_HEAD_RE = re.compile(
    "|".join(r.pattern for _, r in _OPENCODE_STAGE_RES), re.I)
# Case-sensitive on purpose: the row's own key hints read "enter confirm" and
# "esc cancel" in lower case, and on a narrow pane they share the option row.
_OPENCODE_OPTION_RE = re.compile(
    r"(Allow once|Allow always|Reject|Allow|Deny|Confirm|Cancel)")
# The reject stage has no ⇆ row at all — its two actions are the footer's key
# hints, so they are the options we offer for it.
_OPENCODE_REJECT_OPTIONS = ("Confirm", "Cancel")
# Footer/key-hint text, which is not part of what the gate is asking.
_OPENCODE_HINT_RE = re.compile(
    r"⇆\s*select|enter\s+conf|esc\s+(cancel|reject)|ctrl\+", re.I)
# In a colour capture the highlighted option is dark-on-accent; the others are
# rendered grey. Grey foreground is the reliable "not selected" marker.
_OPENCODE_DIM_FG = "38;2;128;128;128"


def capture_pane_ansi(session_id: str) -> Optional[str]:
    """The visible pane *with* SGR escape sequences (`capture-pane -e`).

    Only the opencode gate needs this: which option is highlighted is carried
    purely by colour, so the plain text capture can't tell them apart.
    """
    try:
        out = subprocess.run(
            ["tmux", "capture-pane", "-p", "-e", "-t", session_id],
            capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def opencode_working(session_id: str) -> bool:
    """True when the live opencode REPL is mid-turn, read from its footer."""
    screen = capture_pane(session_id, history=0)
    if not screen:
        return False
    if _OPENCODE_GATE_HEAD_RE.search(screen) \
            or _OPENCODE_ASK_FOOT_RE.search(screen):
        return False                      # sitting at a gate, not generating
    tail = "\n".join(screen.splitlines()[-6:])
    return bool(_OPENCODE_BUSY_RE.search(tail))


# Box borders and the arrow opencode puts in front of the gate's subject line.
_OPENCODE_BOX_CHARS = "┃│┆╎╹╻▀▄─━╭╮╰╯┌┐└┘ \t←→△"


def _opencode_unbox(line: str) -> str:
    """A gate body line with its panel border and lead-in glyphs stripped."""
    return _strip(line).strip(_OPENCODE_BOX_CHARS)


def _opencode_option_row(lines: list[str], head: int = 0) -> Optional[int]:
    """Index of the line holding the gate's option row, or None.

    The row is the lowest line below the header carrying more than one option
    label — "Allow once   Allow always   Reject" — which is what separates it
    from a body line that merely mentions one of those words.
    """
    for i in range(len(lines) - 1, head - 1, -1):
        if len(_OPENCODE_OPTION_RE.findall(lines[i])) > 1:
            return i
    return None


def _opencode_head(lines: list[str]) -> tuple[Optional[int], str]:
    """(index, stage) of the gate's header line, scanning up from the bottom."""
    for i in range(len(lines) - 1, -1, -1):
        for stage, rx in _OPENCODE_STAGE_RES:
            if rx.search(lines[i]):
                return i, stage
    return None, ""


def _opencode_question(lines: list[str], head: int, end: int) -> str:
    """What the gate is asking, from the body lines between header and footer.

    opencode draws the panel inside a box, so the border glyphs come off too —
    otherwise a blank line reads as "┃" — and the footer's key hints are dropped
    because they say how to answer, not what is being asked.
    """
    body = [_opencode_unbox(l) for l in lines[head + 1:end]]
    body = [b for b in body if b and not _OPENCODE_HINT_RE.search(b)]
    return " — ".join(body[:3])


def parse_opencode_gate(screen: Optional[str],
                        ansi: Optional[str] = None) -> Optional[dict]:
    """A pending opencode permission gate from a captured screen, or None.

    Returns the same shape parse_prompt does — {question, options:[{num, label,
    selected}], raw} — so the dashboard's approval UI needs no special case,
    plus `stage` ("permission", "always" or "reject") because answering the
    follow-up stages takes different keys.
    Options are numbered 1..N left-to-right to match that contract; opencode
    itself selects them with ⇆.

    `ansi` is the same frame captured with -e. Without it every option comes
    back selected=False, which is honest: the highlight is colour-only.
    """
    if not screen:
        return None
    lines = screen.splitlines()
    # The header is what says this is a gate at all, and which stage it is; the
    # option row is the one below it. Anchoring this way round keeps an
    # "Allow"/"Reject" in ordinary transcript output from being mistaken for the
    # widget.
    head, stage = _opencode_head(lines)
    if head is None:
        return None

    if stage == "reject":
        # A reason box, not a row: Enter submits the rejection, Escape backs out
        # to the first stage. Enter is the default, so it reads as selected.
        return {
            "question": _opencode_question(lines, head, len(lines))
                        or "Reject permission",
            "options": [{"num": i + 1, "label": l, "selected": i == 0}
                        for i, l in enumerate(_OPENCODE_REJECT_OPTIONS)],
            "stage": stage,
            "raw": "\n".join(lines[head:]),
        }

    row = _opencode_option_row(lines, head)
    if row is None:
        return None

    labels = _OPENCODE_OPTION_RE.findall(lines[row])
    if not labels:
        return None

    # Which one is highlighted — from the colour capture's matching row.
    selected = -1
    if ansi:
        alines = ansi.splitlines()
        arow = _opencode_option_row(alines)
        if arow is not None:
            ln = alines[arow]
            for idx, label in enumerate(labels):
                at = ln.find(label)
                if at < 0:
                    continue
                # The SGR run immediately preceding this label decides it.
                if _OPENCODE_DIM_FG not in ln[max(0, at - 24):at]:
                    selected = idx

    # The gate's subject: the lines between the header and the option row (the
    # tool and what it wants to touch, or what an always-rule would cover).
    question = _opencode_question(lines, head, row) \
        or _opencode_unbox(lines[head]) or "Permission required"

    return {
        "question": question,
        "options": [{"num": i + 1, "label": l, "selected": i == selected}
                    for i, l in enumerate(labels)],
        "stage": stage,
        "raw": "\n".join(lines[head:row + 1]),
    }


# opencode's `question` tool is a *different* widget from the permission gate: a
# numbered list with a description under each row, ending in a "Type your own
# answer" row, under a "↑↓ select  enter submit  esc dismiss" footer. The footer
# is the anchor — a transcript can easily hold a numbered list of its own.
_OPENCODE_ASK_FOOT_RE = re.compile(r"↑↓\s*select")
_OPENCODE_ASK_OPTION_RE = re.compile(r"^(\d+)\.\s+(.+?)\s*$")
_OPENCODE_ASK_CUSTOM_RE = re.compile(r"^(\[.\]\s*)?Type your own answer$", re.I)
# A background colour run. The whole dialog is painted with theme.surface, so
# "has a background" means nothing — the *odd one out* is the highlighted row.
_OPENCODE_BG_RE = re.compile(r"\x1b\[[0-9;]*?(48;(?:2;\d+;\d+;\d+|5;\d+))")
# Colour runs have to come off before a -e capture's rows can be matched as text.
_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")


def _opencode_ask_rows(lines: list[str], foot: int) -> list[tuple[int, int, str]]:
    """The dialog's numbered rows above `foot`, as (line index, number, label).

    Read bottom-up and kept only while the numbering steps down by one to 1, so
    a stray "1." in the transcript above the dialog can't extend the list. The
    per-option description lines carry no number and are skipped on the way.
    """
    found = []
    for i in range(foot - 1, -1, -1):
        m = _OPENCODE_ASK_OPTION_RE.match(_opencode_unbox(lines[i]))
        if m:
            found.append((i, int(m.group(1)), m.group(2)))
    run: list[tuple[int, int, str]] = []
    expect = None
    for i, num, label in found:
        if expect is None:
            expect = num                  # the last row, nearest the footer
        if num != expect:
            break
        run.append((i, num, label))
        expect -= 1
        if expect == 0:
            break
    run.reverse()
    return run if len(run) > 1 and run[0][1] == 1 else []


def _opencode_ask_selected(ansi: Optional[str], count: int) -> int:
    """Index of the highlighted row, or -1.

    Rows differ only by background: the selected one gets theme.line where the
    rest get the dialog's theme.surface. So the answer is whichever background
    is in the minority — which needs no knowledge of the user's theme.
    """
    if not ansi:
        return -1
    alines = ansi.splitlines()
    afoot = next((i for i in range(len(alines) - 1, -1, -1)
                  if _OPENCODE_ASK_FOOT_RE.search(alines[i])), None)
    if afoot is None:
        return -1
    rows = _opencode_ask_rows([_SGR_RE.sub("", l) for l in alines], afoot)
    if len(rows) != count:
        return -1
    bgs = []
    for i, _, _ in rows:
        m = _OPENCODE_BG_RE.search(alines[i])
        bgs.append(m.group(1) if m else "")
    odd = [b for b in set(bgs) if bgs.count(b) == 1]
    if len(odd) != 1:
        return -1                          # nothing differs, or nothing agrees
    return bgs.index(odd[0])


def parse_opencode_ask(screen: Optional[str],
                       ansi: Optional[str] = None) -> Optional[dict]:
    """A pending opencode `question` dialog from a captured screen, or None.

    Same shape as parse_opencode_gate, plus `custom`: the number of the "Type
    your own answer" row, which opens a textarea instead of replying.
    """
    if not screen:
        return None
    lines = screen.splitlines()
    foot = next((i for i in range(len(lines) - 1, -1, -1)
                 if _OPENCODE_ASK_FOOT_RE.search(lines[i])), None)
    if foot is None:
        return None
    rows = _opencode_ask_rows(lines, foot)
    if not rows:
        return None

    # The question sits directly above the first row, one blank line up, and
    # wraps across as many lines as it needs.
    head = rows[0][0] - 1
    while head >= 0 and not _opencode_unbox(lines[head]):
        head -= 1
    top = head
    while top >= 0 and _opencode_unbox(lines[top]):
        top -= 1
    question = " ".join(_opencode_unbox(l) for l in lines[top + 1:head + 1])

    selected = _opencode_ask_selected(ansi, len(rows))
    custom = next((n for _, n, label in rows
                   if _OPENCODE_ASK_CUSTOM_RE.match(label)), None)
    return {
        "question": question or "Question",
        "options": [{"num": n, "label": label, "selected": i == selected}
                    for i, (_, n, label) in enumerate(rows)],
        "stage": "ask",
        "custom": custom,
        "raw": "\n".join(lines[top + 1:foot + 1]),
    }


def _opencode_ask_answer(session_id: str, ask: dict, choice: int,
                         text: Optional[str]) -> dict:
    """Answer a question dialog. opencode's key handler maps 1..9 to "select
    this row and act on it", so the digit is the whole answer — except on the
    free-text row, where it opens a textarea we then have to fill and submit.
    """
    label = next(o["label"] for o in ask["options"] if o["num"] == choice)
    if choice > 9:
        return {"ok": False,
                "error": f"option {choice} is past the digit keys opencode binds"}
    if choice == ask.get("custom") and not (text and text.strip()):
        return {"ok": False,
                "error": f"option {choice} opens a free-text box — pass `text`"}
    _send_keys(session_id, str(choice))
    if choice == ask.get("custom"):
        time.sleep(0.3)                   # let the textarea take focus
        _send_keys(session_id, "-l", "--", text)
        time.sleep(0.3)
        _send_keys(session_id, "Enter")
    return {"ok": True, "choice": choice, "label": label}


def opencode_pending(session_id: str) -> Optional[dict]:
    """The pending opencode permission gate for a live session, or None."""
    screen = capture_pane(session_id, history=0)
    if screen is None:
        return None
    # Two unrelated widgets can be waiting on the user: the permission gate and
    # the `question` tool's dialog. Cheap-reject both before the -e capture.
    is_gate = bool(_OPENCODE_GATE_HEAD_RE.search(screen))
    if not is_gate and not _OPENCODE_ASK_FOOT_RE.search(screen):
        return None
    ansi = capture_pane_ansi(session_id)
    if is_gate:
        return parse_opencode_gate(screen, ansi)
    return parse_opencode_ask(screen, ansi)


def _opencode_pick(session_id: str, gate: dict, choice: int,
                   tries: int = 3) -> dict:
    """Move the ⇆ highlight onto option `choice` (1-based) and press Enter.

    The row wraps, so there's no "press Left until it stops" home position — we
    read where the highlight currently is, step the shortest way to the wanted
    option, re-read, and only then confirm.
    """
    opts = gate["options"]
    want = choice - 1

    for _ in range(tries):
        cur = next((i for i, o in enumerate(opts) if o["selected"]), None)
        if cur is None:
            return {"ok": False, "error": "could not read the gate's selection"}
        if cur == want:
            break
        step = "Right" if want > cur else "Left"
        for _ in range(abs(want - cur)):
            _send_keys(session_id, step)
            time.sleep(0.1)
        time.sleep(0.3)
        gate = opencode_pending(session_id)
        if gate is None:
            return {"ok": False, "error": "gate disappeared mid-answer"}
        opts = gate["options"]
    else:
        return {"ok": False, "error": "could not move the gate's selection"}

    _send_keys(session_id, "Enter")
    return {"ok": True, "choice": choice, "label": opts[want]["label"]}


def _opencode_reject(session_id: str, choice: int,
                     text: Optional[str] = None) -> dict:
    """Answer the reason box: Enter submits the rejection, Escape backs out."""
    if choice == 2:
        _send_keys(session_id, "Escape")
        return {"ok": True, "choice": 2, "label": "Cancel"}
    if text and text.strip():
        _send_keys(session_id, "-l", "--", text)
        time.sleep(0.3)
    _send_keys(session_id, "Enter")
    return {"ok": True, "choice": 1, "label": "Confirm"}


def _opencode_follow_up(session_id: str, label: str, text: Optional[str],
                        tries: int) -> Optional[dict]:
    """Drive the second dialog "Allow always"/"Reject" opens, if one appeared.

    Neither of those answers the request by itself, so stopping at the first
    Enter parks the session on a dialog nobody is watching.
    """
    if label not in ("Allow always", "Reject"):
        return None
    time.sleep(0.4)
    nxt = opencode_pending(session_id)
    if nxt is None:
        return None              # nothing followed — the gate is already gone
    stage = nxt.get("stage")
    if stage == "always":
        done = _opencode_pick(session_id, nxt, 1, tries)    # "Confirm"
        if not done["ok"]:
            return {"ok": False,
                    "error": f"could not confirm allow-always: {done['error']}"}
        return {"followed_up": "always"}
    if stage == "reject":
        _opencode_reject(session_id, 1, text)
        return {"followed_up": "reject"}
    return None


def opencode_answer(session_id: str, choice: int, text: Optional[str] = None,
                    tries: int = 3) -> dict:
    """Answer a live opencode gate by option number (1-based, left to right).

    The gate is three dialogs, not one. "Allow once" answers the request, but
    "Allow always" and "Reject" only open a follow-up — Confirm/Cancel, or a box
    asking what to do instead — so this drives that one too and a single call
    finishes the gate. `text` is the reason to type into a rejection box.
    """
    gate = opencode_pending(session_id)
    if gate is None:
        return {"ok": False, "error": "no opencode permission gate on screen"}
    opts = gate["options"]
    if not 1 <= choice <= len(opts):
        return {"ok": False,
                "error": f"choice {choice} out of range (1..{len(opts)})"}

    if gate.get("stage") == "ask":
        return _opencode_ask_answer(session_id, gate, choice, text)
    if gate.get("stage") == "reject":
        return _opencode_reject(session_id, choice, text)

    picked = _opencode_pick(session_id, gate, choice, tries)
    if not picked["ok"]:
        return picked
    follow = _opencode_follow_up(session_id, picked["label"], text, tries)
    if follow:
        picked.update(follow)
    return picked


def _opencode_input_pending(session_id: str, snippet: str) -> bool:
    """True if `snippet` still sits in opencode's composer (not yet submitted)."""
    screen = capture_pane(session_id, history=0)
    if not screen:
        return False
    key = snippet.strip()[:24]
    if not key:
        return False
    return any(key in l for l in screen.splitlines()[-8:])


def opencode_say(session_id: str, text: str, tries: int = 4) -> dict:
    """Type `text` into a live opencode REPL and submit it.

    opencode's editor takes a literal paste + Enter cleanly, but we verify the
    same way as claude and grok rather than trusting it — a swallowed Enter
    leaves the message sitting unsent in the composer, which looks identical to
    a session that simply hasn't replied yet.
    """
    if not text.strip():
        return {"ok": False, "error": "empty message"}
    if capture_pane(session_id) is None:
        return {"ok": False, "error": "no live tmux session"}
    _send_keys(session_id, "-l", "--", text)
    time.sleep(0.5)                       # let the editor ingest the paste
    for attempt in range(1, tries + 1):
        _send_keys(session_id, "Enter")
        time.sleep(0.6)
        if opencode_working(session_id) \
                or not _opencode_input_pending(session_id, text):
            return {"ok": True, "attempts": attempt}
        time.sleep(0.4)
    return {"ok": True, "attempts": tries, "warning": "submit unconfirmed"}


def agy_answer(session_id: str, action: str) -> dict:
    """Answer an agy approval gate: 'approve' → C-k, 'manage' → M-j (Alt+j),
    'reject' → Escape. (agy gates use key chords, not a numbered menu.)"""
    if capture_pane(session_id) is None:
        return {"ok": False, "error": "no live tmux session"}
    key = {"approve": "C-k", "manage": "M-j", "reject": "Escape"}.get(action)
    if not key:
        return {"ok": False, "error": f"unknown action: {action}"}
    _send_keys(session_id, key)
    return {"ok": True, "action": action}


def interrupt(session_id: str) -> dict:
    """Stop the current turn by sending Escape to the live REPL (Claude Code
    interrupts generation / a running tool on Esc). No-op error if nothing live."""
    if capture_pane(session_id) is None:
        return {"ok": False, "error": "no live tmux session"}
    _send_keys(session_id, "Escape")
    return {"ok": True}


def prompt_sig(prompt: Optional[dict]) -> str:
    """A stable fingerprint of one gate — what it asks, offers, and is about.

    Used to tell "the same gate is still up" from "a new gate replaced it",
    which is how both answer() and the autonomy watcher know whether a keypress
    actually did anything.

    The context — the command/tool preview above the question — has to be in
    here. Without it every Bash gate in a session fingerprints identically:
    they all ask "Do you want to proceed?" over "1. Yes / 2. No". A session
    running one curl after another then produced a run of gates this function
    could not tell apart, and both callers broke on it. answer() watched the
    signature after pressing 1, saw the *next* gate's identical signature and
    reported "the keypress didn't take"; the watcher meanwhile had recorded
    that signature as already-answered, so it skipped the real gate underneath.
    Three of those and it gave up — a yolo session parked on an unanswered
    prompt with the auto-approver working exactly as written.

    Whitespace is collapsed, because a repaint may rewrap the preview without
    the gate having changed at all.
    """
    if not prompt:
        return ""
    context = " ".join((prompt.get("context") or "").split())
    return prompt.get("question", "") + "|" + "|".join(
        f"{o.get('num')}{o.get('label')}" for o in (prompt.get("options") or [])
    ) + "|" + context


def answer(session_id: str, choice: int, text: str = "",
           verify: float = 4.0) -> dict:
    """Answer a live permission prompt by selecting option `choice`.

    Sends the digit then Enter into the live pane (matches Claude Code's menu).
    For a "No, and tell Claude what to do differently" style option, pass `text`
    to type the follow-up message after selecting it.

    Then it *checks*, because a keypress into a TUI is not a promise. If the
    digit lands while the menu is mid-repaint the REPL swallows it, and for a
    long time this function reported success anyway — which is what let a yolo
    session sit on an unanswered gate forever: the watcher recorded the gate as
    handled and never tried again. So wait for the gate to actually go (or be
    replaced) and report honestly if it doesn't. `verify=0` skips the wait.
    """
    screen = capture_pane(session_id)
    if screen is None:
        return {"ok": False, "error": "no live tmux session"}
    before = prompt_sig(parse_prompt(screen))

    # Select the numbered option and confirm. The pause matters: the digit sets
    # the selection and the menu redraws around it, and an Enter in the same
    # flush can arrive before that has happened.
    _send_keys(session_id, "--", str(choice))
    time.sleep(0.25)
    _send_keys(session_id, "Enter")
    if text:
        # The option opened a free-text field; type the guidance and submit.
        time.sleep(0.25)
        _send_keys(session_id, "--", text)
        _send_keys(session_id, "Enter")
        return {"ok": True}      # the follow-up box isn't a gate — nothing to verify

    if not before or verify <= 0:
        return {"ok": True}      # nothing was up, or the caller doesn't care

    waited = 0.0
    while waited < verify:
        time.sleep(0.4)
        waited += 0.4
        now = prompt_sig(parse_prompt(capture_pane(session_id, history=0) or ""))
        if now != before:
            return {"ok": True}
    return {"ok": False,
            "error": "the gate is still on screen — the keypress didn't take"}


# A multi-select answer widget's Submit button (its own line inside the box).
_SUBMIT_LINE_RE = re.compile(r"^[\s│|>❯]*Submit\s*$")
# The confirm menu shown after Submit ("Ready to submit your answers?").
_REVIEW_RE = re.compile(r"Submit answers|Ready to submit")


def _pointer_and_submit(screen: str) -> tuple[int, int]:
    """(pointer_line, submit_line) for a multi-select widget, or (-1, -1).

    The REPL's own input line also renders a ❯, so the pointer is taken as the
    ❯ line nearest the Submit line — the widget's, not the prompt's.
    """
    lines = screen.splitlines()
    subs = [i for i, l in enumerate(lines) if _SUBMIT_LINE_RE.match(l)]
    if not subs:
        return -1, -1
    submit = subs[-1]
    ptrs = [i for i, l in enumerate(lines) if "❯" in l]
    if not ptrs:
        return -1, submit
    return min(ptrs, key=lambda i: abs(i - submit)), submit


def answer_multi(session_id: str, nums: list[int], timeout: float = 8.0) -> dict:
    """Answer a multi-select (checkbox) question by ticking `nums` then Submit.

    Claude Code's multiSelect widget: a digit toggles that checkbox and leaves
    the cursor put; ↑/↓ move the cursor (clamped at the ends); Enter on the
    Submit line opens a "Ready to submit your answers?" confirm menu whose
    option 1 commits. Verified against a live widget.
    """
    screen = capture_pane(session_id, history=0)
    if screen is None:
        return {"ok": False, "error": "no live tmux session"}
    if _pointer_and_submit(screen)[1] < 0:
        return {"ok": False, "error": "no multi-select prompt on screen"}

    # 1) Toggle each requested checkbox (digits are pure toggles).
    for n in nums:
        _send_keys(session_id, "-l", "--", str(n))
        time.sleep(0.35)

    # 2) Walk the cursor onto Submit. Capture-guided so it self-corrects rather
    #    than relying on a fixed number of arrow presses.
    moved = False
    for _ in range(14):
        screen = capture_pane(session_id, history=0) or ""
        ptr, submit = _pointer_and_submit(screen)
        if submit < 0:
            return {"ok": False, "error": "multi-select prompt disappeared"}
        if ptr == submit:
            moved = True
            break
        _send_keys(session_id, "Down" if ptr < submit else "Up")
        time.sleep(0.3)
    if not moved:
        return {"ok": False, "error": "could not reach Submit"}

    # 3) Enter on Submit → review step, then confirm with option 1.
    _send_keys(session_id, "Enter")
    waited = 0.0
    while waited < timeout:
        time.sleep(0.4)
        waited += 0.4
        if _REVIEW_RE.search(capture_pane(session_id, history=0) or ""):
            _send_keys(session_id, "-l", "--", "1")
            return {"ok": True}
    return {"ok": False, "error": "submit confirm never appeared"}


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


def compact(session_id: str, instructions: str = "") -> dict:
    """Trigger Claude Code's /compact on the live REPL to shrink its context.

    Types the `/compact` slash command (optionally with focus instructions,
    e.g. "keep the auth refactor details") and submits it. Drives the live
    tmux session, same as say(). No-op error if nothing is live.
    """
    if capture_pane(session_id) is None:
        return {"ok": False, "error": "no live tmux session"}
    cmd = "/compact"
    if instructions.strip():
        cmd += " " + instructions.strip()
    # -l = literal so the slash/text aren't taken as tmux keys.
    _send_keys(session_id, "-l", "--", cmd)
    _send_keys(session_id, "Enter")
    return {"ok": True}


def _jsonl_ids(dirpath: str) -> set[str]:
    """Session ids with a transcript in `dirpath` (i.e. its *.jsonl basenames)."""
    try:
        return {n[:-6] for n in os.listdir(dirpath) if n.endswith(".jsonl")}
    except OSError:
        return set()


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def reset(session_id: str, transcript_dir: Optional[str],
          timeout: float = 25.0) -> dict:
    """Run /clear on the live REPL, then follow the session to its new id.

    /clear doesn't restart anything — the process, the pane and the tmux name
    all stay put. What changes is underneath: Claude drops the conversation and
    starts writing a *new* transcript under a fresh uuid. That breaks this
    module's one invariant, tmux name == Claude session id, and it's why a
    hand-typed /clear leaves the dashboard driving a session that no longer
    exists: every send goes to a pane named after the dead id, and the new
    transcript shows up as a stranger with no tmux of its own.

    So watch the project's transcript directory for the file that appears — the
    SessionStart:clear hook writes into it within a second or so — and
    `tmux rename-session` onto it. Same pane, same REPL, name corrected.

    Returns {ok, session_id (the new one), old_id}. ok False with the clear
    already sent means we lost track of the new id; say so plainly, because the
    old conversation is gone either way and nothing will bring it back.
    """
    if capture_pane(session_id) is None:
        return {"ok": False, "error": "no live tmux session"}
    if not transcript_dir or not os.path.isdir(transcript_dir):
        return {"ok": False, "error": "transcript directory not found"}

    before = _jsonl_ids(transcript_dir)
    _send_keys(session_id, "-l", "--", "/clear")
    # A beat before Enter. Typing a slash pops the command menu, and submitting
    # into it mid-render can select the wrong entry.
    time.sleep(0.5)
    _send_keys(session_id, "Enter")

    waited = 0.0
    while waited < timeout:
        time.sleep(0.5)
        waited += 0.5
        fresh = _jsonl_ids(transcript_dir) - before
        fresh.discard(session_id)
        if not fresh:
            continue
        # Newest wins, in case another session in the same project started at
        # the same moment.
        new_id = max(fresh, key=lambda i: _mtime(os.path.join(transcript_dir, i + ".jsonl")))
        return _rename_onto(session_id, new_id)

    return {"ok": False, "old_id": session_id,
            "error": "context cleared, but the new session id never appeared — "
                     "the tmux session is still named after the old one"}


def _rename_onto(old_id: str, new_id: str) -> dict:
    """Point the tmux name at the id the REPL now writes under.

    The rename *is* the reset, as far as this module is concerned: until it
    lands, name == session id is broken and every send goes to a pane named
    after a conversation that no longer exists.
    """
    try:
        r = subprocess.run(["tmux", "rename-session", "-t", old_id, new_id],
                           capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        return {"ok": False, "old_id": old_id, "session_id": new_id, "error": str(e)}
    if r.returncode != 0:
        return {"ok": False, "old_id": old_id, "session_id": new_id,
                "error": r.stderr.strip() or "tmux rename-session failed"}
    return {"ok": True, "old_id": old_id, "session_id": new_id}


def _grok_session_ids(parent_dir: str) -> set[str]:
    """Session ids grok has stored under one project directory."""
    try:
        return {n for n in os.listdir(parent_dir)
                if os.path.isfile(os.path.join(parent_dir, n, "summary.json"))}
    except OSError:
        return set()


def grok_reset(session_id: str, parent_dir: Optional[str],
               timeout: float = 25.0) -> dict:
    """Run /new on a live grok REPL, then follow it to the session id it gets.

    Same problem as reset(), different store. /new restarts nothing — same
    process, same pane, same tmux name — but grok begins writing to a *new*
    session directory, so the tmux name stops matching the session it drives.
    Watch the project's session folder for the directory that appears and
    rename the tmux onto it.

    grok's own store is the source of truth: each session is
    ~/.grok/sessions/<enc-cwd>/<id>/ and gets a summary.json as soon as it
    exists, so a new id is visible without parsing anything.

    Submits through grok_say because grok's editor debounces keystrokes — an
    Enter fired straight after the text lands as a newline often enough that a
    plain send would leave "/new" sitting unsent in the composer.

    Returns {ok, session_id (the new one), old_id}.
    """
    if capture_pane(session_id) is None:
        return {"ok": False, "error": "no live tmux session"}
    if not parent_dir or not os.path.isdir(parent_dir):
        return {"ok": False, "error": "grok session directory not found"}

    before = _grok_session_ids(parent_dir)
    sent = grok_say(session_id, "/new")
    if not sent.get("ok"):
        return {"ok": False, "old_id": session_id,
                "error": sent.get("error", "/new was not sent")}

    waited = 0.0
    while waited < timeout:
        time.sleep(0.5)
        waited += 0.5
        fresh = _grok_session_ids(parent_dir) - before
        fresh.discard(session_id)
        if not fresh:
            continue
        # Newest wins, in case another grok session in the same project started
        # at the same moment.
        new_id = max(fresh, key=lambda i: _mtime(os.path.join(parent_dir, i)))
        return _rename_onto(session_id, new_id)

    return {"ok": False, "old_id": session_id,
            "error": "/new was sent, but no new grok session appeared — "
                     "the tmux session is still named after the old one"}


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
