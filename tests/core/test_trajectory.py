"""Canonical trajectory protocol + the mini adapter (the conversion layer).

These lock the telemetry contract: the schema validates, and mini's native
thread/turn/item stream maps to (thinking, action, observation) steps — including
token usage and submit→sim_result tagging.
"""
import json

from archbench.core import trajectory as T
from runtimes.mini.trajectory_adapter import to_canonical


def test_schema_validate_accepts_good():
    recs = [T.meta("r", "mini", "gemma4", "branch_predictor", "L1"),
            T.step(0, thinking_text="hi", action_kind="submit", action_name="submit",
                   obs_kind="sim_result", submission_id="sub_001", status="ok")]
    assert T.validate(recs) == []


def test_schema_validate_rejects_bad_action_kind():
    recs = [T.meta("r", "mini", None, "c", None),
            T.step(0, action_kind="nonsense")]
    errs = T.validate(recs)
    assert errs and any("action.kind" in e for e in errs)


def test_schema_validate_rejects_missing_meta():
    recs = [T.step(0)]
    assert T.validate(recs)  # record 0 isn't meta


def _mini_fixture(tmp_path):
    events = [
        {"type": "thread.started", "thread": {"id": "t", "created_at": "2026-01-01T00:00:00Z"}},
        {"type": "turn.started", "turn": {"number": 1, "started_at": "2026-01-01T00:00:01Z"}},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "Inspect, then submit."}},
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "tool": "browse_simulator",
                                            "arguments": {"path": "/work"}, "result": "files..."}},
        {"type": "turn.completed", "turn": {"usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}},
        {"type": "turn.started", "turn": {"number": 2, "started_at": "2026-01-01T00:00:05Z"}},
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "tool": "submit",
                                            "arguments": {"implementation_paths": ["candidate.cc"]},
                                            "result": {"submission_id": "sub_001", "status": "queued"}}},
        {"type": "turn.completed", "turn": {"usage": {"prompt_tokens": 150, "completion_tokens": 30, "total_tokens": 180}}},
    ]
    p = tmp_path / "trajectory.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events))
    return p


def test_mini_adapter_maps_thinking_action_observation(tmp_path):
    steps = to_canonical(_mini_fixture(tmp_path))
    assert len(steps) == 2
    # step 0: reasoning -> thinking; tool call -> action+observation; tokens captured
    s0 = steps[0]
    assert "Inspect, then submit." in s0["thinking"]["text"]
    assert s0["thinking"]["tokens"]["prompt"] == 100
    assert s0["action"]["kind"] == "tool_call" and s0["action"]["name"] == "browse_simulator"
    assert s0["observation"]["kind"] == "tool_result"
    # step 1: submit -> action.kind=submit, observation tagged sim_result + submission_id
    s1 = steps[1]
    assert s1["action"]["kind"] == "submit"
    assert s1["observation"]["kind"] == "sim_result"
    assert s1["observation"]["submission_id"] == "sub_001"


def test_mini_adapter_output_validates(tmp_path):
    steps = to_canonical(_mini_fixture(tmp_path))
    assert T.validate([T.meta("r", "mini", "gemma4", "c", "L1"), *steps]) == []


# --- the protocol is enforced by CODE, not just documented -------------------

def test_write_rejects_invalid_trajectory(tmp_path):
    """T.write validates BEFORE writing, so a runtime adapter physically cannot
    emit a non-conforming canonical trajectory — it raises, never silently writes
    garbage. This is the code that MAINTAINS the protocol (not a convention)."""
    import pytest
    bad_step = {"kind": "step", "step": 0, "thinking": {"text": "", "tokens": {}},
                "action": {"kind": "nonsense"}, "observation": {"kind": "none"}}
    with pytest.raises(ValueError):
        T.write(T.meta("r", "mini", None, "c", None), [bad_step], tmp_path / "x.jsonl")
    assert not (tmp_path / "x.jsonl").exists()  # rejected -> nothing written


def test_base_adapter_raises_not_silent():
    """A runtime that forgets its adapter inherits the base, which RAISES — so a
    missing adapter is LOUD, never a silent no-op that drops telemetry."""
    import pytest
    from archbench.core.runtime_base import AgentRuntime

    class _Fake:
        name = "fake"
    with pytest.raises(NotImplementedError):
        AgentRuntime.to_canonical_trajectory(_Fake(), "/tmp/whatever")
