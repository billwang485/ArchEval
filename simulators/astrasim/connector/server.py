"""astrasim MCP server — SCAFFOLD.

Thin re-export wrapper around ``simulators.champsim.connector.server``.
The submit/check/session_end/browse handlers are universal across
simulators; per-sim divergence lives in the underlying
``SimulatorPlugin`` (here: ``AstrasimPlugin``) and the
corresponding ``SubmitContext`` injected by ``archbench/runtimes/session.py``.
"""
from __future__ import annotations

# Re-export the universal handler surface from the champsim connector.
# The protocol is the same; the only sim-specific surface is the
# SimulatorPlugin which is plumbed through SubmitContext at session
# start time. This keeps a single source of truth for tool semantics.
from simulators.champsim.connector.server import (  # noqa: F401
    SubmissionState,
    SubmitContext,
    handle_browse,
    handle_check_submission,
    handle_read,
    handle_session_end,
    handle_submit,
    handle_submit_and_wait,
    handle_submit_async,
)


def serve(ctx: SubmitContext, port: int) -> None:
    """Stub — actual FastMCP wiring lives in ``server_subprocess.py``."""
    raise NotImplementedError(
        "MCP transport wiring lives in "
        "simulators/astrasim/connector/server_subprocess.py."
    )


__all__ = [
    "SubmissionState",
    "SubmitContext",
    "handle_browse",
    "handle_check_submission",
    "handle_read",
    "handle_session_end",
    "handle_submit",
    "handle_submit_and_wait",
    "handle_submit_async",
    "serve",
]
