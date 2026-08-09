"""
autonomy.py — per-session autonomy levels + an auto-approver for live gates.

Levels (per session, persisted to server/.autonomy.json; absence == manual):

  manual      every permission gate waits for a human (dashboard / Slack).
  auto-safe   auto-approve read-only / low-risk gates; escalate writes &
              shell commands to a human.
  yolo        auto-approve every gate (pick the affirmative option).

A background watcher (start_watcher) scans live tmux gates and applies the
policy. It is the *single* authority for auto-answering — the Slack watcher
only POSTS gates for sessions still on `manual`. Two kill switches disable all
auto-answering without changing per-session levels:

  * env AUTONOMY_DISABLED=1   (process-wide, set before launch)
  * set_paused(True)          (runtime toggle, e.g. from the triage view)

This module never imports slackbot (avoids an import cycle); callers register a
notify hook via set_auto_answer_hook so an auto-answer can be mirrored to Slack.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Callable, Optional

from . import agyparser, tmuxio

_PATH = os.path.join(os.path.dirname(__file__), ".autonomy.json")
_lock = threading.Lock()

LEVELS = ("manual", "auto-safe", "yolo")
DEFAULT = "manual"

# Runtime global pause (in addition to the AUTONOMY_DISABLED env switch).
_paused = False
_started = False

# Hook(sid, level, choice, prompt) called best-effort after an auto-answer.
_hook: Optional[Callable[[str, str, int, dict], None]] = None

# {sid: (gate signature, when)} for the last gate we answered, so we don't press
# twice while it lingers on screen for a poll or two.
#
# It expires. Two identical commands in a row produce two gates with the same
# signature, and a permanent entry would mean the second one never gets
# answered — the session parks on it forever with autonomy switched on. Holding
# the memory only long enough to cover the repaint is the safe way round: press
# again a moment later at worst, versus never pressing at all.
_answered: dict[str, tuple[str, float]] = {}
ANSWERED_TTL = 15.0

# {sid: (gate signature, attempts)} for answers that didn't take. A keypress
# into a TUI can be swallowed mid-repaint; before tmuxio.answer verified its
# own work this went unnoticed and the gate was marked handled anyway, so a
# yolo session could sit on an unanswered gate indefinitely. Now we retry —
# but only so many times, because a gate that won't budge after several tries
# is telling us something a louder keypress won't fix.
_fails: dict[str, tuple[str, int]] = {}
MAX_ATTEMPTS = 3

POLL_SECS = float(os.environ.get("AUTONOMY_POLL_SECS", "2"))


# --- persistent per-session levels ------------------------------------------

def _load() -> dict[str, str]:
    try:
        with open(_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return {k: v for k, v in data.items() if v in LEVELS}
    except (OSError, ValueError):
        return {}


def _save(d: dict[str, str]) -> None:
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=0, sort_keys=True)
    os.replace(tmp, _PATH)


def get(session_id: str) -> str:
    with _lock:
        return _load().get(session_id, DEFAULT)


def all() -> dict[str, str]:
    with _lock:
        return _load()


def set(session_id: str, level: str) -> str:
    if level not in LEVELS:
        raise ValueError(f"bad level: {level!r}")
    with _lock:
        d = _load()
        if level == DEFAULT:
            d.pop(session_id, None)
        else:
            d[session_id] = level
        _save(d)
    # A level change means the next gate should be reconsidered.
    _answered.pop(session_id, None)
    return level


def rekey(old_id: str, new_id: str) -> None:
    """Move a session's autonomy level onto a new id (see tmuxio.reset).

    How much you trust this session to answer its own gates is a standing
    decision about the work; a /clear shouldn't silently drop it back to
    manual. The de-dupe memory is dropped, though — the new conversation's
    first gate deserves a fresh look.
    """
    if old_id == new_id:
        return
    with _lock:
        d = _load()
        if old_id in d:
            d[new_id] = d.pop(old_id)
            _save(d)
    _answered.pop(old_id, None)


# --- global pause -----------------------------------------------------------

def env_disabled() -> bool:
    return os.environ.get("AUTONOMY_DISABLED", "") not in ("", "0", "false", "False")


def is_paused() -> bool:
    return _paused or env_disabled()


def set_paused(paused: bool) -> bool:
    global _paused
    _paused = bool(paused)
    return _paused


def set_auto_answer_hook(fn: Optional[Callable[[str, str, int, dict], None]]) -> None:
    global _hook
    _hook = fn


# --- policy -----------------------------------------------------------------

# A gate whose text contains any of these is treated as a *write / action* and
# is escalated to a human under auto-safe (only auto-approved under yolo).
_UNSAFE = (
    "bash", "command", "run ", "execute", "shell", "script",
    "write", "edit", "create", "modify", "overwrite", "apply",
    "delete", "remove", "rm ", "rmdir", "git ", "push", "commit",
    "install", "npm", "pip", "yarn", "pnpm", "chmod", "chown",
    "sudo", "kill", "curl", "wget", "deploy",
)
# Read-only / low-risk markers that make a gate auto-approvable under auto-safe.
_SAFE = (
    "read", "view", "fetch", "search", "grep", "glob", "list",
    "do you want to read", "show", "cat ",
)


def _affirmative(prompt: dict) -> Optional[int]:
    """The option number that means 'yes/proceed/allow', or None."""
    opts = prompt.get("options") or []
    for o in opts:
        lab = (o.get("label") or "").lower()
        if lab.startswith("yes") or "proceed" in lab or lab.startswith("allow") \
           or "trust" in lab or "approve" in lab:
            return o.get("num")
    # Claude Code's first option is the affirmative one by convention.
    return opts[0]["num"] if opts else None


def _blob(prompt: dict) -> str:
    parts = [prompt.get("question", ""), prompt.get("context", "")]
    parts += [o.get("label", "") for o in (prompt.get("options") or [])]
    return " ".join(parts).lower()


def decide(level: str, prompt: dict) -> Optional[int]:
    """Option number to auto-select for this gate, or None to leave for a human."""
    if level == "yolo":
        return _affirmative(prompt)
    if level == "auto-safe":
        blob = _blob(prompt)
        if any(m in blob for m in _UNSAFE):
            return None                      # an action — escalate
        if any(m in blob for m in _SAFE):
            return _affirmative(prompt)      # read-only — approve
        return None                          # ambiguous — escalate (conservative)
    return None                              # manual


def _sig(prompt: dict) -> str:
    # Same fingerprint tmuxio.answer uses to check its own keypress landed, so
    # "we answered this gate" and "this gate went away" mean the same thing.
    return tmuxio.prompt_sig(prompt)


def _just_answered(sid: str, sig: str) -> bool:
    """True if we pressed this exact gate moments ago and it may still be up."""
    prev, at = _answered.get(sid, ("", 0.0))
    return prev == sig and (time.time() - at) < ANSWERED_TTL


def _mark_answered(sid: str, sig: str) -> None:
    _answered[sid] = (sig, time.time())


def _too_many_tries(sid: str, sig: str) -> bool:
    """True once this exact gate has swallowed MAX_ATTEMPTS answers."""
    fsig, n = _fails.get(sid, ("", 0))
    return fsig == sig and n >= MAX_ATTEMPTS


def _note_failure(sid: str, sig: str, level: str, choice: int, err: str) -> None:
    fsig, n = _fails.get(sid, ("", 0))
    n = n + 1 if fsig == sig else 1
    _fails[sid] = (sig, n)
    print(f"[autonomy] {sid[:8]} {level} → option {choice} DIDN'T TAKE "
          f"(attempt {n}/{MAX_ATTEMPTS}): {err}")
    if n >= MAX_ATTEMPTS:
        print(f"[autonomy] {sid[:8]} giving up on this gate — needs a human")


# --- watcher ----------------------------------------------------------------

def _agy_pending_ids() -> set:
    """Live agy conversations currently sitting at an approval gate."""
    return {sid for sid in tmuxio.tmux_sessions()
            if agyparser.has_conversation(sid)
            and agyparser.parse_gate(tmuxio.capture_pane(sid)) is not None}


def _watch() -> None:
    while True:
        try:
            if not is_paused():
                gated = tmuxio.pending_ids()
                for sid in gated:
                    level = get(sid)
                    if level == "manual":
                        continue
                    p = tmuxio.pending(sid)
                    if not p:
                        continue
                    sig = _sig(p)
                    if _just_answered(sid, sig) or _too_many_tries(sid, sig):
                        continue
                    choice = decide(level, p)
                    if choice is None:
                        continue             # auto-safe escalation — human handles
                    res = tmuxio.answer(sid, choice)
                    if not res.get("ok"):
                        # Leave it un-recorded so the next poll tries again.
                        _note_failure(sid, sig, level, choice, res.get("error", ""))
                        continue
                    _fails.pop(sid, None)
                    _mark_answered(sid, sig)
                    print(f"[autonomy] {sid[:8]} {level} → auto-answered option {choice}")
                    if _hook:
                        try:
                            _hook(sid, level, choice, p)
                        except Exception as e:
                            print(f"[autonomy] hook failed: {e}")
                # Antigravity (agy) gates use a key chord (ctrl+k approve), not a
                # numbered menu, so they aren't in pending_ids — handle separately.
                agy_gated = _agy_pending_ids()
                for sid in agy_gated:
                    level = get(sid)
                    if level == "manual":
                        continue
                    p = agyparser.parse_gate(tmuxio.capture_pane(sid))
                    if not p:
                        continue
                    sig = _sig(p)
                    if _just_answered(sid, sig) or _too_many_tries(sid, sig):
                        continue
                    choice = decide(level, p)
                    if choice is None:
                        continue             # auto-safe escalation — human handles
                    res = tmuxio.agy_answer(sid, "approve")
                    if not res.get("ok"):
                        _note_failure(sid, sig, level, choice, res.get("error", ""))
                        continue
                    _fails.pop(sid, None)
                    _mark_answered(sid, sig)
                    print(f"[autonomy] {sid[:8]} {level} → auto-approved agy gate")
                    if _hook:
                        try:
                            _hook(sid, level, choice, p)
                        except Exception as e:
                            print(f"[autonomy] hook failed: {e}")
                # Forget gates that have cleared, so a re-gate is acted on afresh.
                cleared = gated | agy_gated
                for sid in list(_answered):
                    if sid not in cleared:
                        _answered.pop(sid, None)
                # Same for the retry counters — a session that isn't gated any
                # more gets its full allowance back on the next gate.
                for sid in list(_fails):
                    if sid not in cleared:
                        _fails.pop(sid, None)
        except Exception as e:
            print(f"[autonomy] watch error: {e}")
        time.sleep(POLL_SECS)


def start_watcher() -> None:
    """Start the auto-approver thread (idempotent)."""
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_watch, daemon=True, name="autonomy-watch").start()
    print(f"[autonomy] watcher started (poll {POLL_SECS}s, "
          f"{'PAUSED' if is_paused() else 'active'})")
