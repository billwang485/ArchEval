"""[concept: MONITOR] Per-run profiling — token spend + wall-clock.

MONITOR already turns each run dir into a self-contained archive (final
workspace, per-turn history, trajectory, submit outcomes). This adds the
*cost* face of that archive: how many tokens the agent burned and how
long things took, distilled into one ``profile.json`` per run for
profiling and cost-vs-benefit reporting (CLAUDE.md scientific rule #7).

Nothing new is instrumented — the data is already emitted:
  - token usage: the mini runtime emits a ``turn.completed`` trajectory
    event per turn with ``usage = {input_tokens, output_tokens,
    cached_input_tokens, total_tokens}``. We sum across turns.
  - wall-clock: ``trajectory.canonical.jsonl`` stamps every step with an
    ISO ``ts``; the span first→last is the agent's think/act wall-time.
  - per-submit: ``submit_outcomes.jsonl`` carries unix
    ``submitted_at / started_at / finished_at`` per submission — the
    simulator/eval wall-time, separable from the agent's.

:func:`extract_profile` is pure over the archive (no live session), so it
backfills any past run and is the source for the session's end-of-run
``profile.json``. Output schema is fixed::

    {
      "tokens": {"input", "output", "cached_input", "total"},
      "turns": int,
      "tokens_per_turn_mean": float|None,
      "agent_wall_seconds": float|None,     # trajectory ts span
      "submits": [{"id","outcome","seconds"}],
      "sim_wall_seconds_total": float,      # sum of submit durations
      "n_submits": int,
      "source": {"trajectory","canonical","outcomes"}  # which files were found
    }
"""

from __future__ import annotations

import datetime as _dt
import glob
import json
from pathlib import Path
from typing import Any, Optional


def _first(results_dir: Path, name: str) -> Optional[Path]:
    hits = sorted(Path(results_dir).rglob(name))
    return hits[0] if hits else None


def _iso(ts: str) -> Optional[float]:
    try:
        return _dt.datetime.fromisoformat(ts).timestamp()
    except Exception:
        return None


def _token_sum(traj: Optional[Path]) -> tuple[dict, int]:
    tok = {"input": 0, "output": 0, "cached_input": 0, "total": 0}
    turns = 0
    if traj is None:
        return tok, turns
    for line in traj.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line or '"turn.completed"' not in line:
            continue
        try:
            u = (json.loads(line).get("usage") or {})
        except Exception:
            continue
        if not u:
            continue
        turns += 1
        tok["input"] += u.get("input_tokens", 0) or 0
        tok["output"] += u.get("output_tokens", 0) or 0
        tok["cached_input"] += u.get("cached_input_tokens", 0) or 0
        tok["total"] += u.get("total_tokens", 0) or 0
    return tok, turns


def _agent_wall(canonical: Optional[Path]) -> Optional[float]:
    if canonical is None:
        return None
    ts = []
    for line in canonical.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            t = json.loads(line).get("ts")
        except Exception:
            continue
        if t and (sec := _iso(t)) is not None:
            ts.append(sec)
    return (max(ts) - min(ts)) if len(ts) >= 2 else None


def _submit_timing(outcomes: Optional[Path]) -> tuple[list[dict], float]:
    submits, total = [], 0.0
    if outcomes is None:
        return submits, total
    for line in outcomes.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        st, fin = r.get("started_at"), r.get("finished_at")
        secs = (fin - st) if isinstance(st, (int, float)) and isinstance(fin, (int, float)) else None
        if secs is not None:
            total += secs
        submits.append({
            "id": r.get("submission_id"),
            "outcome": r.get("outcome"),
            "seconds": round(secs, 3) if secs is not None else None,
        })
    return submits, total


def extract_profile(results_dir: Path) -> dict[str, Any]:
    """Distill token spend + wall-clock from a run archive. Pure (read-only)."""
    results_dir = Path(results_dir)
    traj = _first(results_dir, "trajectory.jsonl")
    canon = _first(results_dir, "trajectory.canonical.jsonl")
    outcomes = _first(results_dir, "submit_outcomes.jsonl")

    tokens, turns = _token_sum(traj)
    agent_wall = _agent_wall(canon)
    submits, sim_total = _submit_timing(outcomes)

    return {
        "tokens": tokens,
        "turns": turns,
        "tokens_per_turn_mean": round(tokens["total"] / turns, 1) if turns else None,
        "agent_wall_seconds": round(agent_wall, 1) if agent_wall is not None else None,
        "submits": submits,
        "n_submits": len(submits),
        "sim_wall_seconds_total": round(sim_total, 1),
        "source": {
            "trajectory": traj is not None,
            "canonical": canon is not None,
            "outcomes": outcomes is not None,
        },
    }


def write_profile(results_dir: Path) -> Optional[Path]:
    """Compute + persist ``profile.json`` into the run dir. Best-effort:
    returns the path on success, None on failure (never raises — MONITOR
    telemetry must not break a session's finally block)."""
    try:
        prof = extract_profile(results_dir)
        out = Path(results_dir) / "profile.json"
        out.write_text(json.dumps(prof, indent=2))
        return out
    except Exception:
        return None


def backfill(root: Path) -> int:
    """Write profile.json for every run dir under root that has a
    submit_outcomes.jsonl or trajectory.jsonl. Returns count written."""
    root = Path(root)
    seen, n = set(), 0
    for marker in ("submit_outcomes.jsonl", "trajectory.jsonl"):
        for f in root.rglob(marker):
            run = f.parent
            if run in seen:
                continue
            seen.add(run)
            if write_profile(run):
                n += 1
    return n
