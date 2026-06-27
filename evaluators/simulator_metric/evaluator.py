"""simulator_metric — geomean speedup vs baseline + per-trace breakdown.

Two paths:

  1. **Bypass** (default for max_submissions=1): if
     ``config.bypass_if_present`` names a file in ``results_dir`` AND
     that file exists, read the agent's on-session sim result from it
     and skip re-running. For cache_replacement_fast the file is
     ``submit_outcomes.jsonl``, written by the MCP server's worker
     thread on every completed submit.

  2. **Re-evaluate**: if bypass is absent or the bypass file is missing,
     re-run ``challenge_dir/evaluate.sh`` against ``results_dir/workspace/``
     for an independent verification pass.

Either path yields the same shape of raw_metric: ChampSim's aggregated
output (top-level 1-element list with ``roi.cores[0].instructions/cycles``
and a ``_per_trace`` breakdown). We derive geomean speedup vs
``baseline.average_ipc`` and per-trace IPC + LLC hit rate.
"""

from __future__ import annotations

import json
import logging
import math
import subprocess
from pathlib import Path
from typing import Any, Optional

from archbench.evaluators.base import BaseEvaluator

log = logging.getLogger("archbench.evaluators.simulator_metric")


class SimulatorMetricEvaluator(BaseEvaluator):
    name = "simulator_metric"

    def evaluate(
        self,
        challenge,
        results_dir: Path,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        bypass_name = config.get("bypass_if_present")
        reference = config.get("reference", "baseline.json")

        # 1. Try bypass: read the on-session submit_outcomes.jsonl.
        raw_metric: Optional[dict[str, Any]] = None
        source = "none"
        if bypass_name:
            bypass_path = results_dir / bypass_name
            if bypass_path.exists():
                # Take the agent's BEST SIM_OK across all submits (multi-submit
                # tiers iterate), by the challenge's headline metric + direction.
                ev = getattr(challenge, "eval", None)
                metric_key = getattr(ev, "metric", None)
                direction = getattr(ev, "direction", "higher_is_better")
                raw_metric = _best_submit_outcomes(
                    bypass_path, metric_key, direction == "higher_is_better"
                )
                if raw_metric is not None:
                    source = "submit_outcomes_jsonl"

        # 2. Fallback: re-run evaluate.sh on results_dir/workspace.
        if raw_metric is None:
            evaluate_sh = _resolve_evaluate_sh(challenge)
            workspace = _workspace_dir(results_dir)
            if evaluate_sh is None:
                return {
                    "ok": False,
                    "source": source,
                    "error": (
                        f"no bypass file and no evaluate.sh for "
                        f"{challenge.challenge_dir} (checked common/evaluation/, "
                        f"evaluation/, eval/, root)"
                    ),
                }
            if workspace is None:
                return {
                    "ok": False,
                    "source": source,
                    "error": (
                        f"no bypass file and no workspace dir under {results_dir}; "
                        f"checked workspace/, workspace_recovered/"
                    ),
                }
            raw_metric = _run_evaluate_sh(evaluate_sh, workspace)
            if raw_metric is None:
                return {
                    "ok": False,
                    "source": "evaluate_sh",
                    "error": f"evaluate.sh against {workspace} did not produce parseable JSON",
                }
            source = "evaluate_sh"

        # 3. Derive geomean speedup vs baseline.json's per-trace IPCs.
        # Phase H reorg: baseline.json moved to evaluation/. If the
        # literal ``reference`` path is missing, try the common locations.
        baseline_path = _find_baseline(challenge.challenge_dir or Path(), reference)
        baseline: Optional[dict[str, Any]] = None
        if baseline_path is not None:
            try:
                baseline = json.loads(baseline_path.read_text())
            except Exception as e:
                log.warning("baseline %s unreadable: %s", baseline_path, e)

        per_trace_ipc: dict[str, float] = {}
        per_trace_hit_rate: dict[str, float] = {}
        for row in raw_metric.get("_per_trace", []) or []:
            t = row.get("trace")
            if not t:
                continue
            if "ipc" in row:
                per_trace_ipc[t] = float(row["ipc"])
            if "llc_hit_rate" in row:
                per_trace_hit_rate[t] = float(row["llc_hit_rate"])

        geomean_speedup = _compute_geomean_speedup(
            per_trace_ipc, baseline,
        )

        result = {
            "ok": True,
            "source": source,
            "geomean_speedup": geomean_speedup,
            "per_trace_ipc": per_trace_ipc,
            "per_trace_hit_rate": per_trace_hit_rate,
            "raw_metric": raw_metric,
            "baseline_average_ipc": (
                baseline.get("average_ipc") if isinstance(baseline, dict) else None
            ),
        }

        # ADDITIVE: for scalar-EDP DSE challenges (gibbon), the final
        # submit is NOT the score — the agent's BEST FEASIBLE design is.
        # Gate strictly so ChampSim (and any per_trace metric) is untouched:
        # the per-submit metric must be EDP-shaped (`edp_raw`) AND carry no
        # per-trace breakdown. We re-scan ALL bypass rows (the final-submit
        # `raw_metric` above only sees the last row).
        is_scalar_edp = (
            isinstance(raw_metric, dict)
            and "edp_raw" in raw_metric
            and not raw_metric.get("_per_trace")
            and not raw_metric.get("per_trace")
        )
        if is_scalar_edp and source == "submit_outcomes_jsonl" and bypass_name:
            bypass_path = results_dir / bypass_name
            # Reuse the already-loaded baseline; but the legacy
            # ``_find_baseline`` does not understand the family/tier layout
            # (baseline lives under the shared ``common/evaluation/``), so for
            # tier challenges it returns None. Resolve it tier-aware here ONLY
            # for the EDP-reduction field — we do NOT touch the ``baseline``
            # that feeds geomean_speedup, so ChampSim behavior is unchanged.
            edp_baseline = baseline
            if not (isinstance(edp_baseline, dict) and "edp" in edp_baseline):
                edp_baseline = _load_baseline_tier_aware(challenge, reference)
            result.update(
                _best_feasible_edp(bypass_path, edp_baseline)
            )

        return result


def _find_baseline(challenge_dir: Path, reference: str) -> Optional[Path]:
    """Locate baseline.json under the Phase H 3-subdir layout.

    Try the literal ``reference`` path first (honors challenge.yaml's
    declared location like ``evaluation/baseline.json``). If missing,
    fall back to the standard subdirs.
    """
    candidates = [
        challenge_dir / reference,
        challenge_dir / "evaluation" / Path(reference).name,
        challenge_dir / "baseline" / Path(reference).name,
        challenge_dir / Path(reference).name,
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _find_evaluate_sh(challenge_dir: Path) -> Optional[Path]:
    """Locate evaluate.sh under the Phase H 3-subdir layout.

    Phase H moved evaluate.sh from the challenge root to
    ``challenge_dir/evaluation/``. Pre-Phase-H challenges keep it at the
    root or under ``eval/``. Check all three.
    """
    for sub in ("evaluation", "eval", ""):
        candidate = challenge_dir / sub / "evaluate.sh" if sub else challenge_dir / "evaluate.sh"
        if candidate.exists():
            return candidate
    return None


def _resolve_evaluate_sh(challenge) -> Optional[Path]:
    """evaluate.sh location, family/tier-aware.

    Family/tier challenges keep the script under the shared
    ``common/evaluation/`` (resolved via ``resolved_dirs``), not under the tier
    dir; fall back to the legacy 3-subdir search for flat challenges.
    """
    try:
        from archbench.core.path_resolution import resolved_dirs

        _sim, eval_dir, _starter = resolved_dirs(challenge)
        candidate = eval_dir / "evaluate.sh"
        if candidate.exists():
            return candidate
    except Exception:
        pass
    return _find_evaluate_sh(challenge.challenge_dir or Path())


def _load_baseline_tier_aware(challenge, reference: str) -> Optional[dict[str, Any]]:
    """Load baseline.json, family/tier-aware (for the EDP-reduction field).

    ``_find_baseline`` only searches under ``challenge.challenge_dir``; for a
    family/tier challenge the baseline lives under the shared
    ``common/evaluation/`` one level up (resolved via ``resolved_dirs``), same
    as ``_resolve_evaluate_sh`` does for ``evaluate.sh``. Try the resolved eval
    dir first, then fall back to the legacy ``_find_baseline`` search. Returns
    the parsed dict or None; never raises.
    """
    candidates: list[Path] = []
    try:
        from archbench.core.path_resolution import resolved_dirs

        _sim, eval_dir, _starter = resolved_dirs(challenge)
        candidates.append(eval_dir / Path(reference).name)
    except Exception:
        pass
    legacy = _find_baseline(challenge.challenge_dir or Path(), reference)
    if legacy is not None:
        candidates.append(legacy)
    for p in candidates:
        try:
            if p.exists():
                return json.loads(p.read_text())
        except Exception as e:
            log.warning("tier-aware baseline %s unreadable: %s", p, e)
    return None


def _workspace_dir(results_dir: Path) -> Optional[Path]:
    """Find the agent's recovered workspace under results_dir.

    Canonical name is ``workspace/`` (set by session._copy_out_workspace).
    Older bake runs used ``workspace_recovered/`` — keep that fallback.
    """
    for name in ("workspace", "workspace_recovered"):
        p = results_dir / name
        if p.is_dir():
            return p
    return None


def _best_submit_outcomes(
    jsonl_path: Path,
    metric_key: Optional[str],
    higher_is_better: bool,
) -> Optional[dict[str, Any]]:
    """Return the metric dict from the BEST SIM_OK submission, or None.

    The point of a multi-submit tier (L1: up to 10 submits) is to let the
    agent ITERATE and keep its best result — so the score must be the best
    of the SIM_OK submissions, NOT whatever the agent happened to submit
    last (an agent may legitimately probe a worse design on its final shot).
    "Best" is by the challenge's headline metric + direction (e.g. highest
    ``ipc`` / lowest ``mpki``). Falls back to last-SIM_OK when the metric key
    is absent from the rows (preserves prior behavior for odd shapes).
    """
    try:
        lines = [L for L in jsonl_path.read_text().splitlines() if L.strip()]
    except Exception as e:
        log.warning("submit_outcomes.jsonl unreadable: %s", e)
        return None
    sim_ok = []
    for L in lines:
        try:
            d = json.loads(L)
        except Exception:
            continue
        if d.get("outcome") == "sim_ok" and d.get("metric"):
            sim_ok.append(d)
    if not sim_ok:
        return _read_submit_outcomes(jsonl_path)
    if metric_key:
        scored = [
            (r["metric"][metric_key], r)
            for r in sim_ok
            if isinstance(r["metric"].get(metric_key), (int, float))
        ]
        if scored:
            pick = (max if higher_is_better else min)(scored, key=lambda x: x[0])
            return pick[1]["metric"]
    # No usable metric key → fall back to the last SIM_OK.
    return sim_ok[-1]["metric"]


def _read_submit_outcomes(jsonl_path: Path) -> Optional[dict[str, Any]]:
    """Return the metric dict from the last SIM_OK row, or None.

    submit_outcomes.jsonl is append-only; we want the agent's final
    SIM_OK submission (the one that consumed their budget). Rows are
    full :class:`SubmissionState.to_dict()` payloads. (Prefer
    :func:`_best_submit_outcomes` for multi-submit tiers — see its docstring.)
    """
    try:
        lines = [
            line for line in jsonl_path.read_text().splitlines()
            if line.strip()
        ]
    except Exception as e:
        log.warning("submit_outcomes.jsonl unreadable: %s", e)
        return None
    if not lines:
        return None

    # Prefer SIM_OK rows (the ones that actually have metrics).
    sim_ok_rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("outcome") == "sim_ok" and d.get("metric"):
            sim_ok_rows.append(d)
    if sim_ok_rows:
        return sim_ok_rows[-1]["metric"]

    # Fallback: any row with a metric.
    for d in reversed([json.loads(L) for L in lines if L.strip()]):
        m = d.get("metric")
        if m:
            return m
    return None


def _best_feasible_edp(
    jsonl_path: Path,
    baseline: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Best-feasible-design summary for scalar-EDP DSE challenges.

    For a multi-submit DSE (e.g. gibbon_codesign) the agent's SCORE is its
    best *feasible* design across all submits — not whatever it happened to
    submit last. Scan every row of ``submit_outcomes.jsonl`` and pick the
    feasible submit with the smallest honest ``edp_raw``.

    A submit is feasible iff ``outcome == "sim_ok"`` AND ``metric.acc_ok`` is
    True AND ``metric.area_ok`` is True. ``edp_reduction_vs_baseline`` is
    ``baseline_edp / best_edp_raw`` (EDP is lower-is-better, so >1 means the
    best design beat the baseline).

    Returns a dict of the five additive fields. Never raises; on any read
    issue it degrades to the zero-feasible result.
    """
    null_result: dict[str, Any] = {
        "best_feasible_edp_raw": None,
        "best_feasible_accuracy": None,
        "best_feasible_submit_index": None,
        "n_feasible_submits": 0,
        "edp_reduction_vs_baseline": None,
    }
    try:
        lines = [
            line for line in jsonl_path.read_text().splitlines()
            if line.strip()
        ]
    except Exception as e:
        log.warning("submit_outcomes.jsonl unreadable for best-feasible: %s", e)
        return null_result

    feasible: list[tuple[int, float, Optional[float]]] = []  # (1-based idx, edp_raw, accuracy)
    for idx, line in enumerate(lines, start=1):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("outcome") != "sim_ok":
            continue
        m = d.get("metric")
        if not isinstance(m, dict):
            continue
        if not (m.get("acc_ok") is True and m.get("area_ok") is True):
            continue
        edp_raw = m.get("edp_raw")
        if edp_raw is None:
            continue
        try:
            edp_raw = float(edp_raw)
        except (TypeError, ValueError):
            continue
        acc = m.get("accuracy")
        try:
            acc = float(acc) if acc is not None else None
        except (TypeError, ValueError):
            acc = None
        feasible.append((idx, edp_raw, acc))

    if not feasible:
        return null_result

    # Smallest edp_raw wins; ties keep the earliest submit (stable: min over
    # (edp_raw, idx) so the first occurrence of the min EDP is selected).
    best_idx, best_edp, best_acc = min(feasible, key=lambda t: (t[1], t[0]))

    baseline_edp = None
    if isinstance(baseline, dict):
        baseline_edp = baseline.get("edp")
        try:
            baseline_edp = float(baseline_edp) if baseline_edp is not None else None
        except (TypeError, ValueError):
            baseline_edp = None

    reduction = None
    if baseline_edp is not None and best_edp > 0:
        reduction = baseline_edp / best_edp

    return {
        "best_feasible_edp_raw": best_edp,
        "best_feasible_accuracy": best_acc,
        "best_feasible_submit_index": best_idx,
        "n_feasible_submits": len(feasible),
        "edp_reduction_vs_baseline": reduction,
    }


def _run_evaluate_sh(evaluate_sh: Path, workspace: Path) -> Optional[dict[str, Any]]:
    """Re-run evaluate.sh against the recovered workspace.

    evaluate.sh's stdout is the aggregate JSON shape ChampSim's plugin
    parses (top-level 1-element list). We parse it the same way:
    forgiving — find the first ``[{"name"`` token and json-decode.
    """
    try:
        result = subprocess.run(
            ["bash", str(evaluate_sh), str(workspace)],
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except subprocess.TimeoutExpired:
        log.warning("evaluate.sh timed out (1800s)")
        return None
    except Exception as e:
        log.warning("evaluate.sh raised: %s", e)
        return None
    if result.returncode != 0:
        log.warning(
            "evaluate.sh rc=%d; stderr tail:\n%s",
            result.returncode, (result.stderr or "")[-500:],
        )
        # Still try to parse — partial successes happen.
    return _parse_aggregate_json(result.stdout)


def _parse_aggregate_json(stdout: str) -> Optional[dict[str, Any]]:
    """Parse evaluate.sh's stdout into the metric dict.

    Whitespace tolerance (Phase H fix): ``aggregate.py`` emits
    ``json.dumps(..., indent=2)`` so the literal ``[{"name"`` token does
    NOT appear in the output (newlines + spaces separate ``[`` and
    ``{``). Use regex anchors that allow whitespace, then ``raw_decode``
    from the match position. Mirrors
    ``simulators/champsim/plugin.py::_extract_bare_champsim_json``.
    """
    if not stdout:
        return None
    import re
    # Whitespace-tolerant anchors. aggregate.py emits the list-of-objects
    # form with `"name": "AggregatedMultiWorkload"`; raw simulate.sh
    # output uses `"name": "Simulation"`. Both shapes resolve here.
    regex_anchors = [
        r'\[\s*\{\s*"name"',
        r'\{\s*"name"',
    ]
    candidates: list[int] = []
    for pat in regex_anchors:
        m = re.search(pat, stdout)
        if m:
            candidates.append(m.start())
    if not candidates:
        return None
    candidates.sort()
    obj = None
    for start in candidates:
        try:
            # json.JSONDecoder.raw_decode handles trailing text gracefully.
            obj, _ = json.JSONDecoder().raw_decode(stdout[start:])
            break
        except Exception:
            continue
    if obj is None:
        return None
    if isinstance(obj, list) and obj:
        obj = obj[0]
    if not isinstance(obj, dict):
        return None
    # Lift the useful aggregate fields into a flat dict so the rest of
    # this module treats it identically to the submit_outcomes payload.
    flat = {
        "ipc": obj.get("_ipc_cycle_weighted"),
        "_per_trace": obj.get("_per_trace", []),
        "_aggregation": obj.get("_aggregation"),
        "_num_traces": obj.get("_num_traces"),
        "_raw": obj,
    }
    return flat


def _compute_geomean_speedup(
    per_trace_ipc: dict[str, float],
    baseline: Optional[dict[str, Any]],
) -> Optional[float]:
    """Geomean of per-trace IPC speedup vs baseline's per-trace IPC.

    Returns None if we can't match any traces against the baseline.
    """
    if not baseline or not per_trace_ipc:
        return None
    baseline_per_trace = {
        row.get("trace"): row.get("ipc")
        for row in baseline.get("per_trace", []) or []
        if isinstance(row, dict)
    }
    if not baseline_per_trace:
        return None
    ratios: list[float] = []
    for trace, ipc in per_trace_ipc.items():
        b = baseline_per_trace.get(trace)
        if b is None or b <= 0 or ipc is None or ipc <= 0:
            continue
        ratios.append(ipc / b)
    if not ratios:
        return None
    # geomean = exp(mean(log(ratios)))
    return math.exp(sum(math.log(r) for r in ratios) / len(ratios))
