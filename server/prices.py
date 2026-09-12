"""
prices.py — what a model costs, and whether it costs anything at all.

The fleet runs four kinds of model and only one of them has a price:

  metered       Anthropic API models, billed per token. Real dollars.
  subscription  ollama cloud (`:cloud`/`-cloud`) and free tiers (`:free`).
                A flat plan already paid for; per-token dollars would be a
                number nobody is charged.
  local         mlx / gguf / quantised models running on this machine.
  unknown       anything unrecognised.

Only `metered` produces a dollar figure. The other three report tokens and say
so — a guessed rate on a board full of local models is worse than a blank.

Rates are US dollars per million tokens, from Anthropic's published pricing.
Cache reads and writes follow the standard multipliers (write 1.25x input,
read 0.1x input) except where a model prices reads explicitly.

`server/.prices.json` is merged over the table at load, so a rate change is an
edit rather than a release. Its shape is the table's:

    {"claude-opus-5": {"input": 5.0, "output": 25.0,
                       "cache_read": 0.5, "cache_write": 6.25}}
"""

from __future__ import annotations

import json
import os
import re
import time

from . import models

PRICES_FILE = os.path.join(os.path.dirname(__file__), ".prices.json")

METERED = "metered"
SUBSCRIPTION = "subscription"
LOCAL = "local"
UNKNOWN = "unknown"


def _row(inp: float, out: float, read: float | None = None,
         write: float | None = None) -> dict:
    """A rate row. Cache rates default to the standard multipliers."""
    return {
        "input": inp,
        "output": out,
        "cache_read": inp * 0.1 if read is None else read,
        "cache_write": inp * 1.25 if write is None else write,
    }


# Keys are matched exactly first, then as a prefix — so the dated form
# `claude-haiku-4-5-20251001` that Claude Code writes into a transcript lands
# on the `claude-haiku-4-5` row without an entry of its own.
RATES: dict[str, dict] = {
    "claude-fable-5-1":  _row(10.0, 50.0, read=0.25),
    "claude-mythos-5-1": _row(10.0, 50.0, read=0.25),
    "claude-fable-5":    _row(10.0, 50.0),
    "claude-mythos-5":   _row(10.0, 50.0),
    "claude-opus-5":     _row(5.0, 25.0),
    "claude-opus-4-8":   _row(5.0, 25.0),
    "claude-opus-4-7":   _row(5.0, 25.0),
    "claude-opus-4-6":   _row(5.0, 25.0),
    "claude-sonnet-5":   _row(2.0, 10.0),
    "claude-sonnet-4-6": _row(3.0, 15.0),
    "claude-haiku-4-5":  _row(1.0, 5.0),
}

# Hosted behind a plan, not a meter.
_SUBSCRIPTION_SUFFIXES = (":cloud", "-cloud", ":free")

# Markers of a model running on this machine: the mlx and gguf runtimes, a
# llama.cpp quantisation tag (`Q4_K_M`, `Q8_0`, `UD-Q2_K_XL`), or an ollama
# size tag (`gemma4:12b`, `qwen3.6:35b-mlx`).
_LOCAL_RE = re.compile(
    r"(?:\bmlx\b|gguf|[-_.:]q\d(?:_[a-z0-9]+)*\b|:\d+(?:\.\d+)?b\b)",
    re.IGNORECASE)

_cache: dict = {"at": 0.0, "mtime": None, "table": None}


def _overrides() -> dict:
    """`.prices.json` contents, or {} when it is absent or unreadable."""
    try:
        mtime = os.path.getmtime(PRICES_FILE)
    except OSError:
        return {}
    if _cache["mtime"] == mtime and _cache["table"] is not None:
        return _cache["table"]
    try:
        with open(PRICES_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        table = data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        table = {}
    _cache["mtime"], _cache["table"], _cache["at"] = mtime, table, time.time()
    return table


def table() -> dict[str, dict]:
    """The built-in rates with any local overrides applied."""
    merged = dict(RATES)
    for model, row in _overrides().items():
        if isinstance(row, dict):
            merged[str(model).lower()] = {**merged.get(str(model).lower(), {}), **row}
    return merged


def rate(model: str | None) -> dict | None:
    """Per-MTok rates for a model, or None when it isn't a metered one.

    Exact match wins; otherwise the longest key that the model id starts with,
    which is what folds a dated snapshot id onto its family row.
    """
    if not model:
        return None
    m = model.strip().lower()
    rows = table()
    if m in rows:
        return rows[m]
    # `anthropic/claude-3-haiku` style ids carry a vendor prefix.
    if "/" in m and m.split("/", 1)[1] in rows:
        return rows[m.split("/", 1)[1]]
    hits = [k for k in rows if m.startswith(k)]
    if hits:
        return rows[max(hits, key=len)]
    return None


def _by_name(m: str) -> str | None:
    """Class inferred from the name alone, or None when the name says nothing."""
    if m.endswith(_SUBSCRIPTION_SUFFIXES):
        return SUBSCRIPTION
    if _LOCAL_RE.search(m):
        return LOCAL
    return None


def classify(model: str | None) -> str:
    """Which of the four billing classes a model falls in.

    An override entry may name the class outright — `{"kimi-k2.7-code":
    {"billing": "subscription"}}` — which is the answer for a model whose id
    carries no marker at all.

    Failing that, the launch registry settles it: a transcript records the
    resolved id (`glm-5.2`), while the name the session was launched with keeps
    the suffix that says where it ran (`glm-5.2:cloud`). `models.canonical`
    already maps one to the other for the model badge, so the same lookup
    classifies a bare id here.
    """
    if not model:
        return UNKNOWN
    m = model.strip().lower()
    override = _overrides().get(m) or _overrides().get(model.strip())
    if isinstance(override, dict) and override.get("billing") in (
            METERED, SUBSCRIPTION, LOCAL, UNKNOWN):
        return override["billing"]
    if rate(m):
        return METERED
    named = _by_name(m)
    if named:
        return named
    full = models.canonical(m)
    if full:
        named = _by_name(full.lower())
        if named:
            return named
    return UNKNOWN


def cost(model: str | None, tokens: dict | None) -> dict:
    """What a token count cost on a model.

    Returns {usd, priced, billing, per_bucket}. `priced` is False for every
    class but `metered`, and `usd` is then 0.0 — a caller that renders dollars
    should check `priced` and show the token count instead.
    """
    billing = classify(model)
    rates = rate(model)
    tokens = tokens or {}
    if not rates or billing != METERED:
        return {"usd": 0.0, "priced": False, "billing": billing, "per_bucket": {}}

    per_bucket = {}
    for bucket, key in (("input", "input"), ("output", "output"),
                        ("cache_read", "cache_read"),
                        ("cache_creation", "cache_write")):
        n = tokens.get(bucket) or 0
        per_bucket[bucket] = round(n * rates[key] / 1_000_000, 6)
    return {
        "usd": round(sum(per_bucket.values()), 6),
        "priced": True,
        "billing": METERED,
        "per_bucket": per_bucket,
    }
