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
from datetime import datetime, timezone

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


# ---------------------------------------------------------------------------
# Reset times.
#
# ollama.com doesn't publish them: /api/usage carries no reset field, nothing
# useful comes back in the headers, and the page that renders "Resets in 2
# hours" is cookie-authed web, not this API. So we learn them by watching.
#
# Every reading is appended to .ollama_usage.json. When a window's usage (or its
# request count) drops between two readings, that window reset somewhere in
# between — that's an observed boundary, known to within the gap between the two
# readings. Two boundaries give the window length, which gives the next reset.
# Readings only happen when you open the Usage panel, so early boundaries are
# coarse and tighten as more are seen.
# ---------------------------------------------------------------------------

STATE_PATH = os.path.join(os.path.dirname(__file__), ".ollama_usage.json")
_MAX_SAMPLES = 400
_MAX_BOUNDARIES = 40
_WINDOWS = ("session", "weekly")


def _now() -> float:
    return time.time()


def _load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}
    state.setdefault("samples", [])
    state.setdefault("boundaries", {w: [] for w in _WINDOWS})
    for w in _WINDOWS:
        state["boundaries"].setdefault(w, [])
    return state


def _save_state(state: dict) -> None:
    state["samples"] = state["samples"][-_MAX_SAMPLES:]
    for w in _WINDOWS:
        state["boundaries"][w] = state["boundaries"][w][-_MAX_BOUNDARIES:]
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, STATE_PATH)


def _reading(data: dict) -> dict:
    """One window -> {usage, reqs} snapshot, the two things a reset knocks down."""
    limits = data.get("limits") or {}
    out = {}
    for w in _WINDOWS:
        block = limits.get(w)
        if isinstance(block, dict):
            out[w] = {
                "usage": float(block.get("usage") or 0.0),
                "reqs": sum(int(m.get("request_count") or 0)
                            for m in (block.get("models") or [])),
            }
    return out


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _week_grid(data: dict) -> float | None:
    """Next week boundary off the API's own week grid.

    activity.period.starting_at is where ollama.com starts counting weeks (it
    lands on Monday 00:00 UTC), so stepping it forward in 7-day hops gives the
    weekly boundary without guessing anything.
    """
    start = ((data.get("activity") or {}).get("period") or {}).get("starting_at")
    if not isinstance(start, str) or not start:
        return None
    try:
        t0 = datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    week = 7 * 86400
    now = _now()
    return t0 + week * (int((now - t0) // week) + 1)


def observe(data: dict) -> dict:
    """Record this reading and report what's known about each window's reset.

    Per window: {next, window_s, last_reset, uncertainty_s, observed, samples}.
    `next` is None while there's nothing solid to say.
    """
    state = _load_state()
    now = _now()
    cur = _reading(data)
    prev = state["samples"][-1] if state["samples"] else None

    # A drop in either signal means the window rolled over between the two
    # readings; call the midpoint the boundary and carry the gap as its error.
    if prev:
        for w, vals in cur.items():
            before = (prev.get("windows") or {}).get(w)
            if not before:
                continue
            if vals["usage"] < before["usage"] - 1e-9 or vals["reqs"] < before["reqs"]:
                state["boundaries"][w].append(
                    {"at": (prev["at"] + now) / 2, "uncertainty_s": now - prev["at"]})

    state["samples"].append({"at": now, "windows": cur})
    _save_state(state)

    out = {}
    for w in _WINDOWS:
        marks = state["boundaries"][w]
        gaps = [b["at"] - a["at"] for a, b in zip(marks, marks[1:])]
        length = _median(gaps) if gaps else None
        nxt = None
        source = "learning"
        if length and marks:
            # Roll the last boundary forward to the first one still ahead of us.
            last = marks[-1]["at"]
            nxt = last + length * (int((now - last) // length) + 1)
            source = "learned"
        elif w == "weekly":
            nxt = _week_grid(data)
            if nxt:
                source = "week grid"
        out[w] = {
            "next": nxt,
            "source": source,
            "window_s": length,
            "last_reset": marks[-1]["at"] if marks else None,
            "uncertainty_s": marks[-1]["uncertainty_s"] if marks else None,
            "observed": len(marks),
            "samples": len(state["samples"]),
        }
    return out


def _dur(seconds: float) -> str:
    """"2h 41m" / "1d 6h" / "40m" — coarse, because that's all it's worth."""
    s = max(0, int(seconds))
    if s >= 86400:
        return f"{s // 86400}d {(s % 86400) // 3600}h"
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{max(1, s // 60)}m"


def _reset_line(info: dict) -> str:
    """The "resets in …" line under a window's bar."""
    if not info or not info.get("next"):
        n = info.get("samples", 0) if info else 0
        return (f"         reset unknown — learning from readings "
                f"({n} so far; the first observed reset pins it)")
    left = info["next"] - _now()
    when = datetime.fromtimestamp(info["next"], tz=timezone.utc).astimezone()
    note = (f"learned from {info['observed']} reset"
            f"{'' if info['observed'] == 1 else 's'}, "
            f"~{_dur(info['window_s'])} window"
            if info["source"] == "learned" else "from the API's week grid")
    return f"         resets in {_dur(left)} — {when:%a %d %b %H:%M %Z} ({note})"


def _bar(frac: float, width: int = 28) -> str:
    filled = max(0, min(width, round(frac * width)))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def _window(title: str, block: dict, reset: dict | None) -> list[str]:
    frac = float(block.get("usage") or 0.0)
    lines = [f"{title:<9}{_bar(frac)}  {frac * 100:.1f}% used", _reset_line(reset or {})]
    for m in block.get("models") or []:
        lines.append(f"    {m.get('name', '?'):<22} {m.get('request_count', 0):>6} requests")
    return lines


def render(data: dict, plan: str = "", resets: dict | None = None) -> str:
    """The usage payload as the fixed-width panel the Usage modal shows."""
    head = "Ollama cloud usage" + (f" — {plan.capitalize()}" if plan else "")
    out = [head, "=" * len(head), ""]

    limits = data.get("limits") or {}
    for key, title in (("session", "Session"), ("weekly", "Weekly")):
        block = limits.get(key)
        if isinstance(block, dict):
            out += _window(title, block, (resets or {}).get(key)) + [""]

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
    out.append("ollama.com publishes no reset time, so it's learned from readings.")
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
    try:
        resets = observe(data)
    except Exception:                                   # noqa: BLE001
        resets = {}                                     # panel still renders
    return {"ok": True, "source": "ollama", "data": data, "resets": resets,
            "text": render(data, _plan(), resets)}
