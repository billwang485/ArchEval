"""MCP tool handlers.

Each submodule implements one logical group of tools:

    submit  -- submit / submit_and_wait / check_submission / session_end
    browse  -- browse_simulator / read_simulator_file

Every public ``handle_*`` function in these modules is wired to a FastMCP
``@mcp.tool()`` in ``simulators/champsim/connector/server_subprocess.py``;
the schema those tools advertise lives in
``simulators/champsim/connector/tool_schema.py``.
"""
from __future__ import annotations

from simulators.champsim.connector.handlers.browse import handle_browse, handle_read
from simulators.champsim.connector.handlers.submit import (
    SubmissionState,
    SubmitContext,
    handle_check_submission,
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
