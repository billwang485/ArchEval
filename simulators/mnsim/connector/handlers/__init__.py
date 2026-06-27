"""mnsim MCP tool handlers — SCAFFOLD.

Re-exports the universal handler surface from
``simulators.champsim.connector.handlers``. Submit/browse semantics
are the same protocol; mnsim-specific behavior lives in the
``MNSIMPlugin`` invoked by the handler.

TODO: if an mnsim-only handler is needed, add a module under this
directory and re-export the function here.
"""
from __future__ import annotations

from simulators.champsim.connector.handlers import (  # noqa: F401
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
]
