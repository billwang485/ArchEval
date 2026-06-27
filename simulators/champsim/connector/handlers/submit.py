"""Submit handlers — async submit lifecycle + session_end.

Tools implemented here:

    submit()             -- async; spawns a worker thread, returns submission_id
    submit_and_wait()    -- synchronous wrapper around submit() + polling loop
    check_submission()   -- look up an in-flight or completed submission
    session_end()        -- voluntary clean-exit marker

The submission registry (``SubmitContext.submissions``) is the source of
truth for in-flight state. Completed submissions are also persisted to
``<results_dir>/submit_outcomes.jsonl`` so the outcome survives even if
the agent never polls.
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from archbench.core.anonymizer import Anonymizer
from archbench.core.outcomes import OutcomeReport, SubmitOutcome

if TYPE_CHECKING:
    from archbench.core.challenge import Challenge
    from archbench.core.container import ContainerManager
    from archbench.core.plugin_base import SimulatorPlugin

log = logging.getLogger("archbench.mcp")


@dataclass
class SubmitContext:
    """Per-run state the submit() tool needs.

    Notably NOT here: "best so far", "round count", "checkpoint paths".
    Those are runtime concerns; MCP just classifies one submit at a time.
    """

    challenge: "Challenge"
    challenge_dir: Path
    plugin: "SimulatorPlugin"
    agent: "ContainerManager"
    sim: "ContainerManager"
    anonymizer: Anonymizer = field(default_factory=Anonymizer.disabled)
    # Number of SIM_OK submissions counted so far in this run.
    # Initialized from prior round on resume; passed back via OutcomeReport
    # so the runtime can persist it.
    submit_count: int = 0
    attempt_count: int = 0  # All attempts (incl. failures)
    # Async submission registry: submission_id -> SubmissionState dict.
    # Populated by handle_submit when it spawns a worker thread.
    submissions: dict[str, "SubmissionState"] = field(default_factory=dict)
    submissions_lock: threading.Lock = field(default_factory=threading.Lock)
    # Where to persist submit_outcomes.jsonl + session_end.requested.
    # Set by the subprocess runner from --results-dir; None in tests.
    results_dir: Optional[Path] = None
    # Prepended to generated submission ids so multi-sim sessions don't
    # collide. Each sim gets its OWN SubmitContext (one per bound sim
    # container) but they SHARE one ``<results_dir>/.in_flight/`` directory
    # and one ``submit_outcomes.jsonl``. Without a per-sim prefix, two sims
    # would both mint ``sub_001`` and their in-flight markers (CLAUDE.md
    # §1.10) would alias — the grace-period could clear a still-running
    # sim's marker. Single-sim leaves this "" so ids stay ``sub_001`` etc.
    # (zero change). Multi-sim sets e.g. ``"dramsys_"`` -> ``dramsys_sub_001``.
    submission_id_prefix: str = ""


@dataclass
class SubmissionState:
    """Snapshot of one in-flight (or finished) submit.

    Lifecycle: queued -> running -> done. Once done, mutable fields are
    frozen; the worker thread that wrote them does NOT touch them again.
    """

    submission_id: str
    status: str = "queued"  # "queued" | "running" | "done"
    report: Optional[OutcomeReport] = None
    submitted_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None  # set if the worker thread itself raised
    # Contents of /workspace/prediction.json AS OF submit acceptance —
    # binds the agent's self-prediction to the design that was scored
    # (post-session prediction_calibration evaluator). None if the agent
    # wrote no prediction file. Captured BEFORE the worker runs so a
    # file edited mid-simulation cannot retroactively change "what was
    # predicted for this submit".
    prediction_snapshot: Optional[dict] = None

    def to_dict(self) -> dict:
        """JSON-serializable snapshot returned by check_submission()."""
        out: dict = {
            "submission_id": self.submission_id,
            "status": self.status,
            "submitted_at": self.submitted_at,
        }
        if self.started_at is not None:
            out["started_at"] = self.started_at
        if self.finished_at is not None:
            out["finished_at"] = self.finished_at
        if self.report is not None:
            r = self.report
            out["outcome"] = r.outcome.value
            out["detail"] = r.detail
            out["submit_index"] = r.submit_index
            if r.metrics is not None:
                out["metric"] = r.metrics
            if r.raw_log_tail:
                out["raw_log_tail"] = r.raw_log_tail
            if r.metadata:
                out["metadata"] = r.metadata
        if self.error:
            out["error"] = self.error
        if self.prediction_snapshot is not None:
            out["prediction_snapshot"] = self.prediction_snapshot
        return out


# ---------------------------------------------------------------------------
# submit() — core synchronous pipeline (wrapped by submit_async / _and_wait)
# ---------------------------------------------------------------------------


def handle_submit(
    ctx: SubmitContext,
    implementation_paths: Optional[list[str]] = None,
) -> OutcomeReport:
    """Run one submit. Returns an OutcomeReport — never raises to the agent.

    Agent must explicitly flag the absolute paths of files to ship:

        submit(implementation_paths=[
            "/workspace/candidate.h",
            "/workspace/candidate.cc",
        ])

    The connector validates the paths but does NOT enforce that the
    agent has produced any other artifacts (deliverable MDs, tests, …).
    Whether the agent followed the prompt's instructions about
    deliverables is a judging question, not an infrastructure gate
    (any gate would be "helping the agent cheat" by structurally
    forcing compliance instead of measuring it).

    Order of checks (each non-OK is a free retry — does NOT consume budget):

      1. Already at max_submissions → SIM_OK with budget_exhausted=True.
      2. Already at max_attempts (defensive infinite-loop cap) →
         VALIDATION_REJECT with attempt_cap_reached=True.
      3. implementation_paths missing / wrong count / outside /workspace /
         basename mismatch → BUILD_FAIL with a clear message.
      4. Pull agent files → tmp dir. Read errors → BUILD_FAIL.
      5. Code-line cap (challenge.eval.max_code_lines) → BUILD_FAIL.
      6. Evaluate (challenge-owned pre-simulation validation + simulation).
         VALIDATION_FAILED → VALIDATION_REJECT; otherwise the plugin output
         classifies as SIM_TIMEOUT, BUILD_FAIL, or SIM_OK.

    Only step (6)'s success path increments submit_count.
    """
    ctx.attempt_count += 1

    # Defensive: bound total attempts (not just SIM_OK count). Without this
    # an agent stuck in a build-fail or validation-reject loop burns CPU
    # forever — failures are "free retries" but free × ∞ = unbounded.
    # For single-shot challenges (max_submissions=1) the agent typically
    # needs ~15-20 attempts to write all deliverables + tests + iterate
    # on pre-simulation validation; the cap is generous but bounded.
    max_attempts = max(ctx.challenge.eval.max_submissions * 10, 30)
    if ctx.attempt_count > max_attempts:
        return OutcomeReport(
            outcome=SubmitOutcome.VALIDATION_REJECT,
            detail=(
                f"Attempt cap reached ({ctx.attempt_count}/{max_attempts}). "
                f"Successful submissions so far: {ctx.submit_count}/"
                f"{ctx.challenge.eval.max_submissions}. Session ending."
            ),
            submit_index=ctx.submit_count,
            metadata={"attempt_cap_reached": True,
                      "attempt_count": ctx.attempt_count},
        )

    if ctx.submit_count >= ctx.challenge.eval.max_submissions:
        return OutcomeReport(
            outcome=SubmitOutcome.SIM_OK,
            detail=(
                f"Submission budget exhausted "
                f"({ctx.submit_count}/{ctx.challenge.eval.max_submissions})"
            ),
            submit_index=ctx.submit_count,
            metadata={"budget_exhausted": True},
        )

    submission_files = ctx.plugin.submission_files(ctx.challenge)

    # 3. Validate agent-supplied paths. If the agent didn't flag paths
    # (legacy auto-discovery), fall back to /workspace/<basename>.
    paths_to_copy: list[tuple[str, str]] = []  # (container_path, basename)
    if implementation_paths is None:
        paths_to_copy = [(f"/workspace/{f}", f) for f in submission_files]
    else:
        if len(implementation_paths) != len(submission_files):
            return _outcome(
                SubmitOutcome.BUILD_FAIL,
                detail=(
                    f"submit() expects {len(submission_files)} path(s) for "
                    f"{submission_files}, got {len(implementation_paths)}: "
                    f"{implementation_paths}"
                ),
                ctx=ctx,
            )
        for p in implementation_paths:
            if not p.startswith("/workspace/"):
                return _outcome(
                    SubmitOutcome.BUILD_FAIL,
                    detail=(
                        f"Path {p!r} must be inside /workspace/. "
                        "Place your implementation under /workspace/ and "
                        "pass an absolute path that starts with /workspace/."
                    ),
                    ctx=ctx,
                )
            bn = p.rsplit("/", 1)[-1]
            if bn not in submission_files:
                return _outcome(
                    SubmitOutcome.BUILD_FAIL,
                    detail=(
                        f"Basename {bn!r} not in expected submission "
                        f"files {submission_files}. Match the basenames "
                        "the challenge declares."
                    ),
                    ctx=ctx,
                )
            paths_to_copy.append((p, bn))

    with tempfile.TemporaryDirectory(prefix="archbench_submit_") as tmp:
        tmp_path = Path(tmp)

        # 4. Pull agent files
        copied: dict[str, str] = {}
        missing: list[str] = []
        for container_path, bn in paths_to_copy:
            try:
                ctx.agent.copy_out(container_path, tmp_path / bn)
                copied[bn] = (tmp_path / bn).read_text()
            except Exception as e:
                missing.append(f"{container_path}: {e}")
        if missing:
            return _outcome(
                SubmitOutcome.BUILD_FAIL,
                detail="Some submission files could not be read from the agent workspace.",
                raw=ctx.anonymizer.scrub_outbound("\n".join(missing)),
                ctx=ctx,
            )

        # 3. Code-line cap
        line_cap = ctx.challenge.eval.max_code_lines
        total_lines = sum(
            len(text.splitlines()) for text in copied.values()
        )
        if total_lines > line_cap:
            return _outcome(
                SubmitOutcome.BUILD_FAIL,
                detail=f"Code exceeds line cap: {total_lines} lines > {line_cap} max.",
                ctx=ctx,
            )

        # 4. Build + run (plugin owns the dispatch — evaluate.sh vs simulate.sh)
        try:
            raw_output = ctx.plugin.run_submit(ctx.sim, ctx.challenge, copied)
        except subprocess.TimeoutExpired as e:
            return _outcome(
                SubmitOutcome.SIM_TIMEOUT,
                detail=f"Simulation exceeded wall-clock limit ({e.timeout}s).",
                ctx=ctx,
            )
        except Exception as e:
            # Genuine infra failure (container died, plugin bug). Surface
            # as SIM_TIMEOUT — agent retries are bounded by the budget rule.
            return _outcome(
                SubmitOutcome.SIM_TIMEOUT,
                detail=f"Simulation infrastructure error: {e!s}",
                ctx=ctx,
            )

        if any(line.strip() == "VALIDATION_FAILED" for line in raw_output.splitlines()):
            log.info("challenge pre-simulation validation rejected the submission")
            return _outcome(
                SubmitOutcome.VALIDATION_REJECT,
                detail="Submission failed pre-simulation validation.",
                ctx=ctx,
            )

        metrics = ctx.plugin.parse_output(raw_output)
        if metrics is None:
            # Distinguish build failure from sim-output-unparseable
            outcome = (
                SubmitOutcome.BUILD_FAIL
                if "BUILD_FAIL" in raw_output or "Compilation failed" in raw_output
                else SubmitOutcome.SIM_TIMEOUT
            )
            return _outcome(
                outcome,
                detail="Could not parse simulator output into metrics.",
                raw=ctx.anonymizer.scrub_outbound(raw_output[-3000:]),
                ctx=ctx,
            )

    # Success — count this submission
    ctx.submit_count += 1
    return OutcomeReport(
        outcome=SubmitOutcome.SIM_OK,
        metrics=metrics,
        submit_index=ctx.submit_count,
        detail=(
            f"Simulation OK (submit {ctx.submit_count}/"
            f"{ctx.challenge.eval.max_submissions})"
        ),
        raw_log_tail=ctx.anonymizer.scrub_outbound(raw_output[-1500:]),
    )


def _outcome(
    kind: SubmitOutcome, detail: str, ctx: SubmitContext, raw: str = "",
) -> OutcomeReport:
    """Build an OutcomeReport. Scrubs detail AND raw through the
    anonymizer — sub-agent audit flagged this as a leak path (any
    exception message containing a SPEC trace name would reach the
    agent unscrubbed). Now: structurally impossible."""
    return OutcomeReport(
        outcome=kind,
        detail=ctx.anonymizer.scrub_outbound(detail),
        raw_log_tail=ctx.anonymizer.scrub_outbound(raw) if raw else "",
        submit_index=ctx.submit_count,
        metadata={"attempt_count": ctx.attempt_count},
    )


# ---------------------------------------------------------------------------
# Async submit lifecycle
# ---------------------------------------------------------------------------


def _next_submission_id(ctx: SubmitContext) -> str:
    """Generate the next submission_id under the registry lock.

    The id is ``{submission_id_prefix}sub_{n:03d}``. Single-sim leaves the
    prefix empty (``sub_001``); multi-sim sets a per-sim prefix
    (``dramsys_sub_001``) so two sims sharing one ``.in_flight/`` dir +
    ``submit_outcomes.jsonl`` never alias their ids (CLAUDE.md §1.10). The
    counter is per-``ctx`` (each sim has its own ``submissions`` dict), so
    it stays monotonic within a sim.
    """
    with ctx.submissions_lock:
        n = len(ctx.submissions) + 1
        return f"{ctx.submission_id_prefix}sub_{n:03d}"


def _append_outcome_jsonl(
    ctx: SubmitContext, state: SubmissionState,
) -> None:
    """Append one completed submission's record to submit_outcomes.jsonl.

    Called by the worker thread after parse_output finishes. Best-effort:
    persistence MUST never raise into the worker (a write failure would
    silently kill the worker and leave the agent waiting forever).
    """
    if ctx.results_dir is None:
        return
    try:
        ctx.results_dir.mkdir(parents=True, exist_ok=True)
        path = ctx.results_dir / "submit_outcomes.jsonl"
        line = json.dumps(state.to_dict())
        with open(path, "a") as fh:
            fh.write(line + "\n")
    except Exception as e:
        log.warning("submit_outcomes.jsonl append failed for %s: %s",
                    state.submission_id, e)


def _inflight_dir(ctx: SubmitContext) -> Optional[Path]:
    """Return the ``<results_dir>/.in_flight/`` sidecar dir, or None."""
    if ctx.results_dir is None:
        return None
    return ctx.results_dir / ".in_flight"


def _mark_inflight(ctx: SubmitContext, submission_id: str) -> None:
    """Touch ``<results_dir>/.in_flight/<submission_id>``.

    Used by the session-end grace-period to detect that a worker thread
    is still running even though ``submit_outcomes.jsonl`` has not yet
    been appended to. Without this signal, the grace-period exits after
    the file-size-stable heuristic triggers (10 s) when no submission
    has finished yet — the bug that invalidated branch_haiku.
    """
    d = _inflight_dir(ctx)
    if d is None:
        return
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / submission_id).touch(exist_ok=True)
    except Exception as e:
        log.debug("inflight marker write failed for %s: %s", submission_id, e)


def _clear_inflight(ctx: SubmitContext, submission_id: str) -> None:
    """Remove ``<results_dir>/.in_flight/<submission_id>`` after worker done."""
    d = _inflight_dir(ctx)
    if d is None:
        return
    try:
        (d / submission_id).unlink(missing_ok=True)
    except Exception as e:
        log.debug("inflight marker delete failed for %s: %s", submission_id, e)


def _run_submit_worker(
    ctx: SubmitContext, state: SubmissionState,
    implementation_paths: Optional[list[str]],
) -> None:
    """Worker-thread entry: run the full submit pipeline, mutate state.

    Wraps handle_submit in a try/except so an unexpected exception lands
    in state.error rather than killing the thread silently. The MCP
    handler thread that spawned this worker has already returned the
    submission_id to the agent.
    """
    state.started_at = time.time()
    state.status = "running"
    try:
        report = handle_submit(ctx, implementation_paths)
        state.report = report
    except Exception as e:
        log.exception("submit worker raised for %s: %s",
                      state.submission_id, e)
        # Synthesize a SIM_TIMEOUT outcome so the agent has SOMETHING
        # to read back; the real classification path inside handle_submit
        # already handles plugin/build failures, so anything reaching here
        # is genuinely infra-level.
        state.error = repr(e)
        state.report = OutcomeReport(
            outcome=SubmitOutcome.SIM_TIMEOUT,
            detail=ctx.anonymizer.scrub_outbound(
                f"submit worker raised: {e!s}"
            ),
            submit_index=ctx.submit_count,
        )
    finally:
        state.finished_at = time.time()
        state.status = "done"
        _append_outcome_jsonl(ctx, state)
        _clear_inflight(ctx, state.submission_id)


_PREDICTION_FILE = "/workspace/prediction.json"
_PREDICTION_MAX_BYTES = 16 * 1024


def _capture_prediction_snapshot(ctx: SubmitContext) -> Optional[dict]:
    """Best-effort read of the agent's prediction.json at submit acceptance.

    Returns ``{path, sha256, captured_at, content}`` or None (no file /
    unreadable / oversized / not JSON). MUST never raise — a missing
    prediction is a normal capability datum, not an error.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="archbench_pred_") as tmp:
            dest = Path(tmp) / "prediction.json"
            ctx.agent.copy_out(_PREDICTION_FILE, dest)
            raw = dest.read_bytes()
        if len(raw) > _PREDICTION_MAX_BYTES:
            return {
                "path": _PREDICTION_FILE,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "captured_at": time.time(),
                "content": None,
                "error": f"prediction.json exceeds {_PREDICTION_MAX_BYTES} bytes",
            }
        content = json.loads(raw.decode("utf-8"))
        if not isinstance(content, dict):
            content = {"_non_object": True}
        return {
            "path": _PREDICTION_FILE,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "captured_at": time.time(),
            "content": content,
        }
    except Exception as e:
        log.debug("no prediction snapshot for this submit: %s", e)
        return None


def handle_submit_async(
    ctx: SubmitContext,
    implementation_paths: Optional[list[str]] = None,
) -> dict:
    """Async submit entry point used by the MCP tool wrapper.

    Spawns a worker thread that runs the full handle_submit pipeline,
    returns immediately with {submission_id, status}. The agent polls
    check_submission() to learn the outcome.

    Why a thread (not an asyncio task): FastMCP's tool handlers run on
    asyncio's event loop, and plugin.run_submit blocks on a multi-minute
    subprocess.run. Doing that on the loop would starve other MCP
    handlers (including the agent's check_submission polls). A thread is
    the simpler isolation — Python's GIL is irrelevant here because the
    subprocess does the work outside Python.
    """
    sid = _next_submission_id(ctx)
    state = SubmissionState(submission_id=sid)
    state.prediction_snapshot = _capture_prediction_snapshot(ctx)
    with ctx.submissions_lock:
        ctx.submissions[sid] = state
    # Mark in-flight BEFORE spawning the worker so the session-end
    # grace-period can't miss a submission that's still spinning up
    # (the worker thread does the matching _clear_inflight in its
    # finally block).
    _mark_inflight(ctx, sid)
    t = threading.Thread(
        target=_run_submit_worker,
        args=(ctx, state, implementation_paths),
        name=f"archbench-submit-{sid}",
        daemon=True,
    )
    t.start()
    return {"submission_id": sid, "status": "queued"}


def handle_check_submission(
    ctx: SubmitContext, submission_id: str,
) -> dict:
    """Look up a submission's current state. Idempotent."""
    with ctx.submissions_lock:
        state = ctx.submissions.get(submission_id)
    if state is None:
        return {
            "submission_id": submission_id,
            "status": "unknown",
            "error": f"no submission with id {submission_id!r}",
        }
    return state.to_dict()


def handle_submit_and_wait(
    ctx: SubmitContext,
    implementation_paths: Optional[list[str]] = None,
    poll_interval: float = 2.0,
    timeout: float = 1800.0,
) -> dict:
    """Synchronous submit. Internally: spawn worker, block until done.

    Equivalent to calling ``submit()`` then ``check_submission()`` in a
    loop, but in a single MCP request -- no agent turn wasted on polling.
    For short-eval challenges (e.g. cache_replacement_fast, ~5 min sim)
    this collapses ~75 polling round-trips into one synchronous call.

    Caller's HTTP client must have a timeout >= ``timeout`` for this to
    work end-to-end; the mini/archharness clients already use 1800s.

    Returns the same payload as ``check_submission``'s "done" reply, or
    a ``{status: "timeout", error: ...}`` envelope if the deadline expires.
    """
    state = handle_submit_async(ctx, implementation_paths)
    sub_id = state["submission_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = handle_check_submission(ctx, sub_id)
        if current.get("status") == "done":
            return current
        time.sleep(poll_interval)
    return {
        "submission_id": sub_id,
        "status": "timeout",
        "error": f"submit_and_wait exceeded {timeout}s",
    }


def handle_session_end(
    ctx: SubmitContext, reason: str = "",
) -> dict:
    """Agent's voluntary clean exit.

    Writes <results_dir>/session_end.requested with the reason + timestamp.
    The harness watches for this marker; if present, the finally-block
    runs immediately instead of waiting for round_timeout.

    Idempotent: a second call overwrites the marker (last-call-wins).
    Returns the path the marker was written to, for the agent's trace.
    """
    if ctx.results_dir is None:
        return {
            "status": "ok",
            "detail": "session_end recorded (no results_dir wired; "
                      "marker not persisted)",
            "reason": reason,
        }
    try:
        ctx.results_dir.mkdir(parents=True, exist_ok=True)
        marker = ctx.results_dir / "session_end.requested"
        payload = {
            "reason": reason or "",
            "timestamp": time.time(),
        }
        marker.write_text(json.dumps(payload))
        return {
            "status": "ok",
            "detail": "session_end recorded; harness will tear down shortly",
            "marker_path": str(marker),
        }
    except Exception as e:
        log.warning("session_end marker write failed: %s", e)
        return {
            "status": "error",
            "detail": ctx.anonymizer.scrub_outbound(
                f"failed to write session_end marker: {e!s}"
            ),
        }


__all__ = [
    "SubmitContext",
    "SubmissionState",
    "handle_submit",
    "handle_submit_async",
    "handle_submit_and_wait",
    "handle_check_submission",
    "handle_session_end",
    "_outcome",  # re-exported for tests/test_outcome_anonymization.py
]
