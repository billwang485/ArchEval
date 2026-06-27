"""SubmitOutcome enum invariants — the budget & retry rules.

These tests are the structural fix for past incidents (legacy commits
fe938ef2, 48313c1f) — keep them green or you've broken the contract.
"""

import pytest

from archbench.core.outcomes import OutcomeReport, SubmitOutcome


def test_only_sim_ok_consumes_budget():
    """Past bug: BUILD_FAIL was counted against the submission budget,
    penalizing the agent for honest experimentation."""
    assert SubmitOutcome.SIM_OK.consumes_budget is True
    assert SubmitOutcome.BUILD_FAIL.consumes_budget is False
    assert SubmitOutcome.VALIDATION_REJECT.consumes_budget is False
    assert SubmitOutcome.SIM_TIMEOUT.consumes_budget is False


def test_sim_timeout_does_not_recommend_retry():
    """Past bug (legacy commit 48313c1f): SIM_TIMEOUT was reported as
    'transient retry', agent resubmitted identical 2h-timeout code 3×,
    burning 6 wall-hours."""
    assert SubmitOutcome.SIM_TIMEOUT.retry_recommended is False


def test_build_and_validation_fail_recommend_retry():
    """These ARE recoverable with a code change, so retry is right."""
    assert SubmitOutcome.BUILD_FAIL.retry_recommended is True
    assert SubmitOutcome.VALIDATION_REJECT.retry_recommended is True


def test_agent_message_prefixes_are_unique():
    prefixes = {o.agent_message_prefix for o in SubmitOutcome}
    assert len(prefixes) == len(list(SubmitOutcome))


def test_agent_message_format_for_sim_ok():
    report = OutcomeReport(
        outcome=SubmitOutcome.SIM_OK,
        metrics={"ipc": 0.5113, "mpki": 12.3, "_per_trace": [...]},
        submit_index=1,
    )
    msg = report.to_agent_message()
    assert msg.startswith("SUBMIT SIM_OK")
    assert "submit_index=1" in msg
    assert "ipc=0.5113" in msg
    assert "mpki=12.3" in msg
    # underscore-prefixed metadata is excluded from agent view
    assert "_per_trace" not in msg


def test_agent_message_for_build_fail():
    report = OutcomeReport(
        outcome=SubmitOutcome.BUILD_FAIL,
        detail="compilation error in candidate.cc:42",
        raw_log_tail="error: 'foo' not declared in this scope",
    )
    msg = report.to_agent_message()
    assert msg.startswith("SUBMIT BUILD_FAIL")
    assert "candidate.cc:42" in msg
    assert "foo" in msg
    # build failures must NOT carry metrics
    assert "ipc=" not in msg


def test_no_outcome_outside_enum():
    """The four enum values are exhaustive. Anything else is a bug."""
    assert {o.value for o in SubmitOutcome} == {
        "build_fail", "validation_reject", "sim_timeout", "sim_ok",
    }
