"""
ledger.py — what the fleet's token counts cost.

`prices.py` owns the rate table; this module owns the arithmetic over it and
the caches that keep a board poll cheap.

Three views, in ascending order of cost to compute:

    for_session(summary)   one session's dollars, from token counts the
                           summary already carries. No file work.
    rollup(sessions)       totals by project and by model, plus burn rate.
                           Pure arithmetic over summaries.
    by_day(sessions, days) dollars bucketed by calendar date, which needs a
                           pass over each transcript's usage blocks.

Transcripts are never modified — every function here only reads.
"""

from __future__ import annotations

import glob
import os
from collections import defaultdict
from datetime import datetime, timezone

from . import parser
from . import prices

# ---- Caches ------------------------------------------------------------------

# session_id -> (mtime, cost dict). A board poll over 250 sessions is 250
# dictionary lookups: the token counts come from the summary, which parser
# already caches on the same mtime.
_session_cache: dict[str, tuple[float, dict]] = {}

# transcript path -> (mtime, size, [day rows]). Only the cost page asks for
# these, and only for sessions inside its window.
_daily_cache: dict[str, tuple[float, int, list]] = {}

ZERO = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "total": 0}


def _tokens(d) -> dict:
    """Token counts as a plain dict, whatever shape they arrive in."""
    if not isinstance(d, dict):
        return dict(ZERO)
    out = {k: int(d.get(k) or 0) for k in
           ("input", "output", "cache_read", "cache_creation")}
    out["total"] = sum(out.values())
    return out


def _add(into: dict, more: dict) -> None:
    for k, v in more.items():
        into[k] = into.get(k, 0) + v


# ---- One session -------------------------------------------------------------

def for_session(summary: dict) -> dict:
    """Cost for one session, from the token counts in its summary.

    Returns prices.cost()'s shape: usd, priced, billing, per_bucket. `priced`
    is False for subscription, local and unrecognised models — those show
    tokens and no dollars, because any dollar figure would be invented.
    """
    if not isinstance(summary, dict):
        return prices.cost(None, ZERO)
    sid = summary.get("session_id") or ""
    mtime = summary.get("mtime") or 0.0
    cached = _session_cache.get(sid)
    if cached and cached[0] == mtime:
        return dict(cached[1])
    out = prices.cost(summary.get("model"), _tokens(summary.get("tokens")))
    if sid:
        _session_cache[sid] = (mtime, out)
    return dict(out)


# ---- Fleet totals ------------------------------------------------------------

def _hours(summary: dict) -> float:
    """Wall clock the session was open, in hours: first event to last write."""
    start = parser._epoch(summary.get("created_at"))
    end = summary.get("mtime") or parser._epoch(summary.get("updated_at"))
    if not start or not end or end <= start:
        return 0.0
    return (end - start) / 3600.0


def rollup(sessions: list) -> dict:
    """Totals by project and by model over a list of session summaries.

    Every group carries both dollars and tokens, plus `priced` — False when
    any session in the group is billed by subscription, runs locally, or has
    an unrecognised model, so the UI knows its dollar figure is partial.

    `burn` is dollars per hour of session wall clock, not of elapsed calendar
    time: sessions overlap, so calendar time would understate it.
    """
    by_project: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_billing: dict[str, dict] = defaultdict(lambda: {"usd": 0.0, "sessions": 0})
    total_usd = 0.0
    total_tokens = dict(ZERO)
    total_hours = 0.0
    unpriced = 0

    for s in sessions or []:
        c = for_session(s)
        toks = _tokens(s.get("tokens"))
        usd = float(c.get("usd") or 0.0)
        priced = bool(c.get("priced"))
        if not priced:
            unpriced += 1

        for key, bucket in (
                (s.get("project") or s.get("cwd") or "(unknown)", by_project),
                (s.get("model") or "(unknown)", by_model)):
            row = bucket.setdefault(key, {
                "key": key, "usd": 0.0, "sessions": 0,
                "tokens": dict(ZERO), "priced": True,
            })
            row["usd"] += usd
            row["sessions"] += 1
            _add(row["tokens"], toks)
            if not priced:
                row["priced"] = False

        b = by_billing[c.get("billing") or prices.UNKNOWN]
        b["usd"] += usd
        b["sessions"] += 1

        total_usd += usd
        _add(total_tokens, toks)
        total_hours += _hours(s)

    def ordered(bucket: dict) -> list:
        return sorted(bucket.values(),
                      key=lambda r: (-r["usd"], -r["tokens"]["total"], r["key"]))

    return {
        "by_project": ordered(by_project),
        "by_model": ordered(by_model),
        "by_billing": [dict(billing=k, **v) for k, v in sorted(by_billing.items())],
        "totals": {
            "usd": total_usd,
            "tokens": total_tokens,
            "sessions": len(sessions or []),
            "unpriced_sessions": unpriced,
            "hours": total_hours,
        },
        "burn": (total_usd / total_hours) if total_hours else 0.0,
    }


# ---- By calendar day ---------------------------------------------------------

def path_for(session_id: str) -> str | None:
    """The transcript file behind a session id, or None when it isn't ours."""
    if not session_id:
        return None
    hits = glob.glob(os.path.join(parser.PROJECTS_DIR, "*", f"{session_id}.jsonl"))
    return hits[0] if hits else None


def _date_of(ts) -> str | None:
    epoch = parser._epoch(ts)
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


def daily(path: str) -> list:
    """Tokens and dollars per calendar date for one transcript.

    One pass over the file's usage blocks; each block carries its own
    timestamp and its own model, so a session that switched models mid-run is
    priced correctly on each side of the switch. Cached on (mtime, size).
    """
    try:
        st = os.stat(path)
    except OSError:
        return []
    cached = _daily_cache.get(path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return [dict(r) for r in cached[2]]

    # (date, model) -> tokens
    grid: dict[tuple, dict] = {}
    last_date = None
    for evt in parser._iter_events(path):
        date = _date_of(evt.get("timestamp")) or last_date
        if date:
            last_date = date
        msg = evt.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict) or not date:
            continue
        model = msg.get("model")
        if model == "<synthetic>":
            model = None
        key = (date, model)
        row = grid.setdefault(key, dict(ZERO))
        row["input"] += usage.get("input_tokens", 0) or 0
        row["output"] += usage.get("output_tokens", 0) or 0
        row["cache_read"] += usage.get("cache_read_input_tokens", 0) or 0
        row["cache_creation"] += usage.get("cache_creation_input_tokens", 0) or 0
        row["total"] = (row["input"] + row["output"]
                        + row["cache_read"] + row["cache_creation"])

    days: dict[str, dict] = {}
    for (date, model), toks in grid.items():
        c = prices.cost(model, toks)
        row = days.setdefault(date, {
            "date": date, "usd": 0.0, "tokens": dict(ZERO), "priced": True,
        })
        row["usd"] += float(c.get("usd") or 0.0)
        _add(row["tokens"], toks)
        if not c.get("priced"):
            row["priced"] = False

    out = sorted(days.values(), key=lambda r: r["date"])
    _daily_cache[path] = (st.st_mtime, st.st_size, out)
    return [dict(r) for r in out]


def by_day(sessions: list, days: int = 30) -> list:
    """Fleet spend per calendar date over the last `days` days.

    Only sessions written inside the window are opened; a session that has not
    moved in months cannot have spent anything inside it. Sessions whose
    transcript is gone (agy, grok, opencode) are attributed whole to the date
    of their last activity — coarse, but it is the only date they carry.
    """
    cutoff = None
    if days and days > 0:
        cutoff = (datetime.now(tz=timezone.utc).timestamp() - days * 86400)
    out: dict[str, dict] = {}

    def row(date: str) -> dict:
        return out.setdefault(date, {
            "date": date, "usd": 0.0, "tokens": dict(ZERO), "priced": True,
        })

    for s in sessions or []:
        mtime = s.get("mtime") or parser._epoch(s.get("updated_at")) or 0.0
        if cutoff and mtime < cutoff:
            continue
        path = path_for(s.get("session_id") or "")
        if path:
            rows = daily(path)
        else:
            c = for_session(s)
            date = _date_of(s.get("updated_at")) or _date_of(
                datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat())
            if not date:
                continue
            rows = [{"date": date, "usd": float(c.get("usd") or 0.0),
                     "tokens": _tokens(s.get("tokens")),
                     "priced": bool(c.get("priced"))}]
        for r in rows:
            if cutoff and parser._epoch(r["date"] + "T23:59:59+00:00") < cutoff:
                continue
            d = row(r["date"])
            d["usd"] += r["usd"]
            _add(d["tokens"], r["tokens"])
            if not r["priced"]:
                d["priced"] = False

    return sorted(out.values(), key=lambda r: r["date"])
