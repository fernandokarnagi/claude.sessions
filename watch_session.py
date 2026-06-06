#!/usr/bin/env python3
"""
watch_session.py — live viewer for a running Claude Code session.

Claude Code writes every session to disk as JSONL at:
    ~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl

This script tails the active transcript and pretty-prints events as they
are written, giving you a read-only live view of a running session.

Usage:
    python3 watch_session.py                 # follow the most recent session
    python3 watch_session.py --list          # list recent sessions, then exit
    python3 watch_session.py <file.jsonl>     # follow a specific transcript
    python3 watch_session.py --session <uuid> # follow by session id
    python3 watch_session.py --all            # print existing history, then follow
"""

import argparse
import glob
import json
import os
import sys
import time

# Override the transcript location with the CLAUDE_PROJECTS_DIR environment
# variable; defaults to ~/.claude/projects.
PROJECTS = os.path.expanduser(
    os.environ.get("CLAUDE_PROJECTS_DIR", "~/.claude/projects")
)

# ANSI colors (auto-disabled when not a TTY)
C = {
    "user": "\033[36m",      # cyan
    "assistant": "\033[32m", # green
    "thinking": "\033[90m",  # grey
    "tool": "\033[33m",      # yellow
    "result": "\033[35m",    # magenta
    "dim": "\033[2m",
    "reset": "\033[0m",
}
if not sys.stdout.isatty():
    C = {k: "" for k in C}


def list_sessions(limit=15):
    files = sorted(
        glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")),
        key=os.path.getmtime,
        reverse=True,
    )
    for f in files[:limit]:
        mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(f)))
        proj = os.path.basename(os.path.dirname(f)).lstrip("-").replace("-", "/")
        print(f"{C['dim']}{mtime}{C['reset']}  {os.path.basename(f)[:8]}  {proj}")
    return files


def newest_session():
    files = glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))
    if not files:
        sys.exit(f"No session transcripts found under {PROJECTS}")
    return max(files, key=os.path.getmtime)


def find_by_session(uuid):
    matches = glob.glob(os.path.join(PROJECTS, "*", f"{uuid}*.jsonl"))
    if not matches:
        sys.exit(f"No transcript found for session {uuid}")
    return matches[0]


def render(evt):
    """Turn one JSONL event into a printable string, or None to skip."""
    msg = evt.get("message")
    if not isinstance(msg, dict):
        return None  # skip meta events (mode changes, summaries, etc.)

    role = msg.get("role")
    content = msg.get("content")
    ts = (evt.get("timestamp") or "")[11:19]
    out = []

    # user / assistant content can be a plain string or a list of blocks
    if isinstance(content, str):
        out.append(f"{C[role]}{ts} {role.upper()}{C['reset']}: {content}")
    elif isinstance(content, list):
        for b in content:
            btype = b.get("type")
            if btype == "text":
                out.append(f"{C['assistant']}{ts} ASSISTANT{C['reset']}: {b['text']}")
            elif btype == "thinking":
                txt = b.get("thinking", "").strip().replace("\n", " ")
                out.append(f"{C['thinking']}{ts} (thinking) {txt[:200]}{C['reset']}")
            elif btype == "tool_use":
                inp = json.dumps(b.get("input", {}))[:160]
                out.append(f"{C['tool']}{ts} → TOOL {b.get('name')}{C['reset']}: {inp}")
            elif btype == "tool_result":
                res = b.get("content")
                if isinstance(res, list):
                    res = " ".join(
                        x.get("text", "") for x in res if isinstance(x, dict)
                    )
                res = str(res).strip().replace("\n", " ")
                out.append(f"{C['result']}{ts} ← RESULT{C['reset']}: {res[:160]}")
    return "\n".join(out) if out else None


def follow(path, from_start=False):
    proj = os.path.basename(os.path.dirname(path)).lstrip("-").replace("-", "/")
    print(f"{C['dim']}Watching {os.path.basename(path)[:8]} — {proj}{C['reset']}")
    print(f"{C['dim']}(Ctrl-C to stop){C['reset']}\n")

    with open(path, "r", encoding="utf-8") as fh:
        if not from_start:
            fh.seek(0, os.SEEK_END)  # start at the end → only new events
        while True:
            line = fh.readline()
            if not line:
                time.sleep(0.4)
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = render(evt)
            if text:
                print(text)


def main():
    ap = argparse.ArgumentParser(description="Live viewer for a Claude Code session.")
    ap.add_argument("file", nargs="?", help="path to a .jsonl transcript")
    ap.add_argument("--session", help="follow by session id (uuid prefix)")
    ap.add_argument("--list", action="store_true", help="list recent sessions and exit")
    ap.add_argument("--all", action="store_true", help="print full history before following")
    args = ap.parse_args()

    if args.list:
        list_sessions()
        return

    if args.file:
        path = args.file
    elif args.session:
        path = find_by_session(args.session)
    else:
        path = newest_session()

    try:
        follow(path, from_start=args.all)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
