"""MCP server — the only connector between agent container and simulator.

This module is a thin orchestrator that re-exports the handlers from
``simulators.champsim.connector.handlers`` for tests and callers that
imported the old monolithic module. The actual handler implementations
live in:

    simulators.champsim.connector.handlers.submit   -- submit /
        submit_and_wait / check_submission / session_end
    simulators.champsim.connector.handlers.browse   -- browse_simulator
        / read_simulator_file

The FastMCP transport wiring lives in
``simulators/champsim/connector/server_subprocess.py``; the canonical
tool list that this server advertises is in
``simulators/champsim/connector/tool_schema.py``.

Every outbound string the agent sees is scrubbed by the ``Anonymizer``;
every inbound string from the agent is ``translate_inbound``-ed before
touching the simulator. The server is intentionally stateless about
"best so far" or "round number" -- those are runtime concerns (e.g.
archharness's multi-round resume). Keeping MCP stateless makes the
protocol gameproof and the tests deterministic.

Async submit lifecycle (P7-revision; eliminates MCP read-timeout Bug 1):
  1. Agent calls submit(implementation_paths=[...]).
  2. Handler validates inputs synchronously (paths, line cap, storage check),
     assigns a submission_id, spawns a worker thread that runs the heavy
     plugin.run_submit + parse_output, and returns immediately with
     {"submission_id": "sub_NNN", "status": "queued"}.
  3. Worker thread persists each completed outcome to
     <results_dir>/submit_outcomes.jsonl on completion -- even if the agent
     never polls, the outcome is captured (fixes Bug 3).
  4. Agent polls check_submission(submission_id) to learn outcome, OR uses
     submit_and_wait(...) to collapse submit+poll into one synchronous call.
"""
from __future__ import annotations

# Re-export the handler API surface. Tests and other callers in the repo
# import these names from ``simulators.champsim.connector.server`` for
# backwards compatibility with the pre-refactor monolithic module.
from simulators.champsim.connector.handlers.browse import handle_browse, handle_read
from simulators.champsim.connector.handlers.submit import (
    SubmissionState,
    SubmitContext,
    _outcome,
    handle_check_submission,
    handle_session_end,
    handle_submit,
    handle_submit_and_wait,
    handle_submit_async,
)


def serve(ctx: SubmitContext, port: int) -> None:
    """Stub kept for backwards compatibility.

    The actual FastMCP transport wiring lives in
    ``simulators/champsim/connector/server_subprocess.py``; the
    pure-function handlers (handle_submit, handle_submit_async,
    handle_submit_and_wait, handle_check_submission,
    handle_session_end, handle_browse, handle_read) are testable
    without the transport layer.
    """
    raise NotImplementedError(
        "MCP transport wiring lives in "
        "simulators/champsim/connector/server_subprocess.py. "
        "Pure-function handlers (handle_submit, handle_submit_async, "
        "handle_submit_and_wait, handle_check_submission, "
        "handle_session_end, handle_browse, handle_read) are testable "
        "without the transport layer."
    )


__all__ = [
    "SubmissionState",
    "SubmitContext",
    "_outcome",
    "handle_browse",
    "handle_check_submission",
    "handle_read",
    "handle_session_end",
    "handle_submit",
    "handle_submit_and_wait",
    "handle_submit_async",
    "serve",
]
