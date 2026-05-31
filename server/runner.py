"""
runner.py — drive a Claude Code session turn from the web via headless resume.

Each user message spawns a one-shot:
    claude --print "<msg>" --resume <id> --output-format stream-json --verbose
           --permission-mode <mode>
run with cwd set to the session's project directory. The process streams
JSON events (one per line) and exits when the turn completes. We relay those
events to the browser and mark the session as web-adopted.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from typing import AsyncIterator, Optional

from . import parser, registry

CLAUDE_BIN = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")

VALID_MODES = {"default", "acceptEdits", "plan", "bypassPermissions"}

# session_ids with a turn currently generating (for the live-on-web indicator)
_running: set[str] = set()


def running_ids() -> set[str]:
    return set(_running)


def parse_event_line(line: str) -> Optional[dict]:
    """Parse one stream-json output line; None if blank/unparseable."""
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


async def run_turn(
    session_id: str,
    text: str,
    cwd: Optional[str],
    permission_mode: str = "acceptEdits",
) -> AsyncIterator[dict]:
    """Send `text` to `session_id` and yield stream-json events as dicts.

    Always yields a final {"type": "done", ...} event, even on error.
    """
    if permission_mode not in VALID_MODES:
        permission_mode = "acceptEdits"

    cmd = [
        CLAUDE_BIN,
        "--print", text,
        "--resume", session_id,
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", permission_mode,
    ]

    _running.add(session_id)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd if cwd and os.path.isdir(cwd) else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        _running.discard(session_id)
        yield {"type": "error", "message": f"claude binary not found at {CLAUDE_BIN}"}
        yield {"type": "done", "ok": False}
        return

    # The session is now web-driven; recorded again at turn end (below) so the
    # mtime reference reflects our latest write — until the CLI writes after us.
    registry.set_web_mtime(session_id, parser.session_mtime(session_id))

    try:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            evt = parse_event_line(raw.decode("utf-8", "replace"))
            if evt is not None:
                yield evt
        await proc.wait()
        if proc.returncode not in (0, None):
            err = b""
            if proc.stderr is not None:
                err = await proc.stderr.read()
            yield {
                "type": "error",
                "message": err.decode("utf-8", "replace").strip()
                or f"claude exited with code {proc.returncode}",
            }
    finally:
        # Record the transcript mtime as of the end of our turn. Any later write
        # (e.g. the CLI resuming) will exceed this and flip the label to CLI.
        registry.set_web_mtime(session_id, parser.session_mtime(session_id))
        _running.discard(session_id)
        yield {"type": "done", "ok": True}
