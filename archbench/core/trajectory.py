"""[concept: MONITOR — see ARCHITECTURE.md]

Canonical agent trajectory — OUR telemetry protocol (the one schema analysis reads).

Every agent runtime emits a DIFFERENT native trajectory (mini-swe → thread/turn/
item events; claude_code → its own format; …). Downstream analysis must never
have to adapt to each. So each runtime ships a thin ADAPTER (an anti-corruption
layer — ``AgentRuntime.to_canonical_trajectory``) that converts its native
trajectory into the schema defined HERE, and all analysis reads ONLY this.

The model is the classic agent loop: each STEP is a (thinking, action,
observation) triple. Our domain twist: an action is often a ``submit`` (run a
simulation) and the observation carries the simulator's reaction.

File written per run: ``<run>/trajectory.canonical.jsonl``
  line 0     : a META record   (kind="meta", schema_version, run_id, runtime, …)
  lines 1..N : one STEP record per line

META record:
  {"kind":"meta", "schema_version":1, "run_id":..., "runtime":..., "model":...,
   "challenge":..., "tier":...}

STEP record:
  {"kind":"step", "step":0, "ts":"2026-..Z"|null,
   "thinking": {"text":"...", "tokens":{"prompt":int|null,"completion":int|null,
                                        "thinking":int|null,"total":int|null}},
   "action":   {"kind":"tool_call|submit|bash|message|none", "name":str|null, "args":obj|null},
   "observation":{"kind":"tool_result|sim_result|error|none", "text":str|null,
                  "submission_id":str|null,   # set when action.kind==submit -> join key
                  "status":"ok|error"|null}}

A submit's deep sim metric lives in ``submit_outcomes.jsonl`` (timestamped,
per-trace); join it to the step via ``observation.submission_id``. The canonical
step keeps the submit ACK as its observation so the cognitive stream stays
self-contained.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 1

ACTION_KINDS = {"tool_call", "submit", "bash", "message", "none"}
OBS_KINDS = {"tool_result", "sim_result", "error", "none"}


def meta(run_id: str, runtime: str, model: Optional[str],
         challenge: str, tier: Optional[str]) -> dict:
    return {
        "kind": "meta",
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "runtime": runtime,
        "model": model,
        "challenge": challenge,
        "tier": tier,
    }


def step(
    index: int,
    *,
    ts: Optional[str] = None,
    thinking_text: str = "",
    tokens: Optional[dict] = None,
    action_kind: str = "none",
    action_name: Optional[str] = None,
    action_args: Optional[Any] = None,
    obs_kind: str = "none",
    obs_text: Optional[str] = None,
    submission_id: Optional[str] = None,
    status: Optional[str] = None,
) -> dict:
    """Build one canonical STEP. Adapters call this so the shape can't drift."""
    return {
        "kind": "step",
        "step": index,
        "ts": ts,
        "thinking": {
            "text": thinking_text or "",
            "tokens": tokens or {"prompt": None, "completion": None,
                                 "thinking": None, "total": None},
        },
        "action": {"kind": action_kind, "name": action_name, "args": action_args},
        "observation": {
            "kind": obs_kind, "text": obs_text,
            "submission_id": submission_id, "status": status,
        },
    }


def validate(records: list[dict]) -> list[str]:
    """Return a list of schema violations ('' = valid). Used by the belt-and-suspenders test
    and (optionally) at write time so a bad adapter fails loudly, not silently."""
    errs: list[str] = []
    if not records:
        return ["empty trajectory (no records)"]
    m = records[0]
    if m.get("kind") != "meta":
        errs.append("record 0 must be kind=meta")
    elif m.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"meta.schema_version must be {SCHEMA_VERSION}, got {m.get('schema_version')}")
    for i, r in enumerate(records[1:], start=1):
        if r.get("kind") != "step":
            errs.append(f"record {i}: kind must be 'step'")
            continue
        a = r.get("action") or {}
        o = r.get("observation") or {}
        t = r.get("thinking") or {}
        if a.get("kind") not in ACTION_KINDS:
            errs.append(f"step {r.get('step')}: action.kind={a.get('kind')!r} not in {ACTION_KINDS}")
        if o.get("kind") not in OBS_KINDS:
            errs.append(f"step {r.get('step')}: observation.kind={o.get('kind')!r} not in {OBS_KINDS}")
        if "text" not in t or "tokens" not in t:
            errs.append(f"step {r.get('step')}: thinking must have text + tokens")
    return errs


def write(meta_record: dict, steps: list[dict], path: Path) -> None:
    """Write meta + steps to a canonical .jsonl, validating first (belt-and-suspenders)."""
    records = [meta_record, *steps]
    problems = validate(records)
    if problems:
        raise ValueError("refusing to write invalid canonical trajectory:\n  "
                         + "\n  ".join(problems))
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
