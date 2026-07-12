"""
agyparser.py — read Antigravity (`agy`) CLI conversations for the dashboard.

`agy` stores each conversation as a SQLite DB at
    ~/.gemini/antigravity-cli/conversations/<conversation-id>.db
with a `steps` table of protobuf-encoded turns (user messages, tool calls,
results). We don't have the .proto schema, so we extract readable text
heuristically from the step payload blobs — enough for read-only monitoring:
a title, the workspace path, a rolling activity feed, and timestamps.

This mirrors parser.py's summary shape so agy sessions render on the same board
alongside Claude sessions, tagged with origin "agy". Override the data dir with
AGY_CLI_DIR (default ~/.gemini/antigravity-cli).
"""

from __future__ import annotations

import glob
import json
import os
import re
import sqlite3

from . import parser as claude_parser   # reuse status thresholds + helpers

AGY_DIR = os.path.expanduser(os.environ.get("AGY_CLI_DIR", "~/.gemini/antigravity-cli"))
CONV_DIR = os.path.join(AGY_DIR, "conversations")

# A printable-ASCII run of length >= 5 — the readable bits inside a protobuf blob.
_RUN_RE = re.compile(rb"[ -~]{5,}")
# A JSON object embedded in a tool step, e.g. {"toolAction":"…","toolSummary":"…"}.
_JSON_RE = re.compile(r"\{[^{}]*\"tool(?:Action|Summary)\"[^{}]*\}")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-", re.IGNORECASE)


def _runs(blob) -> list[str]:
    if not blob:
        return []
    return [m.group().decode("ascii", "replace") for m in _RUN_RE.finditer(blob)]


def _tool_action(runs: list[str]) -> str | None:
    """The human 'toolAction' of a tool step (e.g. 'Running git status')."""
    for r in runs:
        m = _JSON_RE.search(r)
        if not m:
            continue
        try:
            o = json.loads(m.group())
        except ValueError:
            continue
        act = (o.get("toolAction") or o.get("toolSummary") or "").strip()
        if act:
            return act
    return None


# Step-type codes observed in agy conversation DBs. 15 carries the assistant's
# natural-language message; 14 the user's message. Tool steps (5/8/9/21/23) hold
# a tool call with a JSON toolAction/toolSummary.
_ASSISTANT_TYPES = {15}
_USER_TYPES = {14}


def _clean_prose(s: str) -> str:
    """Trim the protobuf length-prefix junk (leading) and field junk (trailing)
    around a natural-language run."""
    m = re.search(r"[A-Za-z]", s)
    if m and m.start() <= 4:
        s = s[m.start():]
    s = re.split(r"\(bot-|\x00", s)[0]
    return re.sub(r"([.!?])\s*\d+\s*$", r"\1", s).strip().strip('"').strip()


def _assistant_text(runs: list[str]) -> str | None:
    """Longest prose-like run in an assistant step — words + spaces, not code /
    JSON / a path (those have a low letter+space ratio)."""
    best = None
    for r in runs:
        s = _clean_prose(r.strip().strip('"'))
        if not s or s.startswith(("/", "file://", "{")) or _UUID_RE.match(s):
            continue
        if len(s.split()) < 4:
            continue
        letters = sum(c.isalpha() or c.isspace() for c in s)
        if letters / len(s) < 0.75:       # excludes code / config / tool output
            continue
        if len(s) >= 12 and (best is None or len(s) > len(best)):
            best = s
    return best


_UUID_ANYWHERE_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}", re.I)


def _user_text(runs: list[str]) -> str | None:
    """The user's message text from a user-type step's runs. Unlike assistant
    prose this keeps SHORT messages (e.g. "WSO2 ABA"); it just skips the uuid /
    path / json junk runs. Used for the title and for pane↔db correlation."""
    best = None
    for r in runs:
        s = r.strip().strip('"').strip()
        # Drop leading protobuf junk before the first letter.
        m = re.search(r"[A-Za-z]", s)
        if m and m.start() <= 3:
            s = s[m.start():]
        s = s.strip().strip('"').strip()
        # Skip uuids, paths (any '/'), json/skill junk — keep real message text.
        if not s or "/" in s or s.startswith(("{", "$")) or _UUID_ANYWHERE_RE.search(s):
            continue
        if len(s) >= 2 and (best is None or len(s) > len(best)):
            best = s
    return best


def _step_text(step_type: int, runs: list[str]) -> str | None:
    """The one-line content of a step: assistant message, user message, or the
    tool action."""
    if step_type in _USER_TYPES:
        return _user_text(runs)
    if step_type in _ASSISTANT_TYPES:
        return _assistant_text(runs)
    return _tool_action(runs) or _assistant_text(runs)


# A filesystem path embedded in a run (protobuf field, possibly length-prefixed
# so it isn't at the start), e.g. "bDfile:///Users/me/proj" or "…/Users/me/proj".
_PATH_RE = re.compile(r"(?:file://)?(/(?:Users|home)/[^\"\\\x00]*)")


def _cwd_of(step_runs: list[list[str]]) -> str | None:
    """The conversation's workspace dir — the first real project path, skipping
    agy's own internal paths (.gemini/antigravity-cli, logs, transcripts)."""
    for runs in step_runs:
        for r in runs:
            m = _PATH_RE.search(r)
            if not m:
                continue
            p = m.group(1).rstrip("/")
            low = p.lower()
            if "/.gemini/" in low or "/antigravity-cli/" in low or low.endswith(".jsonl"):
                continue
            return p
    return None


# Throttle the (pane-capturing) tmux↔conversation reconciler.
_RECONCILE_TTL = 8.0
_RECONCILE_STATE = {"at": 0.0}


# Cache parsed summaries per db, keyed on (mtime, size). Parsing a conversation
# (sqlite + protobuf-ish extraction of every step) is expensive; the board /
# Attention page poll every 1–3s, so without this each poll re-parses every db.
_SUMM_CACHE: dict[str, tuple] = {}


def _summarize(db_path: str) -> dict | None:
    cid = os.path.splitext(os.path.basename(db_path))[0]
    try:
        st = os.stat(db_path)
    except OSError:
        return None
    mtime = st.st_mtime
    cached = _SUMM_CACHE.get(db_path)
    if cached and cached[0] == mtime and cached[1] == st.st_size:
        s = dict(cached[2])
        s["status"] = claude_parser.compute_status(mtime)   # age is wall-clock
        return s
    rows = []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            cur = con.execute(
                "select idx, step_type, status, step_payload from steps order by idx")
            rows = cur.fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        rows = []

    step_runs = [_runs(r[3]) for r in rows]
    cwd = _cwd_of(step_runs)

    # Title = the opening user message (first user-type step), else first prose.
    # Title = the first user message (keeps short ones like "WSO2 ABA"); this
    # must equal the pane's opening `> …` line for tmux↔db correlation.
    title = None
    for (idx, step_type, status, _), runs in zip(rows, step_runs):
        if step_type in _USER_TYPES:
            title = _user_text(runs)
            if title:
                break
    if not title:
        for runs in step_runs:
            title = _assistant_text(runs)
            if title:
                break

    # Activity feed = each step's message / tool action, in order.
    acts = []
    for (idx, step_type, status, _), runs in zip(rows, step_runs):
        text = _step_text(step_type, runs)
        if text and text != title:
            acts.append({"kind": "agy", "text": text[:200]})
    recent = acts[-3:]

    created = claude_parser._iso(os.path.getctime(db_path)) if hasattr(os.path, "getctime") else None
    summary = {
        "session_id": cid,
        "title": title or "(agy conversation)",
        "project": _project_label(cwd),
        "cwd": cwd,
        "model": None,
        "entrypoint": "agy",
        "origin": "agy",
        "source": "agy",
        "status": claude_parser.compute_status(mtime),
        "turn_pending": False,
        "created_at": created,
        "updated_at": claude_parser._iso(mtime),
        "mtime": mtime,
        "tokens": {"input": 0, "output": 0, "cache_read": 0,
                   "cache_creation": 0, "total": 0},
        "step_count": len(rows),
        "last_activities": recent,
        "live_tmux": False,
        "live": False,
        "live_web": False,
        "pending_approval": False,
        "archived": False,
        "attention": False,
        "renamed": False,
        "autonomy": "manual",
    }
    _SUMM_CACHE[db_path] = (mtime, st.st_size, summary)
    return dict(summary)


def _project_label(cwd: str | None) -> str:
    if not cwd:
        return "agy"
    return os.path.basename(cwd.rstrip("/")) or cwd


_RULE_RE = re.compile(r"─{10,}")

# --- Live console parsing ---------------------------------------------------
# agy redraws the FULL conversation on every turn, so a scrollback capture holds
# repeated renders. We parse from the last banner (the newest, complete render)
# into structured events shaped like the Claude history (kind/text/name).
_BANNER_RE = re.compile(r"Antigravity CLI \d")
_TOOL_RE = re.compile(r"^●\s*([A-Za-z][\w ]*?)\((.*)$")     # "● ListDir(/path)"
_THOUGHT_RE = re.compile(r"^[▸▶]\s*Thought\b")               # "▸ Thought for 2s…"
_USER_RE = re.compile(r"^>\s+(.*)$")                          # "> show me all files"
_ASK_RE = re.compile(r"^\?\s+(.*)$")                          # "? What would you like…"
# Footer / chrome lines to ignore.
_SKIP_RE = re.compile(
    r"(\? for shortcuts|↑/↓ Navigate|esc to (cancel|interrupt)|tab (Amend|Complete)|"
    r"ctrl\+[a-z]|\(ctrl\+o to expand\)$|Google AI Pro|Gemini .* \(|"
    r"[▄▀▝▘▙▟]|fkarnagi@|@gmail\.com)")   # any block-art glyph = banner chrome


def parse_console(screen: str | None) -> list[dict]:
    """Parse the agy console pane into Claude-style events, newest-first.

    Emits {kind, text, name?} where kind is user / assistant / tool / thinking.
    """
    if not screen:
        return []
    lines = screen.splitlines()
    # Start at the last full render (after the last banner) — the current
    # conversation, so repeated redraws in scrollback don't duplicate turns.
    start = 0
    for i, ln in enumerate(lines):
        if _BANNER_RE.search(ln):
            start = i
    lines = lines[start:]

    events: list[dict] = []
    asst: list[str] = []          # accumulating assistant text lines

    def flush_asst():
        if asst:
            txt = "\n".join(asst).strip()
            if txt:
                events.append({"kind": "assistant", "text": txt})
            asst.clear()

    in_gate = False
    for ln in lines:
        raw = ln.rstrip()
        s = raw.strip()
        if not s or _RULE_RE.match(s) or _BANNER_RE.search(s) or _SKIP_RE.search(s):
            continue
        # Skip the trailing permission-menu block ("Do you want to proceed?" +
        # "> 1. Yes / 2. … / 4. No") — that's the live gate, not conversation.
        if re.match(r"(do you want to proceed|requesting permission|allow this)", s, re.I):
            in_gate = True
            continue
        if in_gate:
            if re.match(r">?\s*\d+\.\s", s) or s.lower().startswith(("yes", "no,")):
                continue
            in_gate = False
        mt = _TOOL_RE.match(s)
        mu = _USER_RE.match(s)
        ma = _ASK_RE.match(s)
        if mu:                     # user turn
            flush_asst()
            events.append({"kind": "user", "text": mu.group(1).strip()})
        elif mt:                   # tool call: "Name(args"
            flush_asst()
            name = mt.group(1).strip()
            arg = mt.group(2).rstrip()
            arg = re.sub(r"\)\s*\(ctrl\+o to expand\)$", "", arg).rstrip(")")
            events.append({"kind": "tool", "name": name, "text": arg})
        elif _THOUGHT_RE.match(s):  # thinking header — skip the label itself
            flush_asst()
        elif ma:                   # assistant question
            flush_asst()
            events.append({"kind": "assistant", "text": ma.group(1).strip()})
        else:                      # assistant prose (incl. indented continuation)
            asst.append(raw.lstrip() if raw.startswith("  ") else s)
    flush_asst()
    events.reverse()              # newest first, matching the Claude history
    return events


# agy's active spinner line, e.g. "⣷ Generating…", "⣿ Running...", "⠹ Thinking...".
# Match a spinner verb immediately followed by an ellipsis or 2+ dots (the live
# animation) — this avoids prose like "tasks currently running:".
_GEN_RE = re.compile(
    r"\b(?:Generating|Thinking|Working|Running|Processing|Loading|Executing|"
    r"Waiting|Analyzing|Reading|Writing|Searching|Building)\s*(?:…|\.{2,})")
# Braille spinner glyph at line start (the animated dot indicator).
_BRAILLE_RE = re.compile(r"^\s*[⠀-⣿]\s", re.MULTILINE)


# The model shown in the agy status-bar footer, e.g. "Gemini 3.5 Flash (Medium)",
# "Claude Sonnet 4.6 (Thinking)", "GPT-OSS 120B (Medium)".
_MODEL_RE = re.compile(
    r"((?:Gemini|Claude|GPT-OSS|GPT|Llama|Mistral)[\w.\- ]*?\([^)]+\))")


def parse_model_picker(screen: str | None) -> dict | None:
    """Parse an open agy "Switch Model" picker, or None. Returns
    {options:[name,…], current_idx, cursor_idx}. The picker is a ↑/↓ list with
    `>` marking the highlighted row and "(current)" the active model."""
    if not screen or "Switch Model" not in screen:
        return None
    lines = screen.splitlines()
    try:
        start = max(i for i, l in enumerate(lines) if l.strip() == "Switch Model")
    except ValueError:
        return None
    options, cursor_idx, current_idx = [], 0, 0
    for ln in lines[start + 1:]:
        s = ln.rstrip()
        if not s.strip() or s.strip().startswith("Keyboard:"):
            break
        is_cursor = s.lstrip().startswith(">")
        name = re.sub(r"^\s*>?\s*", "", s)
        name = re.sub(r"\s*\(current\)\s*$", "", name).strip()
        if not name:
            continue
        if is_cursor:
            cursor_idx = len(options)
        if s.rstrip().endswith("(current)"):
            current_idx = len(options)
        options.append(name)
    if not options:
        return None
    return {"options": options, "current_idx": current_idx, "cursor_idx": cursor_idx}


def model_options() -> list[str]:
    """The models `agy models` offers, for the switch-model UI."""
    import subprocess
    agy = os.path.expanduser(os.environ.get("AGY_BIN", "~/.local/bin/agy"))
    try:
        out = subprocess.run([agy, "models"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


def model_from_screen(screen: str | None) -> str | None:
    """The active model from the agy pane's footer status bar, or None."""
    if not screen:
        return None
    # Scan the last non-empty lines (footer) upward for a model token.
    lines = [ln for ln in screen.splitlines() if ln.strip()]
    for ln in reversed(lines[-6:]):
        m = _MODEL_RE.search(ln)
        if m:
            return m.group(1).strip()
    return None


# Per-step token counts in the console, e.g. "Thought for 2s, 1.2k tokens",
# "↓ 561 tokens". Summed as an approximate output-token total.
_TOK_RE = re.compile(r"([\d.]+)\s*(k)?\s*tokens", re.I)


def _tok_val(num: str, k: str) -> int:
    try:
        return int(float(num) * (1000 if k else 1))
    except ValueError:
        return 0


def tokens_from_screen(screen: str | None) -> int:
    """Approximate total tokens, summed from the console's 'N tokens' counters."""
    if not screen:
        return 0
    return sum(_tok_val(n, k) for n, k in _TOK_RE.findall(screen))


def token_breakdown(screen: str | None) -> dict:
    """Per-line token usage parsed from the console, e.g. each
    "Thought for 2s, 1.2k tokens". Returns {total, entries:[{label,tokens}]}."""
    if not screen:
        return {"total": 0, "entries": []}
    entries, total = [], 0
    for ln in screen.splitlines():
        m = _TOK_RE.search(ln)
        if not m:
            continue
        val = _tok_val(m.group(1), m.group(2))
        total += val
        # Label = the text before the token count, tidied (e.g. "Thought for 2s").
        label = ln[:m.start()].strip(" ,·▸▶↓⎿*✻✽✶✳").strip(" ,") or "step"
        entries.append({"label": label[:60], "tokens": val})
    return {"total": total, "entries": entries}


def is_generating(screen: str | None) -> bool:
    """True when the agy REPL is actively generating (spinner / '…running…' up)."""
    if not screen:
        return False
    return bool(_GEN_RE.search(screen) or _BRAILLE_RE.search(screen))


def spinner_line(screen: str | None) -> str | None:
    """The active agy spinner line (e.g. "⣷ Generating… (2m · ↓ 561 tokens)"),
    or None if idle. Shown in the detail header like claude's spinner."""
    if not screen:
        return None
    # Prefer the braille-spinner status line; else the verb+ellipsis line.
    for ln in reversed([l for l in screen.splitlines() if l.strip()][-8:]):
        s = ln.strip()
        if re.match(r"[⠀-⣿]\s", s) or _GEN_RE.search(s):
            return s
    return None


# agy's subagent / tool approval gate, e.g.
#   coder needs approval for Bash
#   ─────
#   .venv/bin/pip install pytest sqlalchemy
#   ─────
#   ctrl+k approve · alt+j manage
# Answered by ctrl+k (approve), not a numbered menu. Detect it and expose it as a
# prompt so the dashboard can show + answer it.
_APPROVAL_HEAD_RE = re.compile(r"^(.*?)\bneeds approval for\b\s*(.*)$", re.I)
_APPROVE_HINT_RE = re.compile(r"ctrl\+k\s+approve", re.I)


def parse_gate(screen: str | None) -> dict | None:
    """A pending agy approval gate, or None. Shape mirrors tmuxio.parse_prompt so
    the same UI renders it: {question, context, options[{num,label,key}], agy}.
    """
    if not screen or not _APPROVE_HINT_RE.search(screen):
        return None
    lines = screen.splitlines()
    # Find the "<who> needs approval for <what>" header nearest the bottom.
    head_i, who, what = None, "", ""
    for i in range(len(lines) - 1, -1, -1):
        m = _APPROVAL_HEAD_RE.match(lines[i].strip())
        if m:
            head_i, who, what = i, m.group(1).strip(), m.group(2).strip()
            break
    if head_i is None:
        return None
    # Context = the non-rule, non-chrome lines between the header and the
    # "ctrl+k approve" hint (the command / detail being approved).
    ctx = []
    for ln in lines[head_i + 1:]:
        s = ln.strip()
        if _APPROVE_HINT_RE.search(s):
            break
        if s and not _RULE_RE.match(s):
            ctx.append(s)
    question = f"{who} needs approval for {what}".strip()
    return {
        "question": question,
        "context": "\n".join(ctx).strip(),
        "options": [
            {"num": 1, "label": "✓ Approve (ctrl+k)", "key": "approve", "selected": True},
            {"num": 2, "label": "⚙ Manage (alt+j)", "key": "manage"},
            {"num": 3, "label": "✕ Reject (esc)", "key": "reject"},
        ],
        "agy": True,
        "raw": screen,
    }


def at_input_box(screen: str | None) -> bool:
    """True when the agy REPL is idle at its `>` input box (framed by rules).
    Used to tell an idle live session (WAITING) from one that's generating."""
    if not screen:
        return False
    lines = screen.splitlines()
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s.startswith(">"):
            continue
        if re.match(r"\d+\.", s[1:].strip()):   # "> 1. Yes" is a gate option
            continue
        above = lines[i - 1] if i > 0 else ""
        below = lines[i + 1] if i + 1 < len(lines) else ""
        if _RULE_RE.search(above) and _RULE_RE.search(below):
            return True
    return False


def _pane_first_user_msg(screen: str | None) -> str | None:
    """The first `> <message>` line in the (last) agy render — the opening user
    message, used to correlate a pane to its conversation db by content."""
    if not screen:
        return None
    lines = screen.splitlines()
    start = 0
    for i, ln in enumerate(lines):
        if _BANNER_RE.search(ln):
            start = i
    for ln in lines[start:]:
        m = _USER_RE.match(ln.strip())
        if m and m.group(1).strip():
            return m.group(1).strip()
    return None


def reconcile_tmux_names() -> dict:
    """Ensure each live agy tmux is named after the conversation it's running.

    agy mints its id late (temp tmux name at first), and a workspace can hold
    several conversations — so cwd alone is ambiguous. We correlate by CONTENT:
    the pane's opening user message must equal the db's title (first user
    message). For EVERY live agy pane (even one already named like a valid but
    WRONG conversation id) we find the db whose title matches and rename if the
    tmux name differs. Returns {old_name: conversation_id} for renames done.

    Throttled: captures scrollback for every candidate pane, so it only actually
    runs once per _RECONCILE_TTL seconds (the board/Attention poll faster).
    """
    import time as _time
    now = _time.monotonic()
    if now - _RECONCILE_STATE["at"] < _RECONCILE_TTL:
        return {}
    _RECONCILE_STATE["at"] = now
    from . import tmuxio, parser as claude_parser
    live = tmuxio.tmux_sessions()
    if not live:
        return {}
    # Fast path: if every live session already maps to a known conversation id or
    # a claude transcript, there's nothing to reconcile — skip the pane captures.
    known = conversation_ids()
    if all(s in known or claude_parser.session_path(s) is not None for s in live):
        return {}
    # Title -> conversation id (first user message uniquely identifies a convo).
    import glob as _glob
    title_map: dict[tuple, str] = {}
    for db in _glob.glob(os.path.join(CONV_DIR, "*.db")):
        cid = os.path.splitext(os.path.basename(db))[0]
        summ = _summarize(db)
        if summ and summ.get("title"):
            title_map[(summ.get("cwd"), summ["title"].strip())] = cid

    renamed = {}
    for name in live:
        if claude_parser.session_path(name) is not None:
            continue                          # a claude session — leave it
        pane = tmuxio.capture_pane(name, history=3000) or ""
        if not _BANNER_RE.search(pane):
            continue                          # not an agy pane
        msg = _pane_first_user_msg(pane)
        if not msg:
            continue
        cwd = tmuxio.pane_cwd(name)
        cid = title_map.get((cwd, msg)) or title_map.get((None, msg))
        # Fall back: match on title alone if cwd didn't line up.
        if not cid:
            for (_c, t), c in title_map.items():
                if t == msg:
                    cid = c
                    break
        if cid and cid != name and tmuxio.rename_session(name, cid):
            renamed[name] = cid
    return renamed


def conversation_ids() -> set[str]:
    return {os.path.splitext(os.path.basename(p))[0]
            for p in glob.glob(os.path.join(CONV_DIR, "*.db"))}


def has_conversation(cid: str) -> bool:
    return os.path.isfile(os.path.join(CONV_DIR, f"{cid}.db"))


def list_conversations() -> list[dict]:
    """All agy conversations as dashboard summaries, newest activity first."""
    out = []
    for p in glob.glob(os.path.join(CONV_DIR, "*.db")):
        s = _summarize(p)
        if s and s["step_count"] > 0:      # skip empty/never-used conversations
            out.append(s)
    out.sort(key=lambda x: x.get("mtime") or 0, reverse=True)
    return out


def get_conversation(cid: str) -> dict | None:
    """Full detail for one agy conversation: summary header + activity list."""
    path = os.path.join(CONV_DIR, f"{cid}.db")
    if not os.path.isfile(path):
        return None
    s = _summarize(path)
    if not s:
        return None
    # Full ordered activity list (newest first) for the history view.
    rows = []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            rows = con.execute(
                "select idx, step_type, step_payload from steps order by idx").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        rows = []
    acts = []
    for idx, step_type, payload in rows:
        text = _step_text(step_type, _runs(payload))
        if text:
            acts.append({"kind": "agy", "name": None, "text": text[:2000]})
    acts.reverse()
    detail = dict(s)
    detail["activities"] = acts
    return detail
