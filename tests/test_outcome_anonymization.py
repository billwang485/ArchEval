"""Sub-agent review fix: detail/raw strings in OutcomeReport must scrub
through the anonymizer. Without this, an exception message containing
a SPEC trace name (e.g. FileNotFoundError("/.../482.sphinx3-1100B.xz"))
would leak to the agent unscrubbed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archbench.core.anonymizer import Anonymizer
from archbench.core.challenge import Challenge, EvalConfig
from simulators.champsim.connector.server import SubmitContext, _outcome
from archbench.core.outcomes import SubmitOutcome


class _StubPlugin:
    def submission_files(self, _ch): return []
    def default_source_blocklist(self, _ch): return []


def _ctx(anon: Anonymizer, tmp_path: Path) -> SubmitContext:
    ch = Challenge(
        id="t", name="t", simulator="champsim", prompt="",
        starter_files=[], output_files=[],
        eval=EvalConfig(),
        simulator_config={},
        challenge_dir=tmp_path,
    )
    return SubmitContext(
        challenge=ch, challenge_dir=tmp_path,
        plugin=_StubPlugin(),
        agent=None, sim=None,
        anonymizer=anon,
    )


def test_outcome_detail_is_scrubbed(tmp_path):
    """The `detail` field passed to _outcome must be scrubbed before
    reaching OutcomeReport (and the agent)."""
    anon = Anonymizer(forward={"482.sphinx3-1100B": "W003"})
    ctx = _ctx(anon, tmp_path)
    report = _outcome(
        SubmitOutcome.SIM_TIMEOUT,
        detail="Cannot stat /traces/482.sphinx3-1100B.trace.txt",
        ctx=ctx,
    )
    assert "482.sphinx3-1100B" not in report.detail
    assert "W003" in report.detail


def test_outcome_raw_log_tail_is_scrubbed(tmp_path):
    """Same for raw_log_tail — sub-agent caught both leak paths."""
    anon = Anonymizer(forward={"482.sphinx3-1100B": "W003"})
    ctx = _ctx(anon, tmp_path)
    report = _outcome(
        SubmitOutcome.BUILD_FAIL,
        detail="compile error",
        ctx=ctx,
        raw="trace 482.sphinx3-1100B not found in workload pool",
    )
    assert "482.sphinx3-1100B" not in report.raw_log_tail
    assert "W003" in report.raw_log_tail


def test_outcome_with_disabled_anonymizer_passthrough(tmp_path):
    """Disabled anonymizer (no --anonymize flag) → pass-through."""
    ctx = _ctx(Anonymizer.disabled(), tmp_path)
    report = _outcome(
        SubmitOutcome.SIM_TIMEOUT,
        detail="trace 482.sphinx3-1100B missing",
        ctx=ctx,
    )
    assert "482.sphinx3-1100B" in report.detail  # not scrubbed when disabled
