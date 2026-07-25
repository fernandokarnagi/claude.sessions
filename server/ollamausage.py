"""
ollamausage.py — cloud quota for Ollama-hosted models.

Sessions launched against an Ollama *cloud* model (`glm-5.2:cloud`,
`gpt-oss:20b-cloud`, …) run through ollama.com, so Claude Code's own `/usage`
panel says nothing useful about what's left. ollama.com answers that at
`GET /api/usage`, and the ollama CLI's own request signing is what gets us in:

    Authorization: <base64 pubkey material>:<base64 ed25519 signature>
    signed string: "<METHOD>,<path>?ts=<unix seconds>"

using ~/.ollama/id_ed25519 — the same key the local ollama daemon signs with, so
this reads the account already signed in on this machine and nothing else.

Requests go out through `curl` rather than urllib on purpose: this machine sits
behind a TLS-intercepting proxy whose root lives in the macOS keychain, which
curl honours and Python's certifi bundle does not.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time

from cryptography.hazmat.primitives.serialization import load_ssh_private_key

BASE = os.environ.get("OLLAMA_CLOUD_HOST", "https://ollama.com")
KEY_PATH = os.path.expanduser("~/.ollama/id_ed25519")
PUB_PATH = KEY_PATH + ".pub"
TIMEOUT = 20


def is_cloud_model(name: str | None) -> bool:
    """True for an Ollama-hosted model launch name (`:cloud` / `-cloud`)."""
    n = (name or "").strip().lower()
    return n.endswith(":cloud") or n.endswith("-cloud")


def _sign(payload: str) -> str:
    with open(KEY_PATH, "rb") as fh:
        key = load_ssh_private_key(fh.read(), password=None)
    with open(PUB_PATH, "r", encoding="utf-8") as fh:
        blob = fh.read().split()[1]          # drop the "ssh-ed25519 " prefix
    sig = base64.b64encode(key.sign(payload.encode())).decode()
    return f"{blob}:{sig}"


def _get(path: str) -> dict:
    """Signed GET against ollama.com. Returns the decoded JSON body."""
    ts = str(int(time.time()))
    url = f"{BASE}{path}?ts={ts}"
    proc = subprocess.run(
        ["curl", "-s", "--noproxy", "*", "--max-time", str(TIMEOUT),
         "-w", "\n%{http_code}",
         "-H", f"Authorization: {_sign(f'GET,{path}?ts={ts}')}",
         "-H", "Content-Type: application/json",
         "-H", "User-Agent: ollama-dashboard",
         url],
        capture_output=True, text=True, timeout=TIMEOUT + 10)
    body, _, code = proc.stdout.rpartition("\n")
    if code.strip() != "200":
        raise RuntimeError(f"ollama.com {path} returned HTTP {code.strip() or '?'}")
    return json.loads(body)


def _plan() -> str:
    """The account's plan name ("pro", "free", …), or "" if it can't be read.

    /api/me also carries the account email; only `plan` is ever taken from it —
    nothing identifying leaves this function.
    """
    try:
        ts = str(int(time.time()))
        proc = subprocess.run(
            ["curl", "-s", "--noproxy", "*", "--max-time", str(TIMEOUT), "-d", "",
             "-H", f"Authorization: {_sign(f'POST,/api/me?ts={ts}')}",
             "-H", "Content-Type: application/json",
             f"{BASE}/api/me?ts={ts}"],
            capture_output=True, text=True, timeout=TIMEOUT + 10)
        return str(json.loads(proc.stdout).get("Plan") or "")
    except Exception:                                  # plan is decoration only
        return ""


def _bar(frac: float, width: int = 28) -> str:
    filled = max(0, min(width, round(frac * width)))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def _window(title: str, block: dict) -> list[str]:
    frac = float(block.get("usage") or 0.0)
    lines = [f"{title:<9}{_bar(frac)}  {frac * 100:.1f}% used"]
    for m in block.get("models") or []:
        lines.append(f"    {m.get('name', '?'):<22} {m.get('request_count', 0):>6} requests")
    return lines


def render(data: dict, plan: str = "") -> str:
    """The usage payload as the fixed-width panel the Usage modal shows."""
    head = "Ollama cloud usage" + (f" — {plan.capitalize()}" if plan else "")
    out = [head, "=" * len(head), ""]

    limits = data.get("limits") or {}
    for key, title in (("session", "Session"), ("weekly", "Weekly")):
        block = limits.get(key)
        if isinstance(block, dict):
            out += _window(title, block) + [""]

    act = data.get("activity") or {}
    period = act.get("period") or {}
    if act:
        span = f"{(period.get('starting_at') or '')[:10]} → {(period.get('ending_at') or '')[:10]}"
        out.append(f"Activity ({period.get('type', 'period')}: {span})")
        out.append(f"    cost {act.get('cost', '0')}")
        for m in act.get("models") or []:
            out.append(f"    {m.get('name', '?'):<22} {m.get('request_count', 0):>6} requests")
        out.append("")

    out.append("Percentages are of your plan's limit for that window.")
    return "\n".join(out)


def usage() -> dict:
    """Cloud quota for the signed-in ollama.com account, shaped like the other
    /usage providers: {ok, text} on success, {ok: False, error} otherwise."""
    if not os.path.exists(KEY_PATH):
        return {"ok": False, "error": "no ollama key at ~/.ollama/id_ed25519 — run `ollama signin`"}
    try:
        data = _get("/api/usage")
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ollama.com timed out"}
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "error": f"ollama usage failed: {exc}"}
    return {"ok": True, "source": "ollama", "text": render(data, _plan()), "data": data}
