"""MCP handlers (handle_submit / handle_browse / handle_read).

Uses fake Container and SimulatorPlugin so the tests run without docker.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

from archbench.core.anonymizer import Anonymizer
from archbench.core.challenge import Challenge, EvalConfig
from simulators.champsim.connector.server import (
    SubmissionState,
    SubmitContext,
    handle_browse,
    handle_check_submission,
    handle_read,
    handle_session_end,
    handle_submit,
    handle_submit_async,
)
from archbench.core.outcomes import OutcomeReport, SubmitOutcome


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeContainer:
    """Stand-in for ContainerManager: tracks copy_out + list/read calls."""

    name_: str = "fake"
    workspace_files: dict[str, str] = field(default_factory=dict)
    sim_files: dict[str, str] = field(default_factory=dict)
    sim_listings: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.name_

    def copy_out(self, container_path: str, host_path: Path) -> None:
        # Mimics /workspace/<fname>
        fname = container_path.split("/")[-1]
        if fname not in self.workspace_files:
            raise FileNotFoundError(f"no such file in workspace: {fname}")
        host_path.parent.mkdir(parents=True, exist_ok=True)
        host_path.write_text(self.workspace_files[fname])

    def list_files(self, path: str) -> str:
        return self.sim_listings.get(path, f"(no entries for {path})")

    def read_file(self, path: str) -> str:
        if path in self.sim_files:
            return self.sim_files[path]
        return f"ERROR: not found: {path}"


@dataclass
class FakePlugin:
    """Stand-in for SimulatorPlugin: scripted run_submit outcomes."""

    next_raw: str = "SIMULATION_OK\n"
    next_metrics: Optional[dict] = None
    raise_on_submit: Optional[Exception] = None
    submission_files_: list[str] = field(default_factory=lambda: ["main.cc"])

    def submission_files(self, _challenge):
        return list(self.submission_files_)

    def default_source_blocklist(self, _challenge):
        return ["/sim/solution/*"]

    def run_submit(self, _sim, _challenge, _files) -> str:
        if self.raise_on_submit:
            raise self.raise_on_submit
        return self.next_raw

    def parse_output(self, raw_output: str) -> Optional[dict]:
        return self.next_metrics


def _ctx(tmp_path: Path,
         plugin: FakePlugin,
         agent: FakeContainer,
         max_submissions: int = 5,
         max_code_lines: int = 1000,
         source_blocklist: list[str] | None = None,
         anonymizer: Anonymizer | None = None) -> SubmitContext:
    ch = Challenge(
        id="t", name="t", simulator="champsim", prompt="",
        starter_files=["main.cc"], output_files=plugin.submission_files_,
        eval=EvalConfig(
            metric="ipc", max_submissions=max_submissions,
            max_code_lines=max_code_lines,
        ),
        simulator_config={},
        source_blocklist=source_blocklist or [],
        challenge_dir=tmp_path,
    )
    return SubmitContext(
        challenge=ch,
        challenge_dir=tmp_path,
        plugin=plugin,
        agent=agent,
        sim=FakeContainer(name_="sim"),
        anonymizer=anonymizer or Anonymizer.disabled(),
    )


# ---------------------------------------------------------------------------
# handle_submit
# ---------------------------------------------------------------------------


def test_submit_ok_increments_count(tmp_path):
    plugin = FakePlugin(
        next_raw="SIMULATION_OK\n{...}\n",
        next_metrics={"ipc": 0.5113, "mpki": 12.3},
    )
    agent = FakeContainer(workspace_files={"main.cc": "int x;"})
    ctx = _ctx(tmp_path, plugin, agent)

    report = handle_submit(ctx)
    assert report.outcome == SubmitOutcome.SIM_OK
    assert report.metrics["ipc"] == 0.5113
    assert ctx.submit_count == 1
    # second submit increments
    handle_submit(ctx)
    assert ctx.submit_count == 2


def test_submit_build_fail_does_not_count(tmp_path):
    """Past bug fe938ef2: compile failures consumed budget."""
    plugin = FakePlugin(next_raw="Compilation failed: missing header\n",
                         next_metrics=None)
    agent = FakeContainer(workspace_files={"main.cc": "garbage"})
    ctx = _ctx(tmp_path, plugin, agent)

    report = handle_submit(ctx)
    assert report.outcome == SubmitOutcome.BUILD_FAIL
    assert ctx.submit_count == 0
    assert report.outcome.consumes_budget is False


def test_submit_validation_reject_does_not_count_or_leak_details(tmp_path):
    plugin = FakePlugin(
        next_raw=(
            "VALIDATION_FAILED\n"
            "validator detail: used=8192 limit=4096 over_by=4096\n"
        ),
    )
    agent = FakeContainer(workspace_files={"main.cc": "x" * 100})
    ctx = _ctx(tmp_path, plugin, agent)

    report = handle_submit(ctx)
    assert report.outcome == SubmitOutcome.VALIDATION_REJECT
    assert ctx.submit_count == 0
    assert report.raw_log_tail == ""
    assert "8192" not in report.to_agent_message()
    assert "4096" not in report.to_agent_message()


def test_submit_sim_timeout_from_exception(tmp_path):
    """Plugin raises subprocess.TimeoutExpired → SIM_TIMEOUT, not SIM_OK."""
    import subprocess
    plugin = FakePlugin(
        raise_on_submit=subprocess.TimeoutExpired(cmd="x", timeout=7200),
    )
    agent = FakeContainer(workspace_files={"main.cc": "x;"})
    ctx = _ctx(tmp_path, plugin, agent)

    report = handle_submit(ctx)
    assert report.outcome == SubmitOutcome.SIM_TIMEOUT
    assert ctx.submit_count == 0
    # Critical: the outcome must NOT recommend retry (legacy 48313c1f).
    assert report.outcome.retry_recommended is False


def test_submit_code_line_cap_rejects_as_build_fail(tmp_path):
    plugin = FakePlugin()
    big = "\n".join(["//"] * 5000)
    agent = FakeContainer(workspace_files={"main.cc": big})
    ctx = _ctx(tmp_path, plugin, agent, max_code_lines=100)

    report = handle_submit(ctx)
    assert report.outcome == SubmitOutcome.BUILD_FAIL
    assert "line cap" in report.detail
    assert ctx.submit_count == 0


def test_submit_attempt_cap_prevents_infinite_loops(tmp_path):
    """Defensive: cap = max(max_submissions * 10, 30). Generous for
    single-shot challenges where agent writes many deliverables + tests
    before final submit; still bounded against runaway loops."""
    plugin = FakePlugin(next_raw="Compilation failed: ...", next_metrics=None)
    agent = FakeContainer(workspace_files={"main.cc": "garbage"})
    # max_submissions=5 → cap = max(50, 30) = 50
    ctx = _ctx(tmp_path, plugin, agent, max_submissions=5)
    for i in range(50):
        report = handle_submit(ctx)
        assert report.outcome == SubmitOutcome.BUILD_FAIL
    report = handle_submit(ctx)
    assert report.outcome == SubmitOutcome.VALIDATION_REJECT
    assert "attempt_cap_reached" in report.metadata


def test_submit_explicit_paths_validate_workspace_prefix(tmp_path):
    """Agent-supplied implementation_paths must start with /workspace/."""
    plugin = FakePlugin(next_raw="SIMULATION_OK\n", next_metrics={"ipc": 0.5})
    agent = FakeContainer(workspace_files={"main.cc": "x;"})
    ctx = _ctx(tmp_path, plugin, agent)
    report = handle_submit(ctx, implementation_paths=["/etc/passwd"])
    assert report.outcome == SubmitOutcome.BUILD_FAIL
    assert "must be inside /workspace/" in report.detail


def test_submit_explicit_paths_validate_count(tmp_path):
    """Agent must pass exactly len(output_files) paths."""
    plugin = FakePlugin(submission_files_=["main.cc", "main.h"])
    agent = FakeContainer(workspace_files={"main.cc": "x;", "main.h": "y;"})
    ctx = _ctx(tmp_path, plugin, agent)
    report = handle_submit(ctx, implementation_paths=["/workspace/main.cc"])
    assert report.outcome == SubmitOutcome.BUILD_FAIL
    assert "expects 2 path(s)" in report.detail


def test_submit_explicit_paths_validate_basename(tmp_path):
    """Basename of each path must match a declared output file."""
    plugin = FakePlugin(submission_files_=["main.cc"])
    agent = FakeContainer(workspace_files={"main.cc": "x;"})
    ctx = _ctx(tmp_path, plugin, agent)
    report = handle_submit(
        ctx, implementation_paths=["/workspace/wrong_name.cc"],
    )
    assert report.outcome == SubmitOutcome.BUILD_FAIL
    assert "not in expected submission files" in report.detail


def test_submit_no_scaffolding_deliverables_gate(tmp_path):
    """No scaffolding: connector does NOT enforce deliverables/tests.
    Agent submits whatever; judging is post-hoc."""
    plugin = FakePlugin(
        next_raw="SIMULATION_OK\n", next_metrics={"ipc": 0.5113},
    )
    agent = FakeContainer(workspace_files={"main.cc": "int main(){}"})
    ctx = _ctx(tmp_path, plugin, agent)
    report = handle_submit(
        ctx, implementation_paths=["/workspace/main.cc"],
    )
    assert report.outcome == SubmitOutcome.SIM_OK
    assert ctx.submit_count == 1


def test_submit_attempt_cap_floor_for_single_shot(tmp_path):
    """Single-shot challenge (max_submissions=1) still gets ≥30 attempts."""
    plugin = FakePlugin(next_raw="Compilation failed: ...", next_metrics=None)
    agent = FakeContainer(workspace_files={"main.cc": "x;"})
    ctx = _ctx(tmp_path, plugin, agent, max_submissions=1)
    for i in range(30):
        report = handle_submit(ctx)
        assert report.outcome == SubmitOutcome.BUILD_FAIL
    report = handle_submit(ctx)
    assert report.outcome == SubmitOutcome.VALIDATION_REJECT
    assert "attempt_cap_reached" in report.metadata


def test_submit_message_format(tmp_path):
    plugin = FakePlugin(
        next_raw="SIMULATION_OK\n",
        next_metrics={"ipc": 0.5113},
    )
    agent = FakeContainer(workspace_files={"main.cc": "x;"})
    ctx = _ctx(tmp_path, plugin, agent)

    report = handle_submit(ctx)
    msg = report.to_agent_message()
    assert msg.startswith("SUBMIT SIM_OK")
    assert "ipc=0.5113" in msg


def test_submit_outbound_is_anonymized(tmp_path):
    """The raw simulator output that appears in the agent message
    must be scrubbed by the anonymizer."""
    plugin = FakePlugin(
        next_raw="SIMULATION_OK\nLoaded trace 482.sphinx3-1100B for run.\n",
        next_metrics={"ipc": 0.5113},
    )
    agent = FakeContainer(workspace_files={"main.cc": "x;"})
    anon = Anonymizer(forward={"482.sphinx3-1100B": "W003"})
    ctx = _ctx(tmp_path, plugin, agent, anonymizer=anon)

    report = handle_submit(ctx)
    assert "482.sphinx3-1100B" not in report.raw_log_tail
    assert "W003" in report.raw_log_tail


# ---------------------------------------------------------------------------
# handle_browse / handle_read — blocklist enforcement
# ---------------------------------------------------------------------------


def test_browse_blocklist_blocks_plugin_default(tmp_path):
    plugin = FakePlugin()
    agent = FakeContainer()
    ctx = _ctx(tmp_path, plugin, agent)
    out = handle_browse(ctx, "/sim/solution/secret")
    assert "blocked by source_blocklist" in out


def test_browse_blocklist_blocks_challenge_specific(tmp_path):
    plugin = FakePlugin()
    agent = FakeContainer()
    agent_sim = FakeContainer(sim_listings={"/sim/api": "(api files)"})
    ctx = _ctx(tmp_path, plugin, agent,
               source_blocklist=["/sim/api/secret/*"])
    ctx.sim = agent_sim
    out = handle_browse(ctx, "/sim/api/secret/whatever")
    assert "blocked" in out


def test_browse_passes_through_unblocked(tmp_path):
    plugin = FakePlugin()
    sim_box = FakeContainer(sim_listings={"/api": "ls of /api"})
    ctx = _ctx(tmp_path, plugin, FakeContainer())
    ctx.sim = sim_box
    out = handle_browse(ctx, "/api")
    assert "ls of /api" in out


def test_read_blocklist_enforced_same_as_browse(tmp_path):
    plugin = FakePlugin()
    sim_box = FakeContainer(sim_files={"/sim/solution/answer.cc": "secret"})
    ctx = _ctx(tmp_path, plugin, FakeContainer())
    ctx.sim = sim_box
    out = handle_read(ctx, "/sim/solution/answer.cc")
    assert "blocked" in out
    assert "secret" not in out


def test_browse_rejects_relative_path(tmp_path):
    """Phase H: relative paths like ``inc/modules.h`` would silently
    resolve against the container's cwd (``/``), giving ``No such file``.
    Reject up-front with a hint about ``/work/runtimes/champsim/``."""
    plugin = FakePlugin()
    sim_box = FakeContainer(sim_listings={"/work/runtimes/champsim/inc": "stuff"})
    ctx = _ctx(tmp_path, plugin, FakeContainer())
    ctx.sim = sim_box
    out = handle_browse(ctx, "inc")
    assert "relative path" in out
    assert "/work/runtimes/champsim" in out


def test_read_rejects_relative_path(tmp_path):
    """Same rationale as test_browse_rejects_relative_path."""
    plugin = FakePlugin()
    sim_box = FakeContainer(sim_files={"/work/runtimes/champsim/inc/modules.h": "..."})
    ctx = _ctx(tmp_path, plugin, FakeContainer())
    ctx.sim = sim_box
    out = handle_read(ctx, "inc/modules.h")
    assert "relative path" in out
    assert "/work/runtimes/champsim" in out


# handle_challenge_info was removed in P6 — agents read the prompt
# directly (injected as first user message) or via /workspace/prompt.md
# if their runtime writes it.


# ---------------------------------------------------------------------------
# Async submit lifecycle: handle_submit_async + handle_check_submission
# ---------------------------------------------------------------------------


def _wait_for_done(ctx: SubmitContext, sid: str, timeout: float = 5.0) -> dict:
    """Block until handle_check_submission reports status=done, or timeout."""
    import time as _t
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        state = handle_check_submission(ctx, sid)
        if state.get("status") in ("done", "unknown"):
            return state
        _t.sleep(0.02)
    raise AssertionError(
        f"check_submission({sid!r}) did not reach done within {timeout}s; "
        f"last={handle_check_submission(ctx, sid)}"
    )


def test_submit_async_returns_queued_immediately(tmp_path):
    """submit returns {submission_id, status: queued} synchronously,
    spawns a worker thread for the heavy plugin.run_submit."""
    plugin = FakePlugin(
        next_raw="SIMULATION_OK\n", next_metrics={"ipc": 0.5},
    )
    agent = FakeContainer(workspace_files={"main.cc": "x;"})
    ctx = _ctx(tmp_path, plugin, agent)
    ctx.results_dir = tmp_path

    response = handle_submit_async(ctx)
    assert "submission_id" in response
    assert response["status"] == "queued"
    assert response["submission_id"].startswith("sub_")

    # Worker thread runs and outcome lands in the registry
    state = _wait_for_done(ctx, response["submission_id"])
    assert state["status"] == "done"
    assert state["outcome"] == "sim_ok"
    assert state["metric"]["ipc"] == 0.5


def test_submit_async_persists_to_jsonl(tmp_path):
    """submit_outcomes.jsonl gets one line per completed submission
    (Bug 3 fix: outcomes survive even if the agent never polls)."""
    plugin = FakePlugin(
        next_raw="SIMULATION_OK\n", next_metrics={"ipc": 0.5113},
    )
    agent = FakeContainer(workspace_files={"main.cc": "x;"})
    ctx = _ctx(tmp_path, plugin, agent)
    ctx.results_dir = tmp_path

    response = handle_submit_async(ctx)
    _wait_for_done(ctx, response["submission_id"])

    jsonl = tmp_path / "submit_outcomes.jsonl"
    assert jsonl.exists()
    lines = [json.loads(line) for line in jsonl.read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["submission_id"] == response["submission_id"]
    assert lines[0]["outcome"] == "sim_ok"
    assert lines[0]["metric"]["ipc"] == 0.5113


def test_submit_async_ids_are_unique_and_ordered(tmp_path):
    """sub_001, sub_002, ... — each handle_submit_async gets a new id."""
    plugin = FakePlugin(
        next_raw="SIMULATION_OK\n", next_metrics={"ipc": 0.5},
    )
    agent = FakeContainer(workspace_files={"main.cc": "x;"})
    ctx = _ctx(tmp_path, plugin, agent, max_submissions=10)
    ctx.results_dir = tmp_path

    sids = [handle_submit_async(ctx)["submission_id"] for _ in range(3)]
    assert sids == ["sub_001", "sub_002", "sub_003"]
    for s in sids:
        _wait_for_done(ctx, s)


def test_check_submission_unknown_id(tmp_path):
    plugin = FakePlugin()
    agent = FakeContainer(workspace_files={"main.cc": "x;"})
    ctx = _ctx(tmp_path, plugin, agent)
    state = handle_check_submission(ctx, "sub_999")
    assert state["status"] == "unknown"
    assert "no submission with id" in state["error"]


def test_check_submission_reports_build_fail(tmp_path):
    plugin = FakePlugin(
        next_raw="Compilation failed: missing header\n", next_metrics=None,
    )
    agent = FakeContainer(workspace_files={"main.cc": "garbage"})
    ctx = _ctx(tmp_path, plugin, agent)
    ctx.results_dir = tmp_path

    sid = handle_submit_async(ctx)["submission_id"]
    state = _wait_for_done(ctx, sid)
    assert state["status"] == "done"
    assert state["outcome"] == "build_fail"


def test_session_end_writes_marker_file(tmp_path):
    plugin = FakePlugin()
    agent = FakeContainer()
    ctx = _ctx(tmp_path, plugin, agent)
    ctx.results_dir = tmp_path

    result = handle_session_end(ctx, reason="finished my work")
    assert result["status"] == "ok"
    marker = tmp_path / "session_end.requested"
    assert marker.exists()
    payload = json.loads(marker.read_text())
    assert payload["reason"] == "finished my work"
    assert "timestamp" in payload


def test_session_end_without_results_dir_is_no_op(tmp_path):
    """No results_dir wired → returns ok but doesn't try to write."""
    plugin = FakePlugin()
    agent = FakeContainer()
    ctx = _ctx(tmp_path, plugin, agent)
    ctx.results_dir = None

    result = handle_session_end(ctx, reason="x")
    assert result["status"] == "ok"
    assert not (tmp_path / "session_end.requested").exists()


def test_session_end_idempotent_overwrite(tmp_path):
    """Calling session_end twice overwrites the marker (last-call-wins)."""
    plugin = FakePlugin()
    agent = FakeContainer()
    ctx = _ctx(tmp_path, plugin, agent)
    ctx.results_dir = tmp_path

    handle_session_end(ctx, reason="first")
    handle_session_end(ctx, reason="second")
    payload = json.loads((tmp_path / "session_end.requested").read_text())
    assert payload["reason"] == "second"


def test_submission_state_to_dict_round_trip(tmp_path):
    """SubmissionState.to_dict produces JSON-serializable output
    matching the agent-visible schema."""
    state = SubmissionState(submission_id="sub_007")
    state.status = "done"
    state.started_at = 100.0
    state.finished_at = 200.0
    state.report = OutcomeReport(
        outcome=SubmitOutcome.SIM_OK,
        metrics={"ipc": 0.42},
        detail="ok",
        submit_index=1,
    )
    d = state.to_dict()
    # The result must round-trip through json (no datetimes / sets)
    json.dumps(d)
    assert d["submission_id"] == "sub_007"
    assert d["status"] == "done"
    assert d["outcome"] == "sim_ok"
    assert d["metric"]["ipc"] == 0.42
