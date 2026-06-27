"""[concept: MONITOR] Unit coverage for run_profile — token + wall-clock
distillation over a run archive.

run_profile is pure over the MONITOR archive (no live session): it reads
trajectory.jsonl (token usage), trajectory.canonical.jsonl (ts span) and
submit_outcomes.jsonl (per-submit durations). These tests build a synthetic
archive on disk and assert the derived profile, including the graceful path
when files are missing and the best-effort (never-raise) write contract.
"""
import json
from pathlib import Path

from archbench.core.run_profile import backfill, extract_profile, write_profile


def _write(p: Path, lines):
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def _make_archive(d: Path):
    _write(d / "trajectory.jsonl", [
        {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20,
                                             "cached_input_tokens": 5, "total_tokens": 120}},
        {"type": "turn.completed", "usage": {"input_tokens": 200, "output_tokens": 30,
                                             "cached_input_tokens": 0, "total_tokens": 230}},
        {"type": "tool.call", "name": "submit"},  # non-turn line: ignored by token sum
    ])
    _write(d / "trajectory.canonical.jsonl", [
        {"ts": "2026-06-19T02:30:00"},
        {"ts": "2026-06-19T02:38:20"},  # +500s span
    ])
    _write(d / "submit_outcomes.jsonl", [
        {"submission_id": "sub_001", "outcome": "sim_ok",
         "started_at": 1000.0, "finished_at": 1042.5},
        {"submission_id": "sub_002", "outcome": "agent_missing_artifact",
         "started_at": 2000.0, "finished_at": 2010.0},
    ])


def test_extract_profile_sums_tokens_turns_walls(tmp_path):
    _make_archive(tmp_path)
    p = extract_profile(tmp_path)
    assert p["tokens"] == {"input": 300, "output": 50, "cached_input": 5, "total": 350}
    assert p["turns"] == 2
    assert p["tokens_per_turn_mean"] == 175.0
    assert p["agent_wall_seconds"] == 500.0
    assert p["n_submits"] == 2
    assert p["sim_wall_seconds_total"] == 52.5
    submits = {s["id"]: s for s in p["submits"]}
    assert submits["sub_001"]["seconds"] == 42.5
    assert submits["sub_001"]["outcome"] == "sim_ok"
    assert p["source"] == {"trajectory": True, "canonical": True, "outcomes": True}


def test_extract_profile_empty_dir_is_graceful(tmp_path):
    p = extract_profile(tmp_path)
    assert p["tokens"] == {"input": 0, "output": 0, "cached_input": 0, "total": 0}
    assert p["turns"] == 0
    assert p["tokens_per_turn_mean"] is None
    assert p["agent_wall_seconds"] is None
    assert p["n_submits"] == 0
    assert p["sim_wall_seconds_total"] == 0.0
    assert p["source"] == {"trajectory": False, "canonical": False, "outcomes": False}


def test_write_profile_persists_valid_json(tmp_path):
    _make_archive(tmp_path)
    out = write_profile(tmp_path)
    assert out == tmp_path / "profile.json"
    assert json.loads(out.read_text())["tokens"]["total"] == 350


def test_write_profile_is_best_effort_and_never_raises(tmp_path):
    _make_archive(tmp_path)
    # make the destination unwritable (a directory where a file must go):
    (tmp_path / "profile.json").mkdir()
    assert write_profile(tmp_path) is None  # returns None, does not raise


def test_backfill_walks_nested_run_dirs(tmp_path):
    a = tmp_path / "runA" / "nested"
    b = tmp_path / "runB"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    _make_archive(a)
    _make_archive(b)
    assert backfill(tmp_path) == 2
    assert (a / "profile.json").exists()
    assert (b / "profile.json").exists()
