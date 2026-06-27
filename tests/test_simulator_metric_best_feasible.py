"""Best-feasible-design scoring for scalar-EDP DSE challenges (gibbon_codesign).

Regression for the 2026-06-01 gibbon L1 flight: ``simulator_metric`` was
ChampSim-shaped and reported only the agent's FINAL submit. For a multi-submit
DSE (gibbon emits a scalar ``edp``/``edp_raw`` per submit, no ``_per_trace``)
that is wrong — a run that finds a great feasible design mid-flight then
regresses on its last submit looked like a failure.

The additive logic re-scans ALL rows of ``submit_outcomes.jsonl`` and, ONLY
when the per-submit metric is EDP-shaped (has ``edp_raw`` AND no per-trace
breakdown), reports the BEST FEASIBLE design (smallest honest ``edp_raw`` among
``sim_ok`` + ``acc_ok`` + ``area_ok`` rows) and its EDP reduction vs baseline.

These tests assert:
  * the new fields are computed correctly for a gibbon-shaped jsonl
    (mirroring the real run: a feasible-but-worse row, two feasible-better
    ties, two infeasible acc_ok=False rows);
  * the zero-feasible degradation;
  * the ChampSim (``_per_trace``, no ``edp_raw``) path is COMPLETELY
    unaffected — none of the new fields appear.

Style mirrors tests/test_evaluators.py (get_evaluator + load_challenge) and
tests/test_gibbon_tier_fixes.py (REPO root resolution).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from archbench.core.challenge import load_challenge
from archbench.evaluators import get_evaluator

_NEW_FIELDS = (
    "best_feasible_edp_raw",
    "best_feasible_accuracy",
    "best_feasible_submit_index",
    "n_feasible_submits",
    "edp_reduction_vs_baseline",
)


# ---------------------------------------------------------------------------
# Helpers (a minimal loadable challenge + jsonl/baseline writers)
# ---------------------------------------------------------------------------


def _make_challenge_dir(tmp_path: Path) -> Path:
    chdir = tmp_path / "challenges" / "fake_gibbon"
    starter = chdir / "starter"
    starter.mkdir(parents=True)
    (starter / "design.json").write_text("{}")
    (chdir / "challenge.yaml").write_text(
        "id: fake_gibbon\n"
        "name: fake_gibbon\n"
        "simulator: mnsim\n"
        "prompt: 'do the thing'\n"
        "input:\n"
        "  starter_files: [design.json]\n"
        "output:\n"
        "  files: [design.json]\n"
        "eval:\n"
        "  baseline: baseline.json\n"
        "  max_submissions: 5\n"
    )
    return chdir


def _write_baseline(challenge_dir: Path, edp) -> None:
    payload = {"edp": edp, "edp_raw": edp, "per_trace": []}
    if edp is None:
        payload = {"per_trace": []}  # baseline WITHOUT an edp field
    (challenge_dir / "baseline.json").write_text(json.dumps(payload))


def _edp_metric(*, acc_ok, area_ok, accuracy, edp_raw, edp=None):
    """A gibbon-shaped per-submit metric (scalar EDP, NO _per_trace)."""
    return {
        "edp": edp if edp is not None else edp_raw,
        "edp_raw": edp_raw,
        "accuracy": accuracy,
        "accuracy_is_surrogate": True,
        "acc_floor": 0.738,
        "acc_ok": acc_ok,
        "area_ok": area_ok,
        "area_um2": 1.0e8,
    }


def _row(outcome, metric, sub_id):
    return {
        "submission_id": sub_id,
        "status": "done",
        "outcome": outcome,
        "metric": metric,
    }


def _write_outcomes(results_dir: Path, rows: list[dict]) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "submit_outcomes.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n"
    )


def _evaluate(challenge, results_dir: Path):
    ev = get_evaluator("simulator_metric")
    return ev.evaluate(
        challenge,
        results_dir,
        {"bypass_if_present": "submit_outcomes.jsonl", "reference": "baseline.json"},
    )


# ---------------------------------------------------------------------------
# 1. Gibbon-shaped run: best-feasible mirrors the real smoketest flight
# ---------------------------------------------------------------------------


def test_best_feasible_picks_min_edp_among_feasible(tmp_path: Path):
    """Mirror results/gibbon_codesign_L1/smoketest_mnsim_20260601_230220:
    #1 feasible (worse EDP), #2 & #3 feasible ties (better EDP), #4 & #5
    infeasible (acc_ok False). Best feasible = the #2/#3 value; n=3."""
    chdir = _make_challenge_dir(tmp_path)
    baseline_edp = 1.402670638141_7454e12
    _write_baseline(chdir, baseline_edp)
    challenge = load_challenge(chdir)

    results_dir = tmp_path / "results" / "run1"
    better_edp = 293994656944.04126
    worse_edp = baseline_edp
    rows = [
        # #1 feasible but worse EDP (== baseline)
        _row("sim_ok", _edp_metric(
            acc_ok=True, area_ok=True, accuracy=0.7389973693293896,
            edp_raw=worse_edp), "sub_001"),
        # #2 feasible, better EDP
        _row("sim_ok", _edp_metric(
            acc_ok=True, area_ok=True, accuracy=0.7467228300630666,
            edp_raw=better_edp), "sub_002"),
        # #3 feasible, same better EDP (tie)
        _row("sim_ok", _edp_metric(
            acc_ok=True, area_ok=True, accuracy=0.7467228300630666,
            edp_raw=better_edp), "sub_003"),
        # #4 infeasible: acc below floor → honest edp_raw kept, edp gated to 1e30
        _row("sim_ok", _edp_metric(
            acc_ok=False, area_ok=True, accuracy=0.7208958092762144,
            edp_raw=135933473641.20891, edp=1e30), "sub_004"),
        # #5 infeasible: acc below floor
        _row("sim_ok", _edp_metric(
            acc_ok=False, area_ok=True, accuracy=0.7197473568145734,
            edp_raw=better_edp, edp=1e30), "sub_005"),
    ]
    _write_outcomes(results_dir, rows)

    report = _evaluate(challenge, results_dir)

    assert report["ok"] is True
    assert report["source"] == "submit_outcomes_jsonl"
    # geomean_speedup is correctly null for a scalar-EDP (no per_trace) metric.
    assert report["geomean_speedup"] is None

    assert report["n_feasible_submits"] == 3
    assert report["best_feasible_edp_raw"] == pytest.approx(better_edp)
    assert report["best_feasible_accuracy"] == pytest.approx(0.7467228300630666)
    # Tie on EDP → earliest feasible submit (1-based #2) wins.
    assert report["best_feasible_submit_index"] == 2
    assert report["edp_reduction_vs_baseline"] == pytest.approx(
        baseline_edp / better_edp
    )
    # Existing behavior preserved: raw_metric is still the FINAL submit (#5).
    assert report["raw_metric"]["accuracy"] == pytest.approx(0.7197473568145734)
    assert report["raw_metric"]["acc_ok"] is False


def test_best_feasible_ignores_area_violation(tmp_path: Path):
    """area_ok=False is infeasible even with acc_ok=True and a tiny EDP."""
    chdir = _make_challenge_dir(tmp_path)
    _write_baseline(chdir, 1.0e12)
    challenge = load_challenge(chdir)
    results_dir = tmp_path / "results" / "run1"
    rows = [
        _row("sim_ok", _edp_metric(
            acc_ok=True, area_ok=False, accuracy=0.80, edp_raw=1.0e9), "s1"),
        _row("sim_ok", _edp_metric(
            acc_ok=True, area_ok=True, accuracy=0.75, edp_raw=5.0e11), "s2"),
    ]
    _write_outcomes(results_dir, rows)
    report = _evaluate(challenge, results_dir)
    assert report["n_feasible_submits"] == 1
    assert report["best_feasible_edp_raw"] == pytest.approx(5.0e11)
    assert report["best_feasible_submit_index"] == 2


def test_best_feasible_zero_feasible_submits(tmp_path: Path):
    """All submits infeasible → nulls + n=0 + reduction null (no crash)."""
    chdir = _make_challenge_dir(tmp_path)
    _write_baseline(chdir, 1.0e12)
    challenge = load_challenge(chdir)
    results_dir = tmp_path / "results" / "run1"
    rows = [
        _row("sim_ok", _edp_metric(
            acc_ok=False, area_ok=True, accuracy=0.70, edp_raw=1.0e9,
            edp=1e30), "s1"),
        _row("sim_ok", _edp_metric(
            acc_ok=True, area_ok=False, accuracy=0.80, edp_raw=2.0e9), "s2"),
    ]
    _write_outcomes(results_dir, rows)
    report = _evaluate(challenge, results_dir)
    assert report["n_feasible_submits"] == 0
    assert report["best_feasible_edp_raw"] is None
    assert report["best_feasible_accuracy"] is None
    assert report["best_feasible_submit_index"] is None
    assert report["edp_reduction_vs_baseline"] is None


def test_best_feasible_baseline_without_edp_does_not_crash(tmp_path: Path):
    """Baseline lacking an `edp` field → reduction null, others still set."""
    chdir = _make_challenge_dir(tmp_path)
    _write_baseline(chdir, None)  # baseline.json has no `edp`
    challenge = load_challenge(chdir)
    results_dir = tmp_path / "results" / "run1"
    rows = [
        _row("sim_ok", _edp_metric(
            acc_ok=True, area_ok=True, accuracy=0.75, edp_raw=3.0e11), "s1"),
    ]
    _write_outcomes(results_dir, rows)
    report = _evaluate(challenge, results_dir)
    assert report["n_feasible_submits"] == 1
    assert report["best_feasible_edp_raw"] == pytest.approx(3.0e11)
    assert report["edp_reduction_vs_baseline"] is None


def test_best_feasible_baseline_resolves_for_family_tier_layout(tmp_path: Path):
    """The real gibbon challenge is a family/tier (baseline under the shared
    ``common/evaluation/``, NOT the tier dir). ``_find_baseline`` can't see it,
    so the EDP-reduction field would be null unless the tier-aware fallback
    resolves it. Build a minimal family/tier on disk and assert the reduction
    is computed (regression for edp_reduction_vs_baseline == null on real run).
    """
    fam = tmp_path / "challenges" / "fake_family"
    # Shared common/evaluation/baseline.json (the family-level baseline) — this
    # mirrors challenges/gibbon_codesign/common/evaluation/baseline.json.
    common_eval = fam / "common" / "evaluation"
    common_eval.mkdir(parents=True)
    baseline_edp = 1.0e12
    (common_eval / "baseline.json").write_text(
        json.dumps({"edp": baseline_edp, "edp_raw": baseline_edp, "per_trace": []})
    )
    (common_eval / "evaluate.sh").write_text("#!/bin/bash\necho fake\n")
    common_sim = fam / "common" / "simulator"
    common_sim.mkdir(parents=True)
    # The family ROOT is the L3 challenge (live mode-2 layout); the sibling
    # `assisted/` dir is the detection tell-tale, and the shared dirs live under
    # `common/`. (The interim `tiers/<L>` layout was removed — CLAUDE.md §1.3.)
    (fam / "assisted").mkdir(parents=True)
    starter = fam / "starter"
    starter.mkdir(parents=True)
    (starter / "design.json").write_text("{}")
    (fam / "challenge.yaml").write_text(
        "id: fake_family_L3\n"
        "name: fake_family_L3\n"
        "simulator: mnsim\n"
        "prompt: 'do the thing'\n"
        "input:\n  starter_files: [design.json]\n"
        "output:\n  files: [design.json]\n"
        "eval:\n  baseline: evaluation/baseline.json\n  max_submissions: 5\n"
    )
    challenge = load_challenge(fam)
    # Sanity: the loader resolved the shared common/evaluation as the eval dir.
    assert challenge.evaluation_dir == common_eval

    results_dir = tmp_path / "results" / "run1"
    rows = [
        _row("sim_ok", _edp_metric(
            acc_ok=True, area_ok=True, accuracy=0.75, edp_raw=2.0e11), "s1"),
    ]
    _write_outcomes(results_dir, rows)

    ev = get_evaluator("simulator_metric")
    report = ev.evaluate(
        challenge, results_dir,
        {"bypass_if_present": "submit_outcomes.jsonl",
         "reference": "evaluation/baseline.json"},
    )
    assert report["n_feasible_submits"] == 1
    assert report["best_feasible_edp_raw"] == pytest.approx(2.0e11)
    # Tier-aware fallback found common/evaluation/baseline.json → reduction set.
    assert report["edp_reduction_vs_baseline"] == pytest.approx(baseline_edp / 2.0e11)


# ---------------------------------------------------------------------------
# 2. ChampSim path is UNAFFECTED (the load-bearing invariant)
# ---------------------------------------------------------------------------


def test_champsim_per_trace_path_has_no_best_feasible_fields(tmp_path: Path):
    """A pure ChampSim metric (has _per_trace, NO edp_raw) must NOT grow any
    best_feasible_* field — the additive logic is gated off entirely."""
    chdir = _make_challenge_dir(tmp_path)
    # ChampSim-style baseline (per-trace IPCs; no edp).
    (chdir / "baseline.json").write_text(json.dumps({
        "average_ipc": 0.5,
        "per_trace": [
            {"trace": "t1", "ipc": 0.4},
            {"trace": "t2", "ipc": 0.6},
        ],
    }))
    challenge = load_challenge(chdir)
    results_dir = tmp_path / "results" / "run1"
    rows = [
        _row("sim_ok", {
            "ipc": 0.6,
            "_per_trace": [
                {"trace": "t1", "ipc": 0.8, "llc_hit_rate": 0.5},
                {"trace": "t2", "ipc": 0.9, "llc_hit_rate": 0.55},
            ],
        }, "sub_001"),
    ]
    _write_outcomes(results_dir, rows)
    report = _evaluate(challenge, results_dir)

    # ChampSim scoring intact.
    assert report["ok"] is True
    assert report["geomean_speedup"] == pytest.approx((2.0 * 1.5) ** 0.5, rel=1e-3)
    assert report["per_trace_ipc"]["t1"] == 0.8
    # NONE of the additive fields leaked in.
    for k in _NEW_FIELDS:
        assert k not in report, f"ChampSim path unexpectedly grew {k!r}"


def test_edp_shaped_metric_with_per_trace_is_treated_as_per_trace(tmp_path: Path):
    """Defensive: if a metric somehow carries BOTH edp_raw AND a non-empty
    _per_trace, the gate (no per-trace) refuses the best-feasible path so we
    never mis-handle a per-trace metric."""
    chdir = _make_challenge_dir(tmp_path)
    (chdir / "baseline.json").write_text(json.dumps({
        "average_ipc": 0.5, "edp": 1.0e12,
        "per_trace": [{"trace": "t1", "ipc": 0.4}],
    }))
    challenge = load_challenge(chdir)
    results_dir = tmp_path / "results" / "run1"
    rows = [
        _row("sim_ok", {
            "edp_raw": 3.0e11, "acc_ok": True, "area_ok": True,
            "accuracy": 0.75,
            "_per_trace": [{"trace": "t1", "ipc": 0.8}],
        }, "s1"),
    ]
    _write_outcomes(results_dir, rows)
    report = _evaluate(challenge, results_dir)
    for k in _NEW_FIELDS:
        assert k not in report, f"per-trace+edp metric wrongly grew {k!r}"
