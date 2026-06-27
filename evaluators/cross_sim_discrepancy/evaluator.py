"""cross_sim_discrepancy — score how closely two simulators agree.

The FIRST cross-simulator (Tier-3 outcome) evaluator. A multi-sim challenge
(docs/multi_sim_design.md) has the agent submit to each bound simulator
separately; every sim's submit runs ITS own evaluate.sh and writes a SIM_OK
row to ``submit_outcomes.jsonl`` under that sim's ``submission_id`` prefix
(``dramsys_sub_*`` vs ``ramulator_sub_*``). This evaluator reads the FINAL
SIM_OK metric for EACH sim, extracts a chosen metric field (default
``bandwidth_gbps``), and computes the cross-sim discrepancy

    discrepancy_pct = |m_a - m_ref| / m_ref * 100      (single extra sim)

against a reference sim. LOWER is better (the two simulators agree). For more
than two sims the discrepancy is the max pairwise discrepancy vs the reference.

The coupling lives HERE — NOT in a single coupling evaluate.sh — because each
sim's evaluate.sh is sim-local (one image, one container) and only the
post-session layer sees all sims' outcomes at once.

Partial submission (the agent submitted to one sim but not the other) is
reported as ``ok: True, complete: False`` with a null score — an incomplete
run, not a crash.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from archbench.evaluators.base import BaseEvaluator

log = logging.getLogger("archbench.evaluators.cross_sim_discrepancy")


class CrossSimDiscrepancyEvaluator(BaseEvaluator):
    name = "cross_sim_discrepancy"

    def evaluate(
        self,
        challenge,
        results_dir: Path,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        # --- config ---------------------------------------------------------
        # ``sims``: {logical_name: submission_id_prefix}. The prefix is what
        # the connector stamped onto each sim's submission_id (e.g.
        # ``dramsys_``). Default it from the challenge's declared simulators
        # so a challenge that just lists ``simulators: [a, b]`` works without
        # restating the prefixes.
        sims: dict[str, str] = config.get("sims") or {}
        if not sims:
            for s in getattr(challenge, "simulators", []) or []:
                sims[s] = f"{s}_"
        metric_field = config.get("metric_field", "bandwidth_gbps")
        reference_sim = config.get("reference_sim")
        if reference_sim is None and sims:
            # Default reference = the challenge's PRIMARY sim if it's in the
            # set, else the first declared sim.
            primary = getattr(challenge, "simulator", None)
            reference_sim = primary if primary in sims else next(iter(sims))

        jsonl = results_dir / "submit_outcomes.jsonl"
        rows = _read_rows(jsonl)

        # --- extract the final SIM_OK metric value per sim ------------------
        per_sim: dict[str, Optional[float]] = {}
        per_sim_detail: dict[str, dict] = {}
        for sim_name, prefix in sims.items():
            val, detail = _final_metric_for_prefix(rows, prefix, metric_field)
            per_sim[sim_name] = val
            per_sim_detail[sim_name] = detail

        missing = [s for s, v in per_sim.items() if v is None]
        if missing:
            # Partial / incomplete — report, don't crash (the agent may have
            # only submitted to one sim, or one sim never reached SIM_OK).
            return {
                "ok": True,
                "complete": False,
                "score": None,
                "discrepancy_pct": None,
                "metric_field": metric_field,
                "reference_sim": reference_sim,
                "per_sim_metric": per_sim,
                "per_sim_detail": per_sim_detail,
                "reason": (
                    f"no SIM_OK {metric_field!r} found for sim(s) {missing}; "
                    "need one successful submission per simulator to score "
                    "cross-sim agreement"
                ),
                "starter_discrepancy_pct": _starter_discrepancy(challenge),
            }

        # --- compute discrepancy vs the reference sim -----------------------
        ref_val = per_sim.get(reference_sim)
        if ref_val is None or ref_val == 0:
            return {
                "ok": False,
                "complete": True,
                "score": None,
                "discrepancy_pct": None,
                "metric_field": metric_field,
                "reference_sim": reference_sim,
                "per_sim_metric": per_sim,
                "error": (
                    f"reference sim {reference_sim!r} {metric_field}={ref_val!r} "
                    "is missing or zero; cannot normalize discrepancy"
                ),
            }

        pairwise: dict[str, float] = {}
        for sim_name, val in per_sim.items():
            if sim_name == reference_sim:
                continue
            pairwise[sim_name] = round(abs(val - ref_val) / ref_val * 100.0, 4)
        # Headline = the worst (largest) pairwise discrepancy vs the reference.
        discrepancy_pct = max(pairwise.values()) if pairwise else 0.0

        return {
            "ok": True,
            "complete": True,
            # Lower is better — the two simulators agree. The session-level
            # summary uses ``score`` as the tier-3 outcome number.
            "score": discrepancy_pct,
            "discrepancy_pct": discrepancy_pct,
            "direction": "lower_is_better",
            "metric_field": metric_field,
            "reference_sim": reference_sim,
            "per_sim_metric": per_sim,
            "pairwise_discrepancy_pct": pairwise,
            "per_sim_detail": per_sim_detail,
            "starter_discrepancy_pct": _starter_discrepancy(challenge),
        }


def _read_rows(jsonl_path: Path) -> list[dict]:
    """Read submit_outcomes.jsonl into a list of row dicts (best-effort)."""
    if not jsonl_path.exists():
        return []
    rows: list[dict] = []
    try:
        for line in jsonl_path.read_text().splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception as e:
        log.warning("submit_outcomes.jsonl unreadable: %s", e)
    return rows


def _final_metric_for_prefix(
    rows: list[dict], prefix: str, metric_field: str,
) -> tuple[Optional[float], dict]:
    """Return (last SIM_OK metric value for this sim prefix, detail dict).

    The sim is identified by its ``submission_id`` prefix (``dramsys_``).
    We take the LAST SIM_OK row carrying ``metric[metric_field]`` — the
    agent's final successful submission to that sim (the one whose value
    should be scored). Returns (None, detail) if none found.
    """
    matched = [
        r for r in rows
        if str(r.get("submission_id", "")).startswith(prefix)
    ]
    sim_ok = [
        r for r in matched
        if r.get("outcome") == "sim_ok"
        and isinstance(r.get("metric"), dict)
        and r["metric"].get(metric_field) is not None
    ]
    detail = {
        "prefix": prefix,
        "rows_matched": len(matched),
        "sim_ok_with_metric": len(sim_ok),
        "submission_ids": [r.get("submission_id") for r in matched],
    }
    if not sim_ok:
        return None, detail
    last = sim_ok[-1]
    detail["scored_submission_id"] = last.get("submission_id")
    try:
        return float(last["metric"][metric_field]), detail
    except (TypeError, ValueError):
        return None, detail


def _starter_discrepancy(challenge) -> Optional[float]:
    """Read the starter discrepancy from baseline.json, if present.

    Gives the run a denominator-of-record: "the starter configs disagreed by
    X%; the agent got it to Y%". Best-effort — None if no baseline.
    """
    challenge_dir = getattr(challenge, "challenge_dir", None)
    if challenge_dir is None:
        return None
    for cand in (
        Path(challenge_dir) / "evaluation" / "baseline.json",
        Path(challenge_dir) / "baseline.json",
    ):
        if cand.exists():
            try:
                b = json.loads(cand.read_text())
                v = b.get("discrepancy_pct")
                return float(v) if v is not None else None
            except Exception:
                return None
    return None
