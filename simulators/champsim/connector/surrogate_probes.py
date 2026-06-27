"""ChampSim wiring for :class:`evaluators._base.surrogate.BaseSurrogateEvaluator`.

Exports the four bits the base needs to score a ChampSim offline
surrogate:

  * ``CHAMPSIM_CANDIDATE_FILES`` — workspace-relative search list for
    the agent's Python LLC sim source.
  * ``CHAMPSIM_PROBES`` — the four interface shapes the prompt
    historically allows (predict_ipc, simulate, Simulator.run,
    subprocess-with-main).
  * ``champsim_find_workloads`` — locate decoded ``.trace.txt`` files
    on the host (env-var pool, in-repo pool, challenge dir).
  * ``champsim_read_ground_truth`` — pull per-trace IPC from
    ``submit_outcomes.jsonl`` (with fallbacks to ``reeval_result.json``
    and ``raw_log_tail`` scraping for ``SIM_TIMEOUT``-with-data rows).

These were extracted verbatim from the prior monolithic
``evaluators/offline_sim_calibration/evaluator.py`` so the refactor is
behaviorally identical on ChampSim. See
``docs/lessons_learned.md`` §1 (provenance) on why we never fabricate
numbers — every "missing data" path is reported, not back-filled.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from evaluators._base.surrogate import ProbeDescriptor

log = logging.getLogger("archbench.simulators.champsim.surrogate")


# ---------------------------------------------------------------------------
# CANDIDATE_FILES — workspace-relative paths the prompt historically suggests.
# ---------------------------------------------------------------------------


CHAMPSIM_CANDIDATE_FILES: list[str] = [
    "sim_test.py",
    "tests/simulator.py",
    "tests/sim_test.py",
]


# ---------------------------------------------------------------------------
# Probe output extractors.
# ---------------------------------------------------------------------------


def _extract_predict_ipc(result: Any) -> float:
    """``predict_ipc(trace_path) -> float`` — straight cast."""
    return float(result)


def _extract_simulate_dict_or_float(result: Any) -> float:
    """``simulate(trace_path) -> dict | float`` — pull ``ipc`` from dict."""
    if isinstance(result, dict) and "ipc" in result:
        return float(result["ipc"])
    if isinstance(result, (int, float)):
        return float(result)
    raise TypeError(f"simulate() returned {type(result).__name__}, need dict/float")


def _extract_simulator_run(result: Any) -> float:
    """``Simulator().run(trace_path) -> dict | float`` — same as simulate."""
    if isinstance(result, dict) and "ipc" in result:
        return float(result["ipc"])
    if isinstance(result, (int, float)):
        return float(result)
    raise TypeError(f"Simulator.run() returned {type(result).__name__}")


def _extract_subprocess_ipc(streams: tuple[str, str]) -> Optional[float]:
    """Scrape ``ipc=<float>`` from (stdout, stderr) of the agent's script.

    Falls back to the last numeric token on the last non-empty line so
    "print bare number" scripts still produce a value. Returns None if
    nothing parseable was found — caller treats None as a per-trace
    error.
    """
    stdout, stderr = streams
    text = (stdout or "") + "\n" + (stderr or "")
    m = re.search(r"ipc\s*[=:]\s*([0-9.eE+-]+)", text, re.IGNORECASE)
    if m:
        return float(m.group(1))
    for line in reversed(text.splitlines()):
        tok = line.strip().split()[-1] if line.strip() else ""
        try:
            return float(tok)
        except ValueError:
            continue
    return None


def _subprocess_gating(sim_file: Path) -> bool:
    """Only enable the subprocess probe for scripts with a ``__main__`` guard.

    Without this, we'd subprocess-invoke import-only utility modules and
    record meaningless per-trace errors. The earlier probes (predict_ipc,
    simulate, Simulator.run) take priority; subprocess is the catch-all.
    """
    try:
        src = sim_file.read_text()
    except Exception:
        return False
    return "__main__" in src and "if __name__" in src


# ---------------------------------------------------------------------------
# PROBES — verbatim from the prior monolithic evaluator.
# ---------------------------------------------------------------------------


CHAMPSIM_PROBES: list[ProbeDescriptor] = [
    # Probe 1: predict_ipc(trace_path) -> float
    ProbeDescriptor(
        name="predict_ipc",
        kind="callable_takes_path",
        attr_name="predict_ipc",
        output_extract=_extract_predict_ipc,
    ),
    # Probe 2: simulate(trace_path) -> dict (look for 'ipc') or float
    ProbeDescriptor(
        name="simulate",
        kind="callable_takes_path",
        attr_name="simulate",
        output_extract=_extract_simulate_dict_or_float,
    ),
    # Probe 3: Simulator() with .run(trace_path)
    ProbeDescriptor(
        name="Simulator.run",
        kind="class_with_run",
        attr_name="Simulator",
        output_extract=_extract_simulator_run,
    ),
    # Probe 4: subprocess catch-all — agent script with main() that prints.
    # Gated on the presence of a ``__main__`` guard so we don't subprocess
    # utility imports.
    ProbeDescriptor(
        name="subprocess",
        kind="subprocess_main",
        attr_name="",
        output_extract=_extract_subprocess_ipc,
        gating=_subprocess_gating,
    ),
]


# ---------------------------------------------------------------------------
# Workload location — decoded .trace.txt on host.
# ---------------------------------------------------------------------------


def champsim_find_workloads(
    challenge: Any,
    workspace: Path,
    workload_names: list[str],
) -> dict[str, Path]:
    """Locate decoded ``.trace.txt`` paths for the given trace names.

    Looked-at roots (in order):
      1. ``$ARCHBENCH_WORKLOADS_DIR/champsim/decoded`` (canonical host path).
      2. ``<repo>/workload_pools/champsim/decoded`` (in-repo symlink).
      3. ``<challenge_dir>/traces/decoded`` (legacy fallback).
    Returns ``{trace_name: trace_path}`` for every trace we could
    resolve; missing entries are simply absent (the caller records them
    as per-trace errors).
    """
    decoded_root = _find_decoded_traces_root(challenge, workload_names)
    if decoded_root is None:
        return {}
    out: dict[str, Path] = {}
    for name in workload_names:
        p = _resolve_trace_path(decoded_root, name)
        if p is not None:
            out[name] = p
    return out


def _find_decoded_traces_root(
    challenge: Any, trace_names,
) -> Optional[Path]:
    env = os.environ.get("ARCHBENCH_WORKLOADS_DIR")
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env) / "champsim" / "decoded")
    if challenge is not None and getattr(challenge, "challenge_dir", None):
        repo_root = challenge.challenge_dir.resolve().parents[1]
        candidates.append(repo_root / "workload_pools" / "champsim" / "decoded")
        candidates.append(challenge.challenge_dir / "traces" / "decoded")
    trace_basenames = {_trace_basename(t) for t in trace_names}
    for root in candidates:
        if not root.is_dir():
            continue
        for base in trace_basenames:
            for cand in (
                root / f"{base}.trace.txt",
                root / f"{base}.txt",
            ):
                if cand.is_file():
                    return root
        # Partial coverage is still useful.
        if any(root.glob("*.trace.txt")):
            return root
    return None


def _trace_basename(name: str) -> str:
    """Strip extensions to a basename the resolver can match.

    e.g. ``482.sphinx3-1100B_chunk0.champsimtrace.xz`` →
         ``482.sphinx3-1100B_chunk0``.
    """
    for suffix in (
        ".champsimtrace.xz", ".trace.txt", ".trace.xz",
        ".xz", ".txt", ".trace",
    ):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _resolve_trace_path(decoded_root: Path, trace_name: str) -> Optional[Path]:
    base = _trace_basename(trace_name)
    for cand in (
        decoded_root / f"{base}.trace.txt",
        decoded_root / f"{base}.txt",
        decoded_root / f"{base}.trace",
    ):
        if cand.is_file():
            return cand
    # Some decoded files drop the ``_chunkN`` suffix.
    short = base.split("_chunk")[0]
    for cand in (
        decoded_root / f"{short}.trace.txt",
        decoded_root / f"{short}.txt",
    ):
        if cand.is_file():
            return cand
    return None


# ---------------------------------------------------------------------------
# Ground-truth reader — per-trace IPC from the on-session submit.
# ---------------------------------------------------------------------------


def champsim_read_ground_truth(results_dir: Path) -> dict[str, float]:
    """Pull per-trace IPC from ``submit_outcomes.jsonl``.

    Handles two shapes:
      * SIM_OK row with ``metric._per_trace`` populated (the normal case).
      * SIM_TIMEOUT / etc. rows where the parser failed but the
        simulator still finished — ChampSim's aggregate JSON is
        preserved verbatim in ``raw_log_tail``. We try to parse it out.
    Falls back to ``reeval_result.json`` (``bake_*``-style re-eval
    artifacts) when ``submit_outcomes.jsonl`` is unreadable or empty.
    """
    path = results_dir / "submit_outcomes.jsonl"
    if not path.exists():
        return _read_real_from_reeval(results_dir)
    out: dict[str, float] = {}
    try:
        lines = [L for L in path.read_text().splitlines() if L.strip()]
    except Exception as e:
        log.warning("submit_outcomes.jsonl unreadable: %s", e)
        return _read_real_from_reeval(results_dir)
    # Newest-first; first row with usable per-trace data wins.
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except Exception:
            continue
        metric = row.get("metric") or {}
        rows = metric.get("_per_trace") if isinstance(metric, dict) else None
        if isinstance(rows, list) and rows:
            for r in rows:
                if isinstance(r, dict) and r.get("trace") and "ipc" in r:
                    out[r["trace"]] = float(r["ipc"])
            if out:
                return out
        tail = row.get("raw_log_tail") or ""
        if isinstance(tail, str) and tail:
            scraped = _scrape_per_trace_from_tail(tail)
            if scraped:
                return scraped
    return _read_real_from_reeval(results_dir)


def _read_real_from_reeval(results_dir: Path) -> dict[str, float]:
    """Fallback: ``bake_*/reeval_result.json`` has a per_trace block too."""
    path = results_dir / "reeval_result.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    rows = data.get("per_trace") or []
    out: dict[str, float] = {}
    for r in rows:
        if isinstance(r, dict) and r.get("trace") and "agent_ipc" in r:
            out[r["trace"]] = float(r["agent_ipc"])
    return out


def _scrape_per_trace_from_tail(tail: str) -> dict[str, float]:
    """Extract trace→ipc from the JSON-shaped ``raw_log_tail``."""
    out: dict[str, float] = {}
    for m in re.finditer(
        r'"trace"\s*:\s*"([^"]+)"\s*,\s*"instructions"[^}]*?"ipc"\s*:\s*([0-9.eE+-]+)',
        tail,
    ):
        out[m.group(1)] = float(m.group(2))
    return out


__all__ = [
    "CHAMPSIM_CANDIDATE_FILES",
    "CHAMPSIM_PROBES",
    "champsim_find_workloads",
    "champsim_read_ground_truth",
]
