"""
models.py — registry of known model launch names, learned from the local
runclaude_*.sh scripts (Ollama local + cloud models behind ANTHROPIC_BASE_URL).

Claude Code's transcript only records the *resolved* model id (a cloud launch
`glm-5.2:cloud` shows up in messages as `glm-5.2`; `gemma4:31b-cloud` as
`gemma4:31b`). To display the real launch name — and to tell a cloud model
(`:cloud` / `-cloud` suffix) apart from a local one (e.g. `gemma4:12b`) — we
read the launch scripts and map each resolved id back to its full name.

Override the scripts dir with CCOE_SCRIPTS_DIR; if it's absent, the registry is
simply empty and the parser falls back to the resolved id as-is.
"""

from __future__ import annotations

import glob
import os
import re
import time

SCRIPTS_DIR = os.environ.get(
    "CCOE_SCRIPTS_DIR", os.path.expanduser("~/App/ccoe"))

# `runclaude_base.sh "glm-5.2:cloud" "$SESSION_ID"` and `--model gemma4:12b`.
_ARG_RE = re.compile(r'runclaude_base\.sh"?\s+"([^"$][^"]*)"')
_MODEL_RE = re.compile(r'--model\s+"?([A-Za-z0-9._:\-]+)"?')

# Names that are Anthropic aliases, not Ollama models — skip.
_SKIP = {"opus", "sonnet", "haiku", "fable", "default"}

_cache: dict = {"at": 0.0, "reg": {}}


def _strip_cloud(full: str) -> str:
    """Resolved id = launch name minus its cloud marker (`:cloud` / `-cloud`)."""
    for suf in (":cloud", "-cloud"):
        if full.endswith(suf):
            return full[: -len(suf)]
    return full


def _extract(path: str) -> set[str]:
    names: set[str] = set()
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            txt = fh.read()
    except OSError:
        return names
    for m in _ARG_RE.finditer(txt):
        names.add(m.group(1))
    for m in _MODEL_RE.finditer(txt):
        v = m.group(1)
        if v and not v.startswith("$"):
            names.add(v)
    return names


def registry(ttl: float = 30.0) -> dict[str, str]:
    """Map resolved-id (lowercased) -> full launch name, cached for `ttl`s."""
    now = time.time()
    if now - float(_cache["at"]) < ttl and _cache["reg"]:
        return _cache["reg"]  # type: ignore[return-value]
    reg: dict[str, str] = {}
    for p in glob.glob(os.path.join(SCRIPTS_DIR, "runclaude_*.sh")):
        for full in _extract(p):
            if full.lower() in _SKIP:
                continue
            reg[_strip_cloud(full).lower()] = full
    _cache["at"], _cache["reg"] = now, reg
    return reg


def canonical(resolved: str | None) -> str | None:
    """Full launch name for a resolved model id, or None if unknown."""
    if not resolved:
        return None
    return registry().get(resolved.lower())


# Helper scripts that aren't a single-model launcher — hide from the picker.
_NOT_LAUNCHERS = {"base", "ollama"}


def launchers() -> list[dict]:
    """The runclaude_<model>.sh scripts, one per selectable model.

    Returns [{key, script, model}] sorted by key — `key` is the name after
    `runclaude_` (e.g. "kimi"), `script` the absolute path, `model` the launch
    model arg it passes (best-effort; may be empty if not parseable).
    """
    out = []
    for path in sorted(glob.glob(os.path.join(SCRIPTS_DIR, "runclaude_*.sh"))):
        key = os.path.basename(path)[len("runclaude_"):-len(".sh")]
        if key in _NOT_LAUNCHERS:
            continue
        names = _extract(path)
        model = sorted(names)[0] if names else ""
        out.append({"key": key, "script": path, "model": model})
    return out
