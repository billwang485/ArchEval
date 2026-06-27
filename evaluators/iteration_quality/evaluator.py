"""iteration_quality — how well did the agent use its real-simulator budget?

The L1 (full-scaffold) tier gives the agent a generous submit budget and
the REAL simulator in the loop; its Tier-2 "process" question is not
"did you build a surrogate" (L3) or "did you drive the tool" (L2) but
*how efficiently did you iterate*: did scores improve across submits,
how much budget went to build failures, and did the agent stop while
still improving? Everything here is pure arithmetic over
``submit_outcomes.jsonl`` — no LLM, no re-run.

Outputs (class == "scored"):
  attempts_total / outcomes        every row + histogram by outcome.
  sim_ok_count                     scored submits.
  first_ok / best / last_ok        metric values (direction-aware best).
  improvement_from_first           first→best as a >=1.0 ratio (1.0 = the
                                   first scored submit was never beaten).
  improved                         best is strictly better than first.
  best_is_last                     the final scored submit was the best —
                                   the agent stopped while still improving
                                   (or got it right at the end).
  wasted_attempts                  rows that produced no score
                                   (build_fail / timeout / ...).
  budget_max / budget_used_fraction  vs challenge.eval.max_submissions.

Classes: no_ground_truth (no scored submit at all — read the outcome
layer), not_configured (no metric_key/direction).

Tier mapping: Tier 2 (Process) — see docs/evaluator_framework.md.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Optional

from archbench.evaluators.base import BaseEvaluator
from evaluators._base.envelope import EvalClass, envelope

log = logging.getLogger("archbench.evaluators.iteration_quality")


class IterationQualityEvaluator(BaseEvaluator):
    name = "iteration_quality"

    def evaluate(
        self,
        challenge: Any,
        results_dir: Path,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        results_dir = Path(results_dir)
        metric_key = config.get("metric_key")
        direction = config.get("direction")
        if not metric_key or direction not in ("lower", "higher"):
            return envelope(
                EvalClass.NOT_CONFIGURED,
                reason=(
                    "config needs metric_key and direction ('lower'|'higher'); "
                    f"got metric_key={metric_key!r} direction={direction!r}"
                ),
            )

        rows = _outcome_rows(results_dir)
        if not rows:
            return envelope(
                EvalClass.NO_GROUND_TRUTH,
                reason="no submit_outcomes.jsonl rows — the agent never submitted",
            )

        outcomes: dict[str, int] = {}
        scored: list[float] = []  # chronological scored values
        for row in rows:
            oc = str(row.get("outcome"))
            outcomes[oc] = outcomes.get(oc, 0) + 1
            m = row.get("metric")
            v = m.get(metric_key) if isinstance(m, dict) else m
            if isinstance(v, (int, float)) and math.isfinite(v) and v < 1e29:
                scored.append(float(v))

        if not scored:
            return envelope(
                EvalClass.NO_GROUND_TRUTH,
                reason=(
                    f"no submit produced a finite metric[{metric_key!r}] "
                    "(all failed or sentinel-penalized)"
                ),
                attempts_total=len(rows),
                outcomes=outcomes,
            )

        better = (lambda a, b: a < b) if direction == "lower" else (lambda a, b: a > b)
        first, last = scored[0], scored[-1]
        best = scored[0]
        for v in scored[1:]:
            if better(v, best):
                best = v
        improvement = (first / best) if direction == "lower" else (best / first)

        budget_max = config.get("max_submissions")
        if budget_max is None:
            budget_max = getattr(getattr(challenge, "eval", None), "max_submissions", None)

        return envelope(
            EvalClass.SCORED,
            metric=metric_key,
            direction=direction,
            attempts_total=len(rows),
            outcomes=outcomes,
            sim_ok_count=len(scored),
            first_ok=first,
            best=best,
            last_ok=last,
            improvement_from_first=improvement,
            improved=better(best, first),
            best_is_last=(last == best),
            wasted_attempts=len(rows) - len(scored),
            budget_max=budget_max,
            budget_used_fraction=(
                len(scored) / budget_max
                if isinstance(budget_max, (int, float)) and budget_max else None
            ),
        )


def _outcome_rows(results_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for so in sorted(results_dir.rglob("submit_outcomes.jsonl")):
        for line in so.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows
