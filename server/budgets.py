"""
budgets.py — spend caps, per session and across the fleet.

A cap is a dollar ceiling. The autonomy watcher checks it every pass: a
session over its cap is forced back to `manual`, so it stops answering its own
prompts and waits for a person; the fleet over its cap pauses autonomy
outright. Nothing is ever killed — the work is still there, it just stops
spending on its own.

Only metered models can breach a cap. A subscription or local model has no
dollar figure (see prices.py), and a cap against an invented number would fire
at random, so those sessions are never over.

Shape of .budgets.json:
    {
      "caps": {"<session_id>": 12.5},
      "fleet_cap": 200.0,
      "enforced": {"<session_id>": "<iso when the cap was acted on>"},
      "fleet_enforced_at": "<iso>|null"
    }

Kill switch: BUDGETS_DISABLED=1 makes enabled() False. Caps stay on file and
still show in the UI; nothing acts on them.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

_PATH = os.path.join(os.path.dirname(__file__), ".budgets.json")
_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    try:
        with open(_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("caps", {})
    data.setdefault("fleet_cap", None)
    data.setdefault("enforced", {})
    data.setdefault("fleet_enforced_at", None)
    return data


def _save(data: dict) -> None:
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, _PATH)


def _amount(usd) -> float | None:
    """A cap as a positive float, or None for 'no cap'. Junk reads as no cap
    rather than as zero — a zero cap would silence the whole fleet."""
    if usd is None or usd == "":
        return None
    try:
        v = float(usd)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def enabled() -> bool:
    """False when the operator has switched enforcement off for this process."""
    return os.environ.get("BUDGETS_DISABLED", "") not in ("1", "true", "yes")


# ---- per session -------------------------------------------------------------

def get(session_id: str) -> float | None:
    with _lock:
        return _amount(_load()["caps"].get(session_id))


def set_cap(session_id: str, usd) -> float | None:
    """Set (or, with a falsy amount, clear) one session's cap. Setting a cap
    also clears the enforced mark: a raised cap must be able to act again."""
    amount = _amount(usd)
    with _lock:
        data = _load()
        if amount is None:
            data["caps"].pop(session_id, None)
        else:
            data["caps"][session_id] = amount
        data["enforced"].pop(session_id, None)
        _save(data)
    return amount


def clear(session_id: str) -> None:
    set_cap(session_id, None)


def all_caps() -> dict[str, float]:
    """{session_id: cap} in one file read, for the board."""
    with _lock:
        caps = _load()["caps"]
    return {sid: _amount(v) for sid, v in caps.items() if _amount(v) is not None}


# ---- fleet -------------------------------------------------------------------

def fleet_cap() -> float | None:
    with _lock:
        return _amount(_load().get("fleet_cap"))


def set_fleet_cap(usd) -> float | None:
    amount = _amount(usd)
    with _lock:
        data = _load()
        data["fleet_cap"] = amount
        data["fleet_enforced_at"] = None
        _save(data)
    return amount


# ---- breach ------------------------------------------------------------------

def over(summary: dict, cost: dict | None = None, cap: float | None = None) -> dict:
    """Where one session stands against its cap.

    `cost` and `cap` can be passed in when the caller already has them — the
    watcher holds both for every session on each pass, and a file read per
    session per pass would be the expensive part.

    Returns {cap, usd, over, priced}. `over` is only ever True for a metered
    session with a cap it has passed.
    """
    from . import ledger      # local: ledger imports prices, budgets imports neither

    sid = (summary or {}).get("session_id") or ""
    c = cost if cost is not None else ledger.for_session(summary or {})
    cap = cap if cap is not None else get(sid)
    usd = float(c.get("usd") or 0.0)
    priced = bool(c.get("priced"))
    return {
        "cap": cap,
        "usd": usd,
        "over": bool(cap and priced and usd >= cap),
        "priced": priced,
    }


def fleet_over(total_usd: float) -> bool:
    cap = fleet_cap()
    return bool(cap and total_usd >= cap)


# ---- enforcement marks -------------------------------------------------------
#
# The watcher runs every few seconds; without a mark it would force the same
# session to manual — and send the same Slack line — on every pass.

def was_enforced(session_id: str) -> bool:
    with _lock:
        return bool(_load()["enforced"].get(session_id))


def mark_enforced(session_id: str) -> None:
    with _lock:
        data = _load()
        if data["enforced"].get(session_id):
            return
        data["enforced"][session_id] = _now()
        _save(data)


def fleet_enforced() -> bool:
    with _lock:
        return bool(_load().get("fleet_enforced_at"))


def mark_fleet_enforced(on: bool = True) -> None:
    with _lock:
        data = _load()
        want = _now() if on else None
        if bool(data.get("fleet_enforced_at")) == bool(want):
            return
        data["fleet_enforced_at"] = want
        _save(data)


def state() -> dict:
    """Everything the budget UI needs in one read."""
    with _lock:
        data = _load()
    return {
        "enabled": enabled(),
        "fleet_cap": _amount(data.get("fleet_cap")),
        "fleet_enforced": bool(data.get("fleet_enforced_at")),
        "caps": {sid: _amount(v) for sid, v in data["caps"].items()
                 if _amount(v) is not None},
        "enforced": dict(data.get("enforced") or {}),
    }


def rekey(old_id: str, new_id: str) -> None:
    """Carry a cap onto a new session id (see tmuxio.reset). The spend resets
    with the transcript, so the enforced mark does not travel."""
    if old_id == new_id:
        return
    with _lock:
        data = _load()
        cap = data["caps"].pop(old_id, None)
        had = data["enforced"].pop(old_id, None)
        if cap is None and had is None:
            return
        if cap is not None:
            data["caps"][new_id] = cap
        _save(data)
