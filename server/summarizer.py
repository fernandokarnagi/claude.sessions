"""
summarizer.py — generate a one-paragraph "what's expected from you" summary.

Runs the user's configured model via `claude --print --output-format json`
on the session's last assistant message. The throwaway run is isolated to a
dedicated cwd and its transcript is deleted afterwards, so it never appears in
the dashboard's session list.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import shutil
from typing import Optional

from . import parser

CLAUDE_BIN = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
GEN_TIMEOUT = 90  # seconds
MAX_INPUT_CHARS = 4000

PROMPT = """You are summarizing a paused AI coding session for the user who \
stepped away. Below is the assistant's most recent message, after which it \
stopped and is now waiting for the user. In ONE concise paragraph, with no \
preamble, heading, or quotes, tell the user what response or decision the \
assistant needs from them to continue. If it asked specific questions or \
offered options, fold them in briefly. Write in second person ("you").

Assistant's last message:
\"\"\"
{msg}
\"\"\""""


def _delete_throwaway(session_id: str) -> None:
    for p in glob.glob(os.path.expanduser(f"~/.claude/projects/*/{session_id}.jsonl")):
        try:
            os.remove(p)
        except OSError:
            pass


async def generate(last_assistant_text: str) -> Optional[str]:
    """Return a one-paragraph summary, or None on failure."""
    text = (last_assistant_text or "").strip()
    if not text:
        return None
    os.makedirs(parser.SUMMARIZER_CWD, exist_ok=True)

    prompt = PROMPT.format(msg=text[:MAX_INPUT_CHARS])
    cmd = [CLAUDE_BIN, "--print", "--output-format", "json", prompt]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=parser.SUMMARIZER_CWD,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=GEN_TIMEOUT)
    except (FileNotFoundError, asyncio.TimeoutError):
        return None

    raw = out.decode("utf-8", "replace").strip()
    if not raw:
        return None

    # `--output-format json` returns a single result object with result+session_id
    try:
        data = json.loads(raw)
        summary = (data.get("result") or "").strip()
        sid = data.get("session_id")
        if sid:
            _delete_throwaway(sid)
    except json.JSONDecodeError:
        summary = raw  # fall back to raw text if not JSON

    return summary or None
