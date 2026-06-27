"""Unit tests: typed eval envelope + prediction_calibration evaluator.

Synthetic fixtures only — no simulator, no container, no LLM. The
evaluator's contract (see evaluators/prediction_calibration/info.yaml):
grade the agent's prediction.json against the scored submit and the
real baseline, preferring the connector's submit-time snapshot.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluators._base.envelope import EvalClass, envelope
from evaluators._base.surrogate import _is_untouched_starter_copy
from evaluators.prediction_calibration.evaluator import (
    PredictionCalibrationEvaluator,
)


# ---------------------------------------------------------------- envelope

def test_envelope_derives_ok_only_for_scored():
    assert envelope(EvalClass.SCORED)["ok"] is True
    for clazz in EvalClass.ALL:
        if clazz != EvalClass.SCORED:
            assert envelope(clazz)["ok"] is False


def test_envelope_rejects_unknown_class_and_explicit_ok():
    with pytest.raises(ValueError):
        envelope("totally_new_class")
    with pytest.raises(ValueError):
        envelope(EvalClass.SCORED, ok=True)


# ------------------------------------------------------------ fixtures

CONFIG = {"metric_key": "total_cycles", "direction": "lower"}


def _challenge(tmp_path: Path, baseline_value=15775.0) -> SimpleNamespace:
    cdir = tmp_path / "challenge"
    (cdir / "evaluation").mkdir(parents=True)
    (cdir / "evaluation" / "baseline.json").write_text(
        json.dumps({"total_cycles": baseline_value}))
    return SimpleNamespace(
        challenge_dir=cdir,
        eval=SimpleNamespace(baseline_file="evaluation/baseline.json"),
    )


def _results(tmp_path: Path, rows: list[dict], workspace_files: dict[str, str] | None = None) -> Path:
    rd = tmp_path / "results"
    rd.mkdir()
    (rd / "submit_outcomes.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows))
    if workspace_files:
        ws = rd / "workspace"
        ws.mkdir()
        for rel, text in workspace_files.items():
            p = ws / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
    return rd


def _sim_ok_row(value=9000.0, snapshot: dict | None = None) -> dict:
    row = {"outcome": "sim_ok", "metric": {"total_cycles": value}}
    if snapshot is not None:
        row["prediction_snapshot"] = {
            "path": "/workspace/prediction.json",
            "sha256": "f" * 64,
            "captured_at": 0.0,
            "content": snapshot,
        }
    return row


# ------------------------------------------------- prediction_calibration

def test_scored_bound_prediction(tmp_path):
    """Snapshot on the sim_ok row → bound, fully graded."""
    pred = {"metric": "total_cycles", "predicted": 8000,
            "range": [7000, 10000], "baseline_predicted": 14000}
    rd = _results(tmp_path, [_sim_ok_row(9000.0, snapshot=pred)])
    rep = PredictionCalibrationEvaluator().evaluate(_challenge(tmp_path), rd, CONFIG)
    assert rep["class"] == EvalClass.SCORED and rep["ok"] is True
    assert rep["bound_to_submit"] is True
    # predicted 8000 < baseline 15775 (improvement); actual 9000 < baseline too.
    assert rep["direction_correct"] is True
    assert rep["in_range"] is True          # 7000 <= 9000 <= 10000
    assert rep["relative_error"] == pytest.approx(1000 / 9000)
    assert rep["baseline_log10_error"] > 0  # 14000 vs 15775


def test_scored_unbound_falls_back_to_workspace(tmp_path):
    pred = {"metric": "total_cycles", "predicted": 40000}
    rd = _results(tmp_path, [_sim_ok_row(9000.0)],
                  workspace_files={"prediction.json": json.dumps(pred)})
    rep = PredictionCalibrationEvaluator().evaluate(_challenge(tmp_path), rd, CONFIG)
    assert rep["class"] == EvalClass.SCORED
    assert rep["bound_to_submit"] is False
    # predicted 40000 > baseline 15775 (claims regression) but actual 9000
    # improved — direction wrong.
    assert rep["direction_correct"] is False
    assert rep["in_range"] is None          # no range given


def test_missing_prediction_is_capability_evidence(tmp_path):
    rd = _results(tmp_path, [_sim_ok_row(9000.0)])
    rep = PredictionCalibrationEvaluator().evaluate(_challenge(tmp_path), rd, CONFIG)
    assert rep["class"] == EvalClass.AGENT_MISSING_ARTIFACT
    assert rep["actual"] == 9000.0          # ground truth still echoed


def test_broken_schema(tmp_path):
    rd = _results(tmp_path, [_sim_ok_row(9000.0)],
                  workspace_files={"prediction.json": json.dumps({"predicted": "fast"})})
    rep = PredictionCalibrationEvaluator().evaluate(_challenge(tmp_path), rd, CONFIG)
    assert rep["class"] == EvalClass.ARTIFACT_BROKEN


def test_metric_name_mismatch_is_broken(tmp_path):
    pred = {"metric": "mpki", "predicted": 5.0}
    rd = _results(tmp_path, [_sim_ok_row(9000.0)],
                  workspace_files={"prediction.json": json.dumps(pred)})
    rep = PredictionCalibrationEvaluator().evaluate(_challenge(tmp_path), rd, CONFIG)
    assert rep["class"] == EvalClass.ARTIFACT_BROKEN


def test_sentinel_only_run_has_no_ground_truth(tmp_path):
    rd = _results(tmp_path, [_sim_ok_row(1e30)])
    rep = PredictionCalibrationEvaluator().evaluate(_challenge(tmp_path), rd, CONFIG)
    assert rep["class"] == EvalClass.NO_GROUND_TRUTH


def test_unconfigured_challenge(tmp_path):
    rd = _results(tmp_path, [_sim_ok_row(9000.0)])
    rep = PredictionCalibrationEvaluator().evaluate(_challenge(tmp_path), rd, {})
    assert rep["class"] == EvalClass.NOT_CONFIGURED


def test_retrofitted_prediction_on_metricless_resubmit_does_not_win(tmp_path):
    """Codebase-review blocker: after the scored submit, a budget-exhausted
    repeat submit (sim_ok status but NO metric) carrying an edited
    prediction must not override the snapshot bound to the scored row."""
    honest = {"metric": "total_cycles", "predicted": 8000}
    retrofit = {"metric": "total_cycles", "predicted": 9000}  # matches actual exactly
    rows = [
        _sim_ok_row(9000.0, snapshot=honest),
        {"outcome": "sim_ok", "prediction_snapshot": {
            "path": "/workspace/prediction.json", "sha256": "a" * 64,
            "captured_at": 1.0, "content": retrofit}},
    ]
    rd = _results(tmp_path, rows)
    rep = PredictionCalibrationEvaluator().evaluate(_challenge(tmp_path), rd, CONFIG)
    assert rep["class"] == EvalClass.SCORED and rep["bound_to_submit"] is True
    assert rep["predicted"] == 8000  # the honest, bound one — not the retrofit


def test_snapshot_follows_best_scored_row(tmp_path):
    """Two scored submits: the snapshot graded is the one on the BEST row
    (the row _actual_metric grades against), not the last row."""
    first = {"metric": "total_cycles", "predicted": 7500}
    second = {"metric": "total_cycles", "predicted": 20000}
    rows = [
        _sim_ok_row(9000.0, snapshot=first),    # best (lower direction)
        _sim_ok_row(12000.0, snapshot=second),  # worse, later
    ]
    rd = _results(tmp_path, rows)
    rep = PredictionCalibrationEvaluator().evaluate(_challenge(tmp_path), rd, CONFIG)
    assert rep["class"] == EvalClass.SCORED
    assert rep["actual"] == 9000.0 and rep["predicted"] == 7500


# ------------------------------------------------- untouched-stub detection

def test_untouched_starter_stub_detected(tmp_path):
    starter = tmp_path / "challenge" / "starter"
    starter.mkdir(parents=True)
    (starter / "sim_test.py").write_text("STUB = NotImplemented\n")
    ws = tmp_path / "ws"
    (ws / "starter").mkdir(parents=True)
    (ws / "starter" / "sim_test.py").write_text("STUB = NotImplemented\n")
    ch = SimpleNamespace(starter_dir=starter)
    assert _is_untouched_starter_copy(ws / "starter" / "sim_test.py", ws, ch) is True
    # One byte of agent work → graded, not skipped.
    (ws / "starter" / "sim_test.py").write_text("STUB = 42\n")
    assert _is_untouched_starter_copy(ws / "starter" / "sim_test.py", ws, ch) is False
