"""Unit tests: iteration_quality (L1 process) + tool_use_audit (L2 process).

Synthetic fixtures; the real-data validation lives in the replay runs
against the 2026-06-10 campaign dirs (see evaluators/README.md).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from evaluators._base.envelope import EvalClass
from evaluators.iteration_quality.evaluator import IterationQualityEvaluator
from evaluators.tool_use_audit.evaluator import ToolUseAuditEvaluator

CONFIG_IQ = {"metric_key": "mpki", "direction": "lower"}


def _results_with_rows(tmp_path: Path, rows: list[dict]) -> Path:
    rd = tmp_path / "results"
    rd.mkdir()
    (rd / "submit_outcomes.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows))
    return rd


def _ch(max_submissions=10):
    return SimpleNamespace(eval=SimpleNamespace(max_submissions=max_submissions))


# ----------------------------------------------------------- iteration_quality

def test_iteration_quality_improving_run(tmp_path):
    rows = [
        {"outcome": "build_fail"},
        {"outcome": "sim_ok", "metric": {"mpki": 10.0}},
        {"outcome": "sim_ok", "metric": {"mpki": 12.0}},   # worse mid-run
        {"outcome": "sim_ok", "metric": {"mpki": 5.0}},    # best, last
    ]
    rep = IterationQualityEvaluator().evaluate(_ch(), _results_with_rows(tmp_path, rows), CONFIG_IQ)
    assert rep["class"] == EvalClass.SCORED
    assert rep["sim_ok_count"] == 3 and rep["wasted_attempts"] == 1
    assert rep["first_ok"] == 10.0 and rep["best"] == 5.0
    assert rep["improvement_from_first"] == 2.0  # lower-better: 10/5
    assert rep["improved"] is True and rep["best_is_last"] is True
    assert rep["budget_used_fraction"] == 3 / 10


def test_iteration_quality_never_beat_first(tmp_path):
    rows = [
        {"outcome": "sim_ok", "metric": {"mpki": 6.0}},
        {"outcome": "sim_ok", "metric": {"mpki": 7.5}},
    ]
    rep = IterationQualityEvaluator().evaluate(_ch(), _results_with_rows(tmp_path, rows), CONFIG_IQ)
    assert rep["improvement_from_first"] == 1.0
    assert rep["improved"] is False and rep["best_is_last"] is False


def test_iteration_quality_sentinels_only(tmp_path):
    rows = [{"outcome": "sim_ok", "metric": {"mpki": 1e30}}]
    rep = IterationQualityEvaluator().evaluate(_ch(), _results_with_rows(tmp_path, rows), CONFIG_IQ)
    assert rep["class"] == EvalClass.NO_GROUND_TRUTH


def test_iteration_quality_unconfigured(tmp_path):
    rep = IterationQualityEvaluator().evaluate(_ch(), _results_with_rows(tmp_path, []), {})
    assert rep["class"] == EvalClass.NOT_CONFIGURED


# ------------------------------------------------------------- tool_use_audit

def _traj(tmp_path: Path, steps: list[dict]) -> Path:
    rd = tmp_path / "results"
    rd.mkdir()
    lines = [{"kind": "meta", "schema_version": 1}]
    lines += [{"kind": "step", "step": i, **s} for i, s in enumerate(steps)]
    (rd / "trajectory.canonical.jsonl").write_text(
        "".join(json.dumps(x) + "\n" for x in lines))
    return rd


def _bash(cmd):
    return {"action": {"kind": "bash", "name": cmd}}


def _tool(name):
    return {"action": {"kind": "tool_call", "name": name}}


def test_tool_use_audit_dev_loop_before_submit(tmp_path):
    steps = [
        _tool("read_file"),
        _tool("file_change"),
        _bash("./config.sh champsim_config.json && make -j8"),
        _bash("bin/champsim --traces foo.xz"),
        _tool("file_change"),
        _bash("make -j8"),
        _bash("bin/champsim --traces foo.xz"),
        _tool("submit_and_wait"),
    ]
    rep = ToolUseAuditEvaluator().evaluate(None, _traj(tmp_path, steps),
                                           {"run_patterns": ["bin/champsim"]})
    assert rep["class"] == EvalClass.SCORED
    assert rep["built_before_first_submit"] is True
    assert rep["ran_real_sim_before_first_submit"] is True
    assert rep["edit_build_run_loops"] == 2
    assert rep["first_submit_step"] == 7 and rep["submits"] == 1


def test_tool_use_audit_edit_without_build(tmp_path):
    """The branch_L2 signature: edits + submits, zero builds."""
    steps = [
        _tool("read_file"), _tool("file_change"),
        _tool("submit_and_wait"), _tool("file_change"), _tool("submit_and_wait"),
    ]
    rep = ToolUseAuditEvaluator().evaluate(None, _traj(tmp_path, steps), {})
    assert rep["build_cmds"] == 0
    assert rep["built_before_first_submit"] is False
    assert rep["submits"] == 2 and rep["file_edits"] == 2


def test_tool_use_audit_no_trajectory_is_infra(tmp_path):
    rd = tmp_path / "results"
    rd.mkdir()
    rep = ToolUseAuditEvaluator().evaluate(None, rd, {})
    assert rep["class"] == EvalClass.EVALUATOR_ERROR
