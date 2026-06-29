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

from . import tmuxio

_PATH = os.path.join(os.path.dirname(__file__), ".autonomy.json")
_lock = threading.Lock()

LEVELS = ("manual", "auto-safe", "yolo")
DEFAULT = "manual"

# Runtime global pause (in addition to the AUTONOMY_DISABLED env switch).
_paused = False
_started = False

# Hook(sid, level, choice, prompt) called best-effort after an auto-answer.
_hook: Optional[Callable[[str, str, int, dict], None]] = None

# Last gate signature we auto-answered per session, so we don't re-answer the
# same prompt while it lingers on screen for a poll or two.
_answered: dict[str, str] = {}

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
    return prompt.get("question", "") + "|" + "|".join(
        f"{o.get('num')}{o.get('label')}" for o in (prompt.get("options") or []))


# --- watcher ----------------------------------------------------------------

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
                    if _answered.get(sid) == sig:
                        continue
                    choice = decide(level, p)
                    if choice is None:
                        continue             # auto-safe escalation — human handles
                    tmuxio.answer(sid, choice)
                    _answered[sid] = sig
                    print(f"[autonomy] {sid[:8]} {level} → auto-answered option {choice}")
                    if _hook:
                        try:
                            _hook(sid, level, choice, p)
                        except Exception as e:
                            print(f"[autonomy] hook failed: {e}")
                # Forget gates that have cleared, so a re-gate is acted on afresh.
                for sid in list(_answered):
                    if sid not in gated:
                        _answered.pop(sid, None)
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
