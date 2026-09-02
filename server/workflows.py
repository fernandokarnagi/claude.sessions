"""
workflows.py — reusable multi-agent blueprints.

A workflow is an ordered list of stages. A stage names which agents take part,
how they work together (mode), what the stage is for, and how you know it is
done. Agents themselves are NOT stored here: they live in Claude's own agents
folder and are read through server/agents.py, so there is one definition of any
agent on this machine and a workflow only ever holds ids.

This module is a blueprint store only: nothing here spawns a session, routes a
hand-off, or talks to tmux. Composing a stage into a prompt lives in the same
module (compose_stage) because it is a pure read of this shape plus the agent
roster. Binding a workflow to a session lives here too — the binding is one
pointer into a workflow plus the log of what has actually been run, and keeping
it beside the workflow is what makes a delete able to clean up after itself.

Shape of .workflows.json:
    {
      "workflows": {
        "<wid>": {
          "title": "...", "description": "...",
          "created_at": "<iso>", "updated_at": "<iso>",
          "stages": [{"id": "s1", "name": "Discovery", "goal": "...",
                      "mode": "parallel", "agent_ids": ["code-reviewer-pro"],
                      "exit_criteria": "..."}]
        }
      },
      "bindings": {
        "<session_id>": {"workflow_id": "<wid>", "stage_index": 0,
                         "assigned_at": "<iso>", "sent": ["s1"],
                         "runs": [{"run_id": "...", "stage_id": "s1",
                                   "stage_name": "...", "stage_index": 0,
                                   "model": "claude-opus-5",
                                   "agent_ids": [...], "edited": false,
                                   "chars": 926, "started_at": "<iso>",
                                   "ended_at": "<iso>|null"}]}
      }
    }
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
import yaml
from datetime import datetime, timezone

from . import agents as agent_roster

_PATH = os.path.join(os.path.dirname(__file__), ".workflows.json")
_lock = threading.RLock()

# An agent has no model of its own. Every agent in a stage is read by the one
# session the stage was sent to, and that session's model was fixed by whatever
# launched it. A per-agent model field could only ever be a suggestion the
# session had no way to act on, so there isn't one.
#
# How the agents in one stage work together. Per stage, not per workflow: a
# real procedure researches in parallel and then writes solo.
MODES = ("coordinator", "handoff", "parallel", "solo")
DEFAULT_MODE = "solo"
# Appended to every composed stage. The stage boundary is the whole point of a
# workflow, and nothing else in the prompt enforces it.
SCOPE_NOTE = (
    "Do this stage only. When the exit criteria are met, stop and report what "
    "you did. Do not start the next stage, and do not take on work belonging "
    "to another role in this workflow — a human sends the next stage when they "
    "are ready."
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_STAGE_ID_RE = re.compile(r"^s(\d+)$")


class _AliasNotAllowed(yaml.YAMLError):
    """Raised by _NoAliasSafeLoader when the document uses an anchor/alias."""


class _NoAliasSafeLoader(yaml.SafeLoader):
    """A SafeLoader that refuses anchors/aliases instead of resolving them.

    yaml.safe_load resolves aliases by sharing references, so the parse
    itself stays cheap and small even when the document is an amplification
    bomb (`a: &a [x]*9` chained a few levels deep into gigabytes). The
    blow-up happens afterwards, in every str() call this module makes on the
    parsed result. Refusing aliases outright means the parsed result is
    bounded by the input bytes, which the byte-size guard already covers.
    """

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise _AliasNotAllowed("YAML anchors and aliases are not supported")
        return super().compose_node(parent, index)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    try:
        with open(_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"workflows": {}, "bindings": {}}
    data.setdefault("workflows", {})
    data.setdefault("bindings", {})
    return data


def _save(data: dict) -> None:
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, _PATH)


def _slug(name: str) -> str:
    return _SLUG_RE.sub("-", name.strip().lower()).strip("-") or "agent"


def _next_stage_n(stages: list[dict]) -> int:
    """One past the highest stage number ever used here.

    Ids are never recycled: a binding remembers which stages it has already
    sent by id, and reusing s1 for a brand-new stage would mark it sent.

    Runs ahead of validate()'s own element type-check (it needs the
    high-water mark before it starts minting ids), so a non-mapping element
    is skipped here rather than raised on — the main loop below still
    rejects it, in order, with a message that names the position.
    """
    used = [int(m.group(1)) for s in stages if isinstance(s, dict)
            and (m := _STAGE_ID_RE.match(str(s.get("id") or "")))]
    return max(used, default=0) + 1


def validate(stages: list[dict], previous: dict | None = None) -> list[dict]:
    """Normalise a stage list, or raise ValueError.

    Returns a cleaned copy with stage ids minted where missing. `previous` is
    the stored record being replaced, when there is one — it carries the stage
    numbers already used, so deleting the newest stage and adding one in the
    same save cannot recycle its id.

    Agent ids are checked for shape and duplicates but NOT for existence. The
    roster is a folder of files that can change without this module knowing, and
    a workflow whose agent file was renamed must still be editable and savable —
    compose_stage and the UI report the missing ones instead.
    """
    prev_stages = (previous or {}).get("stages", [])
    clean_stages: list[dict] = []
    seen_stage_ids: set[str] = set()
    next_n = max(_next_stage_n(stages), _next_stage_n(prev_stages))
    for i, st in enumerate(stages):
        if not isinstance(st, dict):
            raise ValueError(f"stages[{i}] must be a mapping")
        name = str(st.get("name") or "").strip()
        if not name:
            raise ValueError("every stage needs a name")
        mode = str(st.get("mode") or DEFAULT_MODE).strip()
        if mode not in MODES:
            raise ValueError(f"stage {name!r}: mode must be one of {MODES}")
        ids = [str(x).strip() for x in (st.get("agent_ids") or []) if str(x).strip()]
        dup_seen: set[str] = set()
        for aid in ids:
            if aid in dup_seen:
                raise ValueError(f"stage {name!r}: duplicate agent id {aid!r}")
            dup_seen.add(aid)
        if mode == "solo" and len(ids) > 1:
            raise ValueError(f"stage {name!r}: a solo stage takes one agent")
        # Stage ids are machine-minted bookkeeping, never operator-authored,
        # so a missing, malformed, or duplicate id is repaired rather than
        # rejected.
        sid = str(st.get("id") or "").strip()
        if not _STAGE_ID_RE.match(sid) or sid in seen_stage_ids:
            sid = f"s{next_n}"
            next_n += 1
        seen_stage_ids.add(sid)
        clean_stages.append({
            "id": sid,
            "name": name,
            "goal": str(st.get("goal") or "").strip(),
            "mode": mode,
            "agent_ids": ids,
            "exit_criteria": str(st.get("exit_criteria") or "").strip(),
        })
    return clean_stages


def _counts(data: dict) -> dict[str, int]:
    counts = {wid: 0 for wid in data["workflows"]}
    for b in data["bindings"].values():
        wid = b.get("workflow_id")
        if wid in counts:
            counts[wid] += 1
    return counts


def _row(wid: str, rec: dict, sessions: int) -> dict:
    """List shape — counts only. The full agents/stages payload is big enough
    that the list page has no business carrying it."""
    return {
        "id": wid,
        "title": rec.get("title", ""),
        "description": rec.get("description", ""),
        "created_at": rec.get("created_at", ""),
        "updated_at": rec.get("updated_at", ""),
        # Distinct agents used across the stages — the roster is central now,
        # so "how many agents" can only mean "how many this workflow calls on".
        "agent_count": len({a for st in rec.get("stages", [])
                            for a in st.get("agent_ids", [])}),
        "stage_count": len(rec.get("stages", [])),
        "session_count": sessions,
    }


def _full(wid: str, rec: dict) -> dict:
    out = dict(rec)
    out["id"] = wid
    return out


def list_workflows() -> list[dict]:
    with _lock:
        data = _load()
        counts = _counts(data)
        rows = [_row(wid, rec, counts.get(wid, 0))
                for wid, rec in data["workflows"].items()]
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return rows


def get_workflow(wid: str) -> dict | None:
    with _lock:
        rec = _load()["workflows"].get(wid)
        return _full(wid, rec) if rec else None


def create_workflow(title: str, description: str = "") -> dict:
    wid = uuid.uuid4().hex[:12]
    now = _now()
    rec = {
        "title": title.strip() or "Untitled workflow",
        "description": description.strip(),
        "created_at": now,
        "updated_at": now,
        "stages": [],
    }
    with _lock:
        data = _load()
        data["workflows"][wid] = rec
        _save(data)
    return _full(wid, rec)


def update_workflow(wid: str, title: str | None = None,
                    description: str | None = None,
                    stages: list[dict] | None = None) -> dict | None:
    """Replace whatever is passed."""
    with _lock:
        data = _load()
        rec = data["workflows"].get(wid)
        if rec is None:
            return None
        if stages is not None:
            rec["stages"] = validate(stages, previous=rec)
        rec.pop("agents", None)      # a workflow written before the central
                                     # roster still carries its own copy; drop
                                     # it on the first save rather than keep two
        if title is not None:
            rec["title"] = title.strip() or rec.get("title", "Untitled workflow")
        if description is not None:
            rec["description"] = description.strip()
        rec["updated_at"] = _now()
        _save(data)
        return _full(wid, rec)


def delete_workflow(wid: str) -> bool:
    with _lock:
        data = _load()
        if wid not in data["workflows"]:
            return False
        del data["workflows"][wid]
        for sid in [s for s, b in data["bindings"].items()
                    if b.get("workflow_id") == wid]:
            del data["bindings"][sid]
        _save(data)
    return True


# ---------------------------------------------------------------------------
# Composition — a stage rendered as the prompt that actually gets typed.
# ---------------------------------------------------------------------------

def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " and " + names[-1]


def mode_sentence(mode: str, names: list[str]) -> str:
    """One line telling the session how these agents work together.

    This sentence is the whole difference between a pile of personas and a
    procedure, so it is spelled out rather than left for the model to infer.
    """
    if not names:
        return "No agents are assigned to this stage."
    if mode == "solo":
        return f"Coordination: solo. {names[0]} runs this stage alone."
    if mode == "coordinator":
        lead, rest = names[0], names[1:]
        if not rest:
            return f"Coordination: coordinator. {lead} runs this stage alone."
        return (f"Coordination: coordinator. {lead} leads this stage and "
                f"delegates to {_join(rest)}. {lead} owns the final answer.")
    if mode == "handoff":
        return (f"Coordination: hand-off. Run in order: {' → '.join(names)}. "
                "Each agent takes the previous agent's output as its input.")
    return (f"Coordination: parallel. {_join(names)} each work the same input "
            "independently; merge the results at the end.")


def compose_stage(wid: str, stage_index: int) -> str:
    """Render one STORED stage as a single markdown prompt."""
    wf = get_workflow(wid)
    if wf is None:
        raise ValueError("workflow not found")
    stages = wf.get("stages", [])
    if not 0 <= stage_index < len(stages):
        raise ValueError(f"stage index {stage_index} out of range")
    return compose(wf["title"], stages[stage_index])


def compose(title: str, stage: dict) -> str:
    """Render a stage as a single markdown prompt.

    Takes the stage itself rather than an index, so the editor can preview what
    is on screen — including edits not saved yet — through the same code that
    builds what actually gets sent.
    """
    taking_part, missing = agent_roster.get_many(stage.get("agent_ids", []))

    # The workflow description and the "of N" count are deliberately NOT sent.
    # Both tell the agent about work it has not been asked to do yet: the
    # description states the whole pipeline as the mission, and "1/4" advertises
    # three more stages. An agent given either will finish the stage and carry
    # straight on into the next one. The operator sees both in the dashboard,
    # which is where they belong.
    lines = [f"# Workflow: {title}"]
    lines.append("")
    lines.append(f"## Stage: {stage.get('name', '')}")
    if stage.get("goal"):
        lines.append(f"Goal: {stage['goal']}")
    if stage.get("exit_criteria"):
        lines.append(f"Exit criteria: {stage['exit_criteria']}")
    lines.append("")
    lines.append(mode_sentence(stage.get("mode", DEFAULT_MODE),
                               [a["name"] for a in taking_part]))
    for a in taking_part:
        lines.append("")
        head = f"### {a['name']}"
        if a.get("description"):
            head += f" — {a['description']}"
        lines.append(head)
        if a.get("prompt"):
            lines.append(a["prompt"])
    # Named, not silently skipped: a stage that was written for four agents and
    # runs with three is a different stage, and the operator reads this prompt
    # before sending it.
    if missing:
        lines.append("")
        lines.append("> Note: no agent file was found for "
                     + ", ".join(missing) + ".")
    # Last, because recency keeps it live: exit criteria say what done looks
    # like but never say to stop there, and an agent that hits its criteria
    # with an obvious next step in view will take it.
    lines.append("")
    lines.append("## Scope")
    lines.append(SCOPE_NOTE)
    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# Bindings — which workflow a session is running, and where it has got to.
#
# One workflow per session. The stage pointer only ever moves because a human
# pressed Advance; nothing in this module watches a session.
# ---------------------------------------------------------------------------

def _binding_pub(data: dict, sid: str, rec: dict) -> dict | None:
    wf = data["workflows"].get(rec.get("workflow_id"))
    if wf is None:
        return None
    stages = wf.get("stages", [])
    # Clamped on read, not on write: the workflow can be edited down to fewer
    # stages long after the binding was made, and a stored pointer past the
    # end would 500 the session page rather than just showing the last stage.
    idx = max(0, min(int(rec.get("stage_index", 0)), max(len(stages) - 1, 0)))
    return {
        "session_id": sid,
        "workflow_id": rec["workflow_id"],
        "title": wf.get("title", ""),
        "stage_index": idx,
        "stage_count": len(stages),
        "stage_name": stages[idx]["name"] if stages else "",
        "assigned_at": rec.get("assigned_at", ""),
        "sent": list(rec.get("sent", [])),
        "run_count": len(rec.get("runs", [])),
    }


def bind(session_id: str, wid: str) -> dict | None:
    """Assign a workflow to a session, starting at its first stage."""
    with _lock:
        data = _load()
        if wid not in data["workflows"]:
            return None
        data["bindings"][session_id] = {
            "workflow_id": wid,
            "stage_index": 0,
            "assigned_at": _now(),
            "sent": [],
            "runs": [],
        }
        _save(data)
        return _binding_pub(data, session_id, data["bindings"][session_id])


def unbind(session_id: str) -> bool:
    with _lock:
        data = _load()
        if data["bindings"].pop(session_id, None) is None:
            return False
        _save(data)
    return True


def get_binding(session_id: str) -> dict | None:
    with _lock:
        data = _load()
        rec = data["bindings"].get(session_id)
        return _binding_pub(data, session_id, rec) if rec else None


def advance(session_id: str, delta: int) -> dict | None:
    """Move the stage pointer, clamped to the workflow's stages."""
    with _lock:
        data = _load()
        rec = data["bindings"].get(session_id)
        if rec is None:
            return None
        wf = data["workflows"].get(rec.get("workflow_id"))
        if wf is None:
            return None
        last = max(len(wf.get("stages", [])) - 1, 0)
        rec["stage_index"] = max(0, min(int(rec.get("stage_index", 0)) + delta, last))
        _save(data)
        return _binding_pub(data, session_id, rec)


def mark_sent(session_id: str, stage_id: str) -> None:
    """Remember that this stage has been typed into the session at least once,
    so the UI can offer "Re-send" instead of "Send"."""
    with _lock:
        data = _load()
        rec = data["bindings"].get(session_id)
        if rec is None:
            return
        sent = rec.setdefault("sent", [])
        if stage_id not in sent:
            sent.append(stage_id)
            _save(data)


# ---------------------------------------------------------------------------
# Run log — what was actually sent, when, and to what.
#
# The stage pointer says where a workflow HAS GOT TO; it says nothing about
# what happened. A run is one press of Send: which stage, which agents it named,
# which model the session was on at the time, and how long the session stayed
# busy afterwards. Runs are appended and never edited, so re-sending a stage
# leaves both attempts on the record.
# ---------------------------------------------------------------------------

def start_run(session_id: str, *, stage_id: str, stage_name: str,
              stage_index: int, model: str, agent_ids: list[str],
              chars: int, edited: bool) -> dict | None:
    """Open a run for a stage that has just been sent. Returns the run."""
    with _lock:
        data = _load()
        rec = data["bindings"].get(session_id)
        if rec is None:
            return None
        run = {
            "run_id": uuid.uuid4().hex[:12],
            "stage_id": stage_id,
            "stage_name": stage_name,
            "stage_index": int(stage_index),
            "model": model or "",
            "agent_ids": list(agent_ids),
            "chars": int(chars),
            "edited": bool(edited),
            "started_at": _now(),
            "ended_at": None,
        }
        runs = rec.setdefault("runs", [])
        # An earlier run still open means the operator sent again before the
        # session went quiet. Close it at this moment: whatever it was doing,
        # this send is where that turn stopped being the one in flight.
        for prev in runs:
            if prev.get("ended_at") is None:
                prev["ended_at"] = run["started_at"]
                prev["superseded"] = True
        runs.append(run)
        _save(data)
        return dict(run)


def close_open_run(session_id: str, ended_at: str) -> bool:
    """Close the newest open run. `ended_at` is the session's last write, not
    the moment this was noticed, so a duration is what the session took rather
    than how often the dashboard was polled."""
    with _lock:
        data = _load()
        rec = data["bindings"].get(session_id)
        if rec is None:
            return False
        for run in reversed(rec.get("runs", [])):
            if run.get("ended_at") is None:
                if ended_at <= run["started_at"]:
                    return False     # nothing written since the send yet
                run["ended_at"] = ended_at
                _save(data)
                return True
        return False


def list_runs(session_id: str) -> list[dict]:
    """Newest first — the log reads as a history, and the last run is the one
    being asked about."""
    with _lock:
        rec = _load()["bindings"].get(session_id) or {}
        return [dict(r) for r in reversed(rec.get("runs", []))]


def bindings_by_session() -> dict[str, dict]:
    """{session_id: binding} in one file read — for the board and triage
    badges, which decorate many sessions at once."""
    with _lock:
        data = _load()
        out = {}
        for sid, rec in data["bindings"].items():
            pub = _binding_pub(data, sid, rec)
            if pub:
                out[sid] = pub
        return out


def rekey(old_id: str, new_id: str) -> None:
    """Carry a binding onto a new session id (see tmuxio.reset).

    Which procedure you are running is about the work, not the conversation,
    so a /clear shouldn't drop it. A binding already on the new id wins.
    """
    if old_id == new_id:
        return
    with _lock:
        data = _load()
        rec = data["bindings"].pop(old_id, None)
        if rec is None:
            return
        data["bindings"].setdefault(new_id, rec)
        _save(data)


# ---------------------------------------------------------------------------
# YAML — a workflow as a file you can diff, review, and keep in git.
#
# Long system prompts are the bulk of a workflow, and JSON string escaping
# makes them unreadable; YAML block scalars keep them legible.
# ---------------------------------------------------------------------------

def to_yaml(wid: str) -> str | None:
    wf = get_workflow(wid)
    if wf is None:
        return None
    # id/created_at/updated_at describe *this install's* copy, not the
    # procedure itself, so this whitelist leaves them out — two exports of
    # the same procedure diff clean.
    ordered = {
        "title": wf.get("title", ""),
        "description": wf.get("description", ""),
        "stages": wf.get("stages", []),
    }
    return yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True,
                          default_flow_style=False, width=100)


def from_yaml(text: str) -> dict:
    """Create a NEW workflow from YAML. Never overwrites an existing one."""
    try:
        doc = yaml.load(text, Loader=_NoAliasSafeLoader)
    except _AliasNotAllowed as e:
        raise ValueError(str(e)) from e
    except RecursionError as e:
        raise ValueError("workflow file is too deeply nested") from e
    except yaml.YAMLError as e:
        raise ValueError(f"could not parse YAML: {e}") from e
    if not isinstance(doc, dict):
        raise ValueError("a workflow file must be a mapping with a title")
    title = str(doc.get("title") or "").strip()
    if not title:
        raise ValueError("a workflow file needs a title")
    stages = doc.get("stages") or []
    if not isinstance(stages, list):
        raise ValueError("stages must be a list")
    # An "agents:" block from a file written before the central roster is
    # ignored, not rejected: its stages still name the agents by id, and those
    # ids are what resolve against ~/.claude/agents now.
    # Validate before creating, so a bad file leaves nothing behind.
    validate(stages)
    wf = create_workflow(title, str(doc.get("description") or ""))
    return update_workflow(wf["id"], stages=stages)
