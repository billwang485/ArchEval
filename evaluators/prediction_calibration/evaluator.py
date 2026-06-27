"""prediction_calibration — score the agent's self-prediction vs reality.

L3 (architect) tiers ask the agent to predict its own outcome BEFORE the
scored submit, as a machine-readable ``prediction.json`` in the
workspace::

    {
      "metric": "total_cycles",      # must match this evaluator's metric_key
      "predicted": 7000,             # required: point prediction (number)
      "range": [5000, 9000],         # optional: [lo, hi] uncertainty bounds
      "baseline_predicted": 15000,   # optional: agent's estimate of baseline
      "confidence": "medium"         # optional: low | medium | high
    }

This evaluator compares that prediction against the REAL scored result
and the REAL baseline. It is pure arithmetic — no LLM, no simulator
re-run — and answers one question the outcome score cannot: *does the
agent know how good its design is?* (2026-06-10 first measurements:
4/5 agents predicted the right direction, 0/5 were numerically
calibrated, errors 1.5x–139x.)

Outputs (class == "scored"):
  direction_correct   predicted side of the real baseline == actual side.
  in_range            actual inside the agent's stated [lo, hi] (None if
                      no range given).
  log10_error         |log10(predicted / actual)| — scale-free magnitude
                      error (None if signs differ or either is <= 0).
  relative_error      |predicted - actual| / |actual|.
  baseline_log10_error  same scale-free error for the agent's baseline
                      estimate (None if not provided).
  bound_to_submit     True iff the prediction was snapshotted into the
                      scored submit's outcome row by the connector
                      (``prediction_snapshot``) — i.e. the prediction
                      provably accompanied the design that was scored,
                      rather than being edited afterwards. Workspace
                      fallback is graded too, but marked unbound.

Failure classes (see evaluators/_base/envelope.py):
  agent_missing_artifact  no prediction file in workspace or submit rows.
  artifact_broken         file exists but unparsable / wrong schema /
                          metric-name mismatch.
  no_ground_truth         no scored submit to grade against (run failed).
  evaluator_error         no baseline to grade against (harness side).
  not_configured          challenge.yaml didn't give metric_key/direction.

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

log = logging.getLogger("archbench.evaluators.prediction_calibration")

DEFAULT_PREDICTION_FILES = ("prediction.json",)


class PredictionCalibrationEvaluator(BaseEvaluator):
    name = "prediction_calibration"

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

        actual = self._actual_metric(results_dir, metric_key, direction)
        if actual is None:
            return envelope(
                EvalClass.NO_GROUND_TRUTH,
                reason=(
                    f"no scored submit with a finite metric[{metric_key!r}] in "
                    "submit_outcomes.jsonl (all submits failed or were sentinel-"
                    "penalized) — nothing to calibrate against"
                ),
            )

        baseline = self._baseline_metric(challenge, config, metric_key)
        if baseline is None:
            return envelope(
                EvalClass.EVALUATOR_ERROR,
                reason=f"baseline.json has no numeric {metric_key!r}",
            )

        candidates = tuple(config.get("prediction_files") or DEFAULT_PREDICTION_FILES)
        raw, source, bound = self._find_prediction(results_dir, candidates, metric_key, direction)
        if raw is None:
            return envelope(
                EvalClass.AGENT_MISSING_ARTIFACT,
                reason=f"no prediction file (looked for {list(candidates)} in scored submit row, then workspace)",
                looked_for=list(candidates),
                actual=actual,
                baseline_actual=baseline,
            )

        parsed = self._parse_prediction(raw, metric_key)
        if isinstance(parsed, str):  # error message
            return envelope(
                EvalClass.ARTIFACT_BROKEN,
                reason=parsed,
                prediction_source=source,
                actual=actual,
                baseline_actual=baseline,
            )

        predicted, lo, hi, baseline_predicted, confidence = parsed
        better = (lambda a, b: a < b) if direction == "lower" else (lambda a, b: a > b)
        report = envelope(
            EvalClass.SCORED,
            prediction_source=source,
            bound_to_submit=bound,
            metric=metric_key,
            direction=direction,
            predicted=predicted,
            predicted_range=([lo, hi] if lo is not None else None),
            baseline_predicted=baseline_predicted,
            confidence=confidence,
            actual=actual,
            baseline_actual=baseline,
            direction_correct=(better(predicted, baseline) == better(actual, baseline)),
            in_range=(lo <= actual <= hi) if lo is not None else None,
            log10_error=_log10_error(predicted, actual),
            relative_error=(abs(predicted - actual) / abs(actual)) if actual else None,
            baseline_log10_error=(
                _log10_error(baseline_predicted, baseline)
                if baseline_predicted is not None else None
            ),
        )
        return report

    # ------------------------------------------------------------- internals

    @staticmethod
    def _actual_metric(results_dir: Path, key: str, direction: str) -> Optional[float]:
        """The officially scored value: eval_simulator_metric.json's
        raw_metric when present, else best sim_ok row by direction."""
        for em in sorted(results_dir.rglob("eval_simulator_metric.json")):
            try:
                raw = json.loads(em.read_text()).get("raw_metric")
            except Exception:
                continue
            v = raw.get(key) if isinstance(raw, dict) else raw
            if isinstance(v, (int, float)) and math.isfinite(v) and v < 1e29:
                return float(v)
        best: Optional[float] = None
        for so in sorted(results_dir.rglob("submit_outcomes.jsonl")):
            for line in so.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("outcome") != "sim_ok":
                    continue
                m = row.get("metric")
                v = m.get(key) if isinstance(m, dict) else m
                if not isinstance(v, (int, float)) or not math.isfinite(v) or v >= 1e29:
                    continue
                if best is None:
                    best = float(v)
                else:
                    best = min(best, float(v)) if direction == "lower" else max(best, float(v))
        return best

    @staticmethod
    def _baseline_metric(challenge: Any, config: dict[str, Any], key: str) -> Optional[float]:
        cdir = getattr(challenge, "challenge_dir", None)
        if cdir is None:
            return None
        rel = (
            config.get("baseline")
            or getattr(getattr(challenge, "eval", None), "baseline_file", None)
            or "evaluation/baseline.json"
        )
        path = Path(cdir) / rel
        try:
            v = json.loads(path.read_text()).get(key)
        except Exception:
            return None
        return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else None

    @staticmethod
    def _find_prediction(
        results_dir: Path, candidates: tuple[str, ...], key: str, direction: str,
    ) -> tuple[Optional[dict], Optional[str], bool]:
        """Prefer the snapshot bound to the SCORED submit, else the
        end-of-session workspace file (unbound). Returns (raw, source, bound).

        "Scored" = the sim_ok row holding the best finite metric[key] by
        direction — the same row _actual_metric grades against. Snapshots on
        metric-less rows (e.g. budget-exhausted repeat submits) must never
        win: an agent could otherwise re-submit after seeing its result and
        retro-fit the prediction (codebase-review blocker, 2026-06-10).
        """
        best_val: Optional[float] = None
        snap: Optional[dict] = None
        for so in sorted(results_dir.rglob("submit_outcomes.jsonl")):
            for line in so.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("outcome") != "sim_ok":
                    continue
                m = row.get("metric")
                v = m.get(key) if isinstance(m, dict) else m
                if not isinstance(v, (int, float)) or not math.isfinite(v) or v >= 1e29:
                    continue
                is_better = (
                    best_val is None
                    or (v < best_val if direction == "lower" else v > best_val)
                )
                if not is_better:
                    continue
                best_val = float(v)
                ps = row.get("prediction_snapshot")
                snap = (
                    ps["content"]
                    if isinstance(ps, dict) and isinstance(ps.get("content"), dict)
                    else None
                )
        if snap is not None:
            return snap, "submit_row:prediction_snapshot", True
        for ws_name in ("workspace", "workspace_recovered"):
            ws = results_dir / ws_name
            if not ws.is_dir():
                # run layouts nest the session dir; search one level down too
                hits = sorted(results_dir.rglob(ws_name))
                ws = hits[0] if hits else None
                if ws is None:
                    continue
            for rel in candidates:
                p = ws / rel
                if p.is_file():
                    try:
                        return json.loads(p.read_text()), str(p), False
                    except Exception as e:
                        return {"_unparsable": f"{type(e).__name__}: {e}"}, str(p), False
        return None, None, False

    @staticmethod
    def _parse_prediction(raw: dict, metric_key: str):
        """Validate the contract. Returns (predicted, lo, hi,
        baseline_predicted, confidence) or an error string."""
        if "_unparsable" in raw:
            return f"prediction file is not valid JSON ({raw['_unparsable']})"
        stated = raw.get("metric")
        if stated is not None and str(stated) != metric_key:
            return f"prediction.metric={stated!r} but this challenge scores {metric_key!r}"
        predicted = raw.get("predicted")
        if not isinstance(predicted, (int, float)) or not math.isfinite(predicted):
            return f"prediction.predicted must be a finite number, got {predicted!r}"
        lo = hi = None
        rng = raw.get("range")
        if rng is not None:
            if (
                not isinstance(rng, (list, tuple)) or len(rng) != 2
                or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in rng)
                or rng[0] > rng[1]
            ):
                return f"prediction.range must be [lo, hi] with lo <= hi, got {rng!r}"
            lo, hi = float(rng[0]), float(rng[1])
        bp = raw.get("baseline_predicted")
        if bp is not None and (not isinstance(bp, (int, float)) or not math.isfinite(bp)):
            return f"prediction.baseline_predicted must be a number, got {bp!r}"
        return (
            float(predicted), lo, hi,
            float(bp) if bp is not None else None,
            raw.get("confidence"),
        )


def _log10_error(predicted: float, actual: float) -> Optional[float]:
    if predicted > 0 and actual > 0:
        return abs(math.log10(predicted / actual))
    return None
