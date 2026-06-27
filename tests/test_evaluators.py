"""Post-session evaluator framework — unit tests.

Covers:
  * BaseEvaluator registry lookup (get_evaluator).
  * simulator_metric: bypass-via-submit_outcomes.jsonl path.
  * simulator_metric: re-eval path (mocked subprocess.run for evaluate.sh).
  * deliverable_files: all present + judge mocked to 1, one missing.
  * trajectory_audit: fixture trajectory with one check_storage call.
  * Failure isolation: one evaluator raising must not stop the others;
    a partial report (error + traceback) is written for the failure.
  * Challenge loader: `evaluations:` block normalizes; missing block warns
    and produces an empty list (back-compat).

All LLM calls are mocked — no network. Real-API integration is covered
implicitly via the smoke probe in the run-script, not in the unit suite.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from archbench.core.challenge import load_challenge
from archbench.evaluators import BaseEvaluator, get_evaluator
from archbench.evaluators.base import _parse_judge_json, judge


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_returns_subclass_instance():
    for name in ("simulator_metric", "deliverable_files", "trajectory_audit",
                 "cross_sim_discrepancy"):
        ev = get_evaluator(name)
        assert isinstance(ev, BaseEvaluator)
        assert ev.name == name


def test_registry_unknown_raises_keyerror():
    with pytest.raises(KeyError):
        get_evaluator("does_not_exist_xyz")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_challenge_dir(tmp_path: Path, with_evaluations: bool = True) -> Path:
    """Create a minimal challenge dir we can load_challenge() on."""
    chdir = tmp_path / "challenges" / "fake_challenge"
    chdir.mkdir(parents=True)
    starter = chdir / "starter"
    starter.mkdir()
    (starter / "thing.cc").write_text("// stub")
    eval_block = ""
    if with_evaluations:
        eval_block = (
            "evaluations:\n"
            "  - evaluator: simulator_metric\n"
            "    bypass_if_present: submit_outcomes.jsonl\n"
            "    config:\n"
            "      reference: baseline.json\n"
        )
    cyaml = (
        "id: fake_challenge\n"
        "name: fake\n"
        "simulator: champsim\n"
        "prompt: 'do the thing'\n"
        "input:\n"
        "  starter_files: [thing.cc]\n"
        "output:\n"
        "  files: [thing.cc]\n"
        "eval:\n"
        "  baseline: baseline.json\n"
        "  max_submissions: 1\n"
        f"{eval_block}"
    )
    (chdir / "challenge.yaml").write_text(cyaml)
    return chdir


def _write_baseline(challenge_dir: Path) -> None:
    baseline = {
        "average_ipc": 0.5,
        "per_trace": [
            {"trace": "t1", "ipc": 0.4, "llc_hit_rate": 0.2},
            {"trace": "t2", "ipc": 0.6, "llc_hit_rate": 0.3},
        ],
    }
    (challenge_dir / "baseline.json").write_text(json.dumps(baseline))


# ---------------------------------------------------------------------------
# simulator_metric — bypass path
# ---------------------------------------------------------------------------


def test_simulator_metric_bypass_reads_submit_outcomes(tmp_path: Path):
    chdir = _make_challenge_dir(tmp_path)
    _write_baseline(chdir)
    challenge = load_challenge(chdir)

    results_dir = tmp_path / "results" / "run1"
    results_dir.mkdir(parents=True)
    # One SIM_OK row with a metric block matching the baseline's traces.
    row = {
        "submission_id": "sub_001",
        "status": "done",
        "outcome": "sim_ok",
        "metric": {
            "ipc": 0.6,
            "_per_trace": [
                {"trace": "t1", "ipc": 0.8, "llc_hit_rate": 0.5},
                {"trace": "t2", "ipc": 0.9, "llc_hit_rate": 0.55},
            ],
        },
    }
    (results_dir / "submit_outcomes.jsonl").write_text(json.dumps(row) + "\n")

    ev = get_evaluator("simulator_metric")
    report = ev.evaluate(
        challenge, results_dir,
        {"bypass_if_present": "submit_outcomes.jsonl", "reference": "baseline.json"},
    )
    assert report["ok"] is True
    assert report["source"] == "submit_outcomes_jsonl"
    # t1 speedup = 0.8/0.4 = 2.0; t2 speedup = 0.9/0.6 = 1.5
    # geomean = sqrt(2.0 * 1.5) ≈ 1.732
    assert report["geomean_speedup"] == pytest.approx(
        (2.0 * 1.5) ** 0.5, rel=1e-3,
    )
    assert report["per_trace_ipc"]["t1"] == 0.8
    assert report["per_trace_hit_rate"]["t2"] == 0.55


def test_simulator_metric_picks_last_sim_ok_row(tmp_path: Path):
    """Two submits — the SECOND should win the speedup calc."""
    chdir = _make_challenge_dir(tmp_path)
    _write_baseline(chdir)
    challenge = load_challenge(chdir)
    results_dir = tmp_path / "results" / "run1"
    results_dir.mkdir(parents=True)
    row1 = {
        "submission_id": "sub_001", "status": "done", "outcome": "sim_ok",
        "metric": {"_per_trace": [{"trace": "t1", "ipc": 0.5}]},
    }
    row2 = {
        "submission_id": "sub_002", "status": "done", "outcome": "sim_ok",
        "metric": {"_per_trace": [{"trace": "t1", "ipc": 0.8}]},
    }
    (results_dir / "submit_outcomes.jsonl").write_text(
        json.dumps(row1) + "\n" + json.dumps(row2) + "\n"
    )
    ev = get_evaluator("simulator_metric")
    report = ev.evaluate(challenge, results_dir, {"bypass_if_present": "submit_outcomes.jsonl"})
    assert report["per_trace_ipc"]["t1"] == 0.8


# ---------------------------------------------------------------------------
# simulator_metric — re-eval path
# ---------------------------------------------------------------------------


def test_simulator_metric_reevaluate_mocked(tmp_path: Path):
    chdir = _make_challenge_dir(tmp_path)
    _write_baseline(chdir)
    # Add an evaluate.sh so the loader sees it exists.
    (chdir / "evaluate.sh").write_text("#!/bin/bash\necho fake")
    challenge = load_challenge(chdir)

    results_dir = tmp_path / "results" / "run1"
    workspace = results_dir / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "thing.cc").write_text("// agent impl")

    # No submit_outcomes.jsonl → fallback path. Mock subprocess.run to
    # return the aggregate-shape stdout aggregate.py would have emitted.
    aggregate = [{
        "name": "AggregatedMultiWorkload",
        "_ipc_cycle_weighted": 0.75,
        "_per_trace": [
            {"trace": "t1", "ipc": 0.5, "llc_hit_rate": 0.3},
            {"trace": "t2", "ipc": 0.9, "llc_hit_rate": 0.5},
        ],
    }]
    class FakeCompleted:
        returncode = 0
        stdout = json.dumps(aggregate)
        stderr = ""

    with patch("evaluators.simulator_metric.evaluator.subprocess.run",
               return_value=FakeCompleted()):
        ev = get_evaluator("simulator_metric")
        report = ev.evaluate(challenge, results_dir, {})
    assert report["ok"] is True
    assert report["source"] == "evaluate_sh"
    # t1: 0.5/0.4=1.25; t2: 0.9/0.6=1.5; geomean ≈ 1.369
    assert report["geomean_speedup"] == pytest.approx(
        (1.25 * 1.5) ** 0.5, rel=1e-3,
    )


def test_simulator_metric_no_evaluate_sh_and_no_bypass(tmp_path: Path):
    chdir = _make_challenge_dir(tmp_path)
    _write_baseline(chdir)
    challenge = load_challenge(chdir)
    results_dir = tmp_path / "results" / "run1"
    results_dir.mkdir(parents=True)
    ev = get_evaluator("simulator_metric")
    report = ev.evaluate(challenge, results_dir, {"bypass_if_present": "submit_outcomes.jsonl"})
    assert report["ok"] is False
    assert "no bypass file and no evaluate.sh" in report.get("error", "")


# ---------------------------------------------------------------------------
# deliverable_files
# ---------------------------------------------------------------------------


def test_deliverable_files_all_present_with_judge(tmp_path: Path):
    chdir = _make_challenge_dir(tmp_path, with_evaluations=False)
    challenge = load_challenge(chdir)
    results_dir = tmp_path / "results" / "run1"
    workspace = results_dir / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "design_principles.md").write_text("D" * 500)
    (workspace / "workload_analysis.md").write_text("W" * 500)

    cfg = {
        "required_files": ["design_principles.md", "workload_analysis.md"],
        "min_chars": 200,
        "llm_judge": "0_or_1",
        "llm_judge_prompts": {
            "design_principles.md": "judge design",
            "workload_analysis.md": "judge workload",
        },
    }
    with patch(
        "evaluators.deliverable_files.evaluator.judge",
        return_value={"score": 1, "rationale": "good"},
    ) as mock_judge:
        ev = get_evaluator("deliverable_files")
        report = ev.evaluate(challenge, results_dir, cfg)

    assert mock_judge.call_count == 2
    assert report["ok"] is True
    for fname in ("design_principles.md", "workload_analysis.md"):
        e = report["per_file"][fname]
        assert e["exists"] is True
        assert e["passes_min_chars"] is True
        assert e["judge_score"] == 1
        assert e["judge_rationale"] == "good"


def test_deliverable_files_one_missing(tmp_path: Path):
    chdir = _make_challenge_dir(tmp_path, with_evaluations=False)
    challenge = load_challenge(chdir)
    results_dir = tmp_path / "results" / "run1"
    workspace = results_dir / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "design_principles.md").write_text("D" * 500)
    # workload_analysis.md NOT created

    cfg = {
        "required_files": ["design_principles.md", "workload_analysis.md"],
        "min_chars": 200,
        "llm_judge": "0_or_1",
        "llm_judge_prompts": {
            "design_principles.md": "judge design",
            "workload_analysis.md": "judge workload",
        },
    }
    with patch(
        "evaluators.deliverable_files.evaluator.judge",
        return_value={"score": 1, "rationale": "good"},
    ) as mock_judge:
        ev = get_evaluator("deliverable_files")
        report = ev.evaluate(challenge, results_dir, cfg)

    # Judge called for the present file only, NOT for the missing one.
    assert mock_judge.call_count == 1
    assert report["ok"] is False
    assert report["per_file"]["workload_analysis.md"]["exists"] is False
    assert report["per_file"]["workload_analysis.md"]["judge_score"] is None
    assert report["per_file"]["design_principles.md"]["exists"] is True


def test_deliverable_files_too_short_skips_judge(tmp_path: Path):
    chdir = _make_challenge_dir(tmp_path, with_evaluations=False)
    challenge = load_challenge(chdir)
    results_dir = tmp_path / "results" / "run1"
    workspace = results_dir / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "design_principles.md").write_text("tiny")  # 4 chars

    cfg = {
        "required_files": ["design_principles.md"],
        "min_chars": 200,
        "llm_judge": "0_or_1",
        "llm_judge_prompts": {"design_principles.md": "judge"},
    }
    with patch(
        "evaluators.deliverable_files.evaluator.judge",
        return_value={"score": 1, "rationale": "x"},
    ) as mock_judge:
        ev = get_evaluator("deliverable_files")
        report = ev.evaluate(challenge, results_dir, cfg)
    assert mock_judge.call_count == 0
    e = report["per_file"]["design_principles.md"]
    assert e["exists"] is True
    assert e["passes_min_chars"] is False
    assert e["judge_score"] is None


# ---------------------------------------------------------------------------
# trajectory_audit
# ---------------------------------------------------------------------------


def test_trajectory_audit_reads_canonical(tmp_path: Path):
    """trajectory_audit reads ONLY trajectory.canonical.jsonl (the normalized
    schema). Per-runtime parsing now lives in each runtime's adapter (tested in
    tests/core/test_trajectory.py) — the old dual-schema parser zoo is gone."""
    from archbench.core import trajectory as T
    chdir = _make_challenge_dir(tmp_path, with_evaluations=False)
    challenge = load_challenge(chdir)
    results_dir = tmp_path / "results" / "run1"
    results_dir.mkdir(parents=True)
    steps = [
        T.step(0, thinking_text="check storage first", action_kind="bash",
               action_name="python3 check_storage.py /workspace --budget 256",
               obs_kind="tool_result", status="completed"),
        T.step(1, action_kind="bash", action_name="ls /workspace",
               obs_kind="tool_result", status="completed"),
        T.step(2, action_kind="tool_call", action_name="browse_simulator",
               action_args={"path": "/sim"}, obs_kind="tool_result", status="ok"),
        T.step(3, action_kind="submit", action_name="submit",
               action_args={"implementation_paths": ["foo.cc"]},
               obs_kind="sim_result", submission_id="sub_001", status="ok"),
    ]
    T.write(T.meta("r", "mini", "gemma4", challenge.id, "L1"), steps,
            results_dir / "trajectory.canonical.jsonl")

    def fake_judge(prompt, context=None, **kw):
        return {"score": 1,
                "invocation_count": context["trajectory_summary"]["check_storage_calls"],
                "rationale": "ok"}

    cfg = {"llm_judge": "0_or_1",
           "checks": {"storage_budget_handling": {"prompt": "count check_storage invocations"}}}
    with patch("evaluators.trajectory_audit.evaluator.judge", side_effect=fake_judge):
        report = get_evaluator("trajectory_audit").evaluate(challenge, results_dir, cfg)
    assert report["ok"] is True
    s = report["trajectory_summary"]
    assert s["bash_calls"] == 2
    assert s["check_storage_calls"] == 1
    assert s["submit_calls"] == 1
    assert s["other_tool_calls"].get("browse_simulator") == 1
    assert report["checks"]["storage_budget_handling"]["invocation_count"] == 1


def test_trajectory_audit_no_canonical_is_explicit(tmp_path: Path):
    """No canonical trajectory -> a clear 'needs an adapter' result (not a crash,
    not a silent native re-parse)."""
    chdir = _make_challenge_dir(tmp_path, with_evaluations=False)
    challenge = load_challenge(chdir)
    results_dir = tmp_path / "results" / "run1"
    results_dir.mkdir(parents=True)
    cfg = {"llm_judge": "0_or_1", "checks": {"x": {"prompt": "p"}}}
    report = get_evaluator("trajectory_audit").evaluate(challenge, results_dir, cfg)
    assert report["ok"] is False
    assert "canonical" in report["error"]


def test_failure_in_one_evaluator_does_not_block_others(tmp_path: Path):
    """Drive the session-finally logic directly via _run_post_session_evaluators."""
    from archbench.runtimes.session import _run_post_session_evaluators

    chdir = _make_challenge_dir(tmp_path, with_evaluations=False)
    # Hand-build a challenge with two evaluator entries: first will raise,
    # second will succeed. We do this by injecting a fake evaluator name
    # into the registry via patch.
    challenge = load_challenge(chdir)
    results_dir = tmp_path / "results" / "run1"
    results_dir.mkdir(parents=True)
    challenge.evaluations = [
        {"evaluator": "boom", "config": {}},
        {"evaluator": "ok", "config": {}},
    ]

    class Boom(BaseEvaluator):
        name = "boom"
        def evaluate(self, ch, rd, cfg):
            raise RuntimeError("kaboom")

    class Ok(BaseEvaluator):
        name = "ok"
        def evaluate(self, ch, rd, cfg):
            return {"ok": True, "note": "second ran"}

    def fake_get(name):
        return {"boom": Boom, "ok": Ok}[name]()

    with patch("archbench.evaluators.get_evaluator", side_effect=fake_get):
        _run_post_session_evaluators(challenge, results_dir)

    boom = json.loads((results_dir / "eval_boom.json").read_text())
    assert boom["ok"] is False
    assert "kaboom" in boom["error"]
    assert "traceback" in boom
    ok = json.loads((results_dir / "eval_ok.json").read_text())
    assert ok["ok"] is True
    assert ok["note"] == "second ran"


# ---------------------------------------------------------------------------
# Challenge loader: evaluations: block
# ---------------------------------------------------------------------------


def test_challenge_load_normalizes_evaluations(tmp_path: Path):
    chdir = _make_challenge_dir(tmp_path, with_evaluations=True)
    challenge = load_challenge(chdir)
    assert len(challenge.evaluations) == 1
    e = challenge.evaluations[0]
    assert e["evaluator"] == "simulator_metric"
    assert e["config"]["reference"] == "baseline.json"
    # bypass_if_present at top of entry was also mirrored into config.
    assert e["config"]["bypass_if_present"] == "submit_outcomes.jsonl"


def test_challenge_load_no_evaluations_warns(tmp_path: Path, caplog):
    chdir = _make_challenge_dir(tmp_path, with_evaluations=False)
    import logging as _logging
    with caplog.at_level(_logging.WARNING, logger="archbench.challenge"):
        challenge = load_challenge(chdir)
    assert challenge.evaluations == []
    assert any("no `evaluations:`" in rec.message for rec in caplog.records)


def test_challenge_load_evaluation_yaml_sibling(tmp_path: Path):
    chdir = _make_challenge_dir(tmp_path, with_evaluations=False)
    (chdir / "evaluation.yaml").write_text(
        "evaluations:\n"
        "  - evaluator: deliverable_files\n"
        "    config: {required_files: [a.md]}\n"
    )
    challenge = load_challenge(chdir)
    assert len(challenge.evaluations) == 1
    assert challenge.evaluations[0]["evaluator"] == "deliverable_files"


# ---------------------------------------------------------------------------
# judge helper degradation
# ---------------------------------------------------------------------------


def test_judge_no_backend_returns_none(monkeypatch):
    """Pure unit: judge() with no API key and no usable proxy gives null.

    Must be hermetic: judge() reads keys from os.environ AND from the
    repo .env FILE via read_env. A developer with a live
    VECTORENGINE_API_KEY / ANTHROPIC_API_KEY in .env would otherwise make
    this test hit a real backend (it did — vectorengine answered a
    non-JSON pleasantry → "not parseable" instead of "no judge
    configured"). Clear every backend env var AND stub read_env to None
    so the .env file can't leak a working key into the unit test.
    """
    monkeypatch.delenv("VECTORENGINE_API_KEY", raising=False)
    monkeypatch.delenv("VECTORENGINE_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ARCHBENCH_JUDGE_MODEL", raising=False)
    monkeypatch.delenv("ARCHBENCH_PROXY_URL", raising=False)
    # judge() does `from archbench.core.env_file import read_env` at call time;
    # patch the source module so the .env file is not consulted.
    monkeypatch.setattr("archbench.core.env_file.read_env", lambda *a, **k: None)
    verdict = judge("test prompt", context={"k": "v"})
    assert verdict["score"] is None
    assert verdict["rationale"] == "no judge configured"


def test_judge_parses_fenced_json():
    text = "Here's my verdict:\n```json\n{\"score\": 1, \"rationale\": \"ok\"}\n```\n"
    parsed = _parse_judge_json(text)
    assert parsed == {"score": 1, "rationale": "ok"}


def test_judge_parses_bare_json():
    text = "{\"score\": 0, \"rationale\": \"nope\"}"
    parsed = _parse_judge_json(text)
    assert parsed == {"score": 0, "rationale": "nope"}


def test_judge_parses_greedy_after_prose():
    text = "Some prose. {\"score\": 1, \"rationale\": \"y\"} trailing"
    parsed = _parse_judge_json(text)
    assert parsed == {"score": 1, "rationale": "y"}


# ---------------------------------------------------------------------------
# offline_sim_calibration
# ---------------------------------------------------------------------------


def _write_offline_sim_real_outcomes(results_dir: Path, real_ipcs: dict) -> None:
    """Drop a submit_outcomes.jsonl carrying the given per-trace IPCs."""
    row = {
        "submission_id": "sub_001",
        "status": "done",
        "outcome": "sim_ok",
        "metric": {
            "_per_trace": [
                {"trace": t, "ipc": float(v)} for t, v in real_ipcs.items()
            ],
        },
    }
    (results_dir / "submit_outcomes.jsonl").write_text(json.dumps(row) + "\n")


def _write_decoded_traces(decoded_root: Path, names: list[str]) -> None:
    """Create empty-but-valid decoded trace files at the canonical path."""
    decoded_root.mkdir(parents=True, exist_ok=True)
    for n in names:
        # Just enough content that the agent sim's open() succeeds.
        (decoded_root / f"{n}.trace.txt").write_text(
            "# header\n0\tdeadbeef\t0\t0\tnone\tnone\t0x100,0x200\t0x0\n"
        )


def test_offline_sim_calibration_no_sim_file(tmp_path: Path):
    chdir = _make_challenge_dir(tmp_path, with_evaluations=False)
    challenge = load_challenge(chdir)
    results_dir = tmp_path / "results" / "run1"
    (results_dir / "workspace").mkdir(parents=True)
    _write_offline_sim_real_outcomes(results_dir, {"t1": 1.0})

    ev = get_evaluator("offline_sim_calibration")
    report = ev.evaluate(challenge, results_dir, {})
    assert report["ok"] is False
    assert "no offline simulator" in report["reason"]


def test_offline_sim_calibration_predicts_calibrated(tmp_path: Path, monkeypatch):
    """Agent sim with predict_ipc(trace) -> real*0.9 + 0.05 → MAE predictable."""
    chdir = _make_challenge_dir(tmp_path, with_evaluations=False)
    # Baseline so direction_accuracy has signal.
    baseline = {
        "average_ipc": 0.5,
        "per_trace": [
            {"trace": "t1", "ipc": 0.5},
            {"trace": "t2", "ipc": 0.5},
        ],
    }
    (chdir / "baseline.json").write_text(json.dumps(baseline))
    challenge = load_challenge(chdir)

    results_dir = tmp_path / "results" / "run1"
    workspace = results_dir / "workspace"
    workspace.mkdir(parents=True)
    real = {"t1": 1.00, "t2": 0.40}
    _write_offline_sim_real_outcomes(results_dir, real)

    # Decoded traces at the canonical location: workload_pools under repo
    # root (= tmp_path / "challenges" / .. → tmp_path).
    decoded_root = tmp_path / "workload_pools" / "champsim" / "decoded"
    _write_decoded_traces(decoded_root, list(real.keys()))

    # Agent sim — module-level predict_ipc(path).
    sim_src = (
        "import os\n"
        "def predict_ipc(trace_path):\n"
        "    # Look the file up in REAL by basename; emulate real*0.9 + 0.05\n"
        "    REAL = {'t1': 1.00, 't2': 0.40}\n"
        "    base = os.path.basename(trace_path).replace('.trace.txt','')\n"
        "    return REAL[base] * 0.9 + 0.05\n"
    )
    (workspace / "sim_test.py").write_text(sim_src)

    ev = get_evaluator("offline_sim_calibration")
    report = ev.evaluate(challenge, results_dir, {})

    assert report["ok"] is True, report
    assert report["interface"] == "predict_ipc"
    # Predicted: t1=0.95 (real 1.00 → err 0.05), t2=0.41 (real 0.40 → err 0.01)
    # MAE = (0.05 + 0.01)/2 = 0.03
    assert report["mean_absolute_error"] == pytest.approx(0.03, abs=1e-6)
    assert report["best_predicted_trace"] == "t2"
    assert report["worst_predicted_trace"] == "t1"
    # Direction: baseline=0.5; t1 real>0.5 AND pred>0.5 (sign correct),
    #            t2 real<0.5 AND pred<0.5 (sign correct). 2/2.
    assert report["direction_accuracy"] == 1.0
    for t in ("t1", "t2"):
        e = report["per_trace_error"][t]
        assert e["predicted"] is not None
        assert e["abs_error"] is not None
        assert e["error"] is None


def test_offline_sim_calibration_import_error(tmp_path: Path):
    """Agent sim that ImportErrors → ok=false with interface mismatch."""
    chdir = _make_challenge_dir(tmp_path, with_evaluations=False)
    challenge = load_challenge(chdir)
    results_dir = tmp_path / "results" / "run1"
    workspace = results_dir / "workspace"
    workspace.mkdir(parents=True)
    _write_offline_sim_real_outcomes(results_dir, {"t1": 0.5})
    # Decoded traces ARE present — we want the import to be the failure path.
    decoded_root = tmp_path / "workload_pools" / "champsim" / "decoded"
    _write_decoded_traces(decoded_root, ["t1"])

    # Module that raises on import AND exposes no recognized interface.
    (workspace / "sim_test.py").write_text(
        "import nonexistent_module_xyz_should_fail  # noqa\n"
    )
    ev = get_evaluator("offline_sim_calibration")
    report = ev.evaluate(challenge, results_dir, {})
    # The loader caught the ImportError, found no interface, reported mismatch.
    assert report["ok"] is False
    assert "interface" in report
    assert "mismatch" in report["reason"] or "interface" in report["reason"]


def test_offline_sim_calibration_no_real_ipc(tmp_path: Path):
    """No submit_outcomes.jsonl + no reeval_result.json → ok=false."""
    chdir = _make_challenge_dir(tmp_path, with_evaluations=False)
    challenge = load_challenge(chdir)
    results_dir = tmp_path / "results" / "run1"
    workspace = results_dir / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "sim_test.py").write_text("def predict_ipc(p): return 1.0\n")

    ev = get_evaluator("offline_sim_calibration")
    report = ev.evaluate(challenge, results_dir, {})
    assert report["ok"] is False
    assert report["class"] == "no_ground_truth"
    assert "no real per-trace metric" in report["reason"]


def test_offline_sim_calibration_decoded_traces_missing(tmp_path: Path):
    """sim_file present + real ipcs present, but decoded traces missing →
    ok=false with the appropriate reason."""
    chdir = _make_challenge_dir(tmp_path, with_evaluations=False)
    challenge = load_challenge(chdir)
    results_dir = tmp_path / "results" / "run1"
    workspace = results_dir / "workspace"
    workspace.mkdir(parents=True)
    _write_offline_sim_real_outcomes(results_dir, {"t1": 0.5})
    (workspace / "sim_test.py").write_text("def predict_ipc(p): return 1.0\n")
    # No decoded traces written → evaluator should say so.
    ev = get_evaluator("offline_sim_calibration")
    report = ev.evaluate(challenge, results_dir, {"per_trace_timeout_s": 30})
    assert report["ok"] is False
    assert "decoded traces" in report["reason"]
    assert "real_per_trace_ipc" in report


# ---------------------------------------------------------------------------
# cross_sim_discrepancy (first multi-sim / Tier-3 evaluator)
# ---------------------------------------------------------------------------


def _make_multi_sim_challenge_dir(tmp_path: Path) -> Path:
    """A 2-sim challenge dir (simulators: [dramsys, ramulator])."""
    chdir = tmp_path / "challenges" / "xval"
    chdir.mkdir(parents=True)
    starter = chdir / "starter"
    starter.mkdir()
    (starter / "config.json").write_text("{}")
    (chdir / "challenge.yaml").write_text(
        "id: xval\n"
        "name: xval\n"
        "simulators: [dramsys, ramulator]\n"
        "prompt: ''\n"
        "input:\n  starter_files: [config.json]\n"
        "output:\n  files: [config.json]\n"
        "eval:\n  baseline: evaluation/baseline.json\n  max_submissions: 4\n"
    )
    # A baseline with a starter discrepancy so the evaluator can echo it.
    evald = chdir / "evaluation"
    evald.mkdir()
    (evald / "baseline.json").write_text(json.dumps({"discrepancy_pct": 13.93}))
    return chdir


def _write_xval_rows(results_dir: Path, rows: list) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "submit_outcomes.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n"
    )


def test_cross_sim_discrepancy_both_present(tmp_path: Path):
    """Both sims have a SIM_OK bandwidth → discrepancy vs reference sim."""
    chdir = _make_multi_sim_challenge_dir(tmp_path)
    challenge = load_challenge(chdir)
    results_dir = tmp_path / "results" / "run1"
    _write_xval_rows(results_dir, [
        {"submission_id": "dramsys_sub_001", "outcome": "sim_ok",
         "metric": {"bandwidth_gbps": 72.49}},
        {"submission_id": "ramulator_sub_001", "outcome": "sim_ok",
         "metric": {"bandwidth_gbps": 62.39}},
    ])
    ev = get_evaluator("cross_sim_discrepancy")
    r = ev.evaluate(challenge, results_dir, {
        "metric_field": "bandwidth_gbps", "reference_sim": "dramsys",
    })
    assert r["ok"] and r["complete"]
    assert abs(r["discrepancy_pct"] - 13.9329) < 0.01
    assert r["score"] == r["discrepancy_pct"]
    assert r["direction"] == "lower_is_better"
    assert r["per_sim_metric"] == {"dramsys": 72.49, "ramulator": 62.39}
    assert abs(r["starter_discrepancy_pct"] - 13.93) < 0.01


def test_cross_sim_discrepancy_takes_last_sim_ok_per_sim(tmp_path: Path):
    """Iteration: the LAST SIM_OK per sim is the scored one."""
    chdir = _make_multi_sim_challenge_dir(tmp_path)
    challenge = load_challenge(chdir)
    results_dir = tmp_path / "results" / "run1"
    _write_xval_rows(results_dir, [
        {"submission_id": "dramsys_sub_001", "outcome": "sim_ok",
         "metric": {"bandwidth_gbps": 72.49}},
        {"submission_id": "ramulator_sub_001", "outcome": "sim_ok",
         "metric": {"bandwidth_gbps": 62.39}},
        {"submission_id": "ramulator_sub_002", "outcome": "sim_ok",
         "metric": {"bandwidth_gbps": 70.00}},
    ])
    ev = get_evaluator("cross_sim_discrepancy")
    r = ev.evaluate(challenge, results_dir, {"reference_sim": "dramsys"})
    assert abs(r["discrepancy_pct"] - 3.435) < 0.01
    assert r["per_sim_detail"]["ramulator"]["scored_submission_id"] == "ramulator_sub_002"


def test_cross_sim_discrepancy_partial_is_incomplete_not_crash(tmp_path: Path):
    """Only one sim submitted → ok=True, complete=False, score=None."""
    chdir = _make_multi_sim_challenge_dir(tmp_path)
    challenge = load_challenge(chdir)
    results_dir = tmp_path / "results" / "run1"
    _write_xval_rows(results_dir, [
        {"submission_id": "dramsys_sub_001", "outcome": "sim_ok",
         "metric": {"bandwidth_gbps": 72.49}},
    ])
    ev = get_evaluator("cross_sim_discrepancy")
    r = ev.evaluate(challenge, results_dir, {})
    assert r["ok"] is True
    assert r["complete"] is False
    assert r["score"] is None
    assert "ramulator" in r["reason"]


def test_cross_sim_discrepancy_defaults_sims_from_challenge(tmp_path: Path):
    """No `sims` in config → derived from challenge.simulators, primary = ref."""
    chdir = _make_multi_sim_challenge_dir(tmp_path)
    challenge = load_challenge(chdir)
    results_dir = tmp_path / "results" / "run1"
    _write_xval_rows(results_dir, [
        {"submission_id": "dramsys_sub_001", "outcome": "sim_ok",
         "metric": {"bandwidth_gbps": 72.49}},
        {"submission_id": "ramulator_sub_001", "outcome": "sim_ok",
         "metric": {"bandwidth_gbps": 62.39}},
    ])
    ev = get_evaluator("cross_sim_discrepancy")
    r = ev.evaluate(challenge, results_dir, {})
    assert r["reference_sim"] == "dramsys"
    assert set(r["per_sim_metric"]) == {"dramsys", "ramulator"}
