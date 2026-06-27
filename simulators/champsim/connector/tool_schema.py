"""Single source of truth for MCP tool signatures advertised to agents.

This module is the canonical contract for the agent <-> harness protocol.
Any tool listed in ``TOOLS`` is what the MCP server's ``tools/list`` endpoint
must expose, and any runtime acting as an MCP client (mini, archharness, ...)
MUST discover tools by querying ``tools/list`` rather than hardcoding the
list from this file.

If you add or remove a tool here, update the registration in
``simulators/champsim/connector/server_subprocess.py`` to match -- the schema and the
FastMCP ``@mcp.tool()`` decorations are kept in lockstep by convention,
not by code generation (Phase A keeps it simple; future phases can wire
auto-registration off this list).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


def render_sim_name(text: str, sim_name: str) -> str:
    """Substitute the ``{sim_name}`` placeholder in a description string.

    Single-sim sessions pass ``sim_name=""``: the placeholder vanishes and
    the resulting double-spaces (and any " 's" -> "'s" artifacts) are
    collapsed so the wire text is byte-for-byte what it was before
    multi-sim namespacing existed. Multi-sim sessions pass the real sim
    name (e.g. ``"dramsys"``), yielding "the dramsys SIMULATOR container".

    We use ``str.replace`` rather than ``str.format`` on purpose: several
    descriptions contain literal JSON-shaped braces (e.g.
    ``{submission_id, status='queued'}``) that ``str.format`` would try to
    interpret as replacement fields and crash on. ``replace`` only touches
    the one placeholder and leaves every other brace untouched.

    This is the single canonical place the placeholder is rendered, used
    by both ``server_subprocess.py`` (registration) and the connector
    smoke tests, so the two can't drift.
    """
    out = text.replace("{sim_name}", sim_name)
    if not sim_name:
        # Collapse the gap the empty placeholder left behind. Only squeeze
        # runs of spaces; never touch newlines (descriptions are single
        # logical strings today, but be conservative).
        out = re.sub(r" {2,}", " ", out)
    return out


@dataclass
class MCPToolSchema:
    """One MCP tool advertised over ``tools/list``.

    Fields:
      name        -- the tool name agents call (e.g. ``submit``).
      description -- human-readable summary; shown to the agent verbatim.
      parameters  -- JSONSchema fragment, keyed by argument name, for the
                     ``properties`` field of the tool's input schema.
      required    -- subset of ``parameters`` keys that are mandatory.
    """

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)


# THE tools list. Anything added here is what the MCP server advertises
# via tools/list and routes to its handler in
# simulators/champsim/connector/handlers/.
#
# Descriptions may carry a ``{sim_name}`` placeholder. ``server_subprocess.py``
# formats it at registration time: ``""`` in single-sim mode (the placeholder
# disappears and double-spaces are collapsed, so the wire text is byte-for-byte
# what it was before namespacing existed) and the real sim name in multi-sim
# mode (e.g. "the dramsys SIMULATOR container"). This keeps TOOLS the single
# source of truth (CLAUDE.md §1.4) — the prefix/format is a registration-time
# decoration, never a mutation of this list. See docs/multi_sim_design.md.
TOOLS: list[MCPToolSchema] = [
    MCPToolSchema(
        name="submit",
        # NOTE: no {sim_name} placeholder here. The original phrasing has no
        # clean adjective slot ("Submit an implementation ...") that could
        # host the placeholder without changing the single-sim wire text
        # (you'd disturb the "an" article or leave a dangling word). In
        # multi-sim mode the *tool name* prefix (e.g. ``dramsys_submit``)
        # already disambiguates which sim this submits to, so a placeholder
        # in the body is redundant. Same reasoning for submit_and_wait and
        # session_end. browse_simulator / read_simulator_file / check_submission
        # DO carry the placeholder because they have a clean "the {sim_name}
        # SIMULATOR container" / "a {sim_name} submission" slot that collapses
        # back to the exact original when sim_name="".
        description=(
            "Submit an implementation for async evaluation. Spawns a "
            "worker thread; returns {submission_id, status='queued'} "
            "immediately. Poll check_submission(submission_id) to learn "
            "the outcome. For short-eval challenges, prefer "
            "submit_and_wait to avoid burning agent turns on polling."
        ),
        parameters={
            "implementation_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Absolute paths to implementation files in /workspace/. "
                    "Each path must start with /workspace/ and its basename "
                    "must match one of the challenge's declared output.files."
                ),
            },
        },
        required=["implementation_paths"],
    ),
    MCPToolSchema(
        name="submit_and_wait",
        # No {sim_name} placeholder — see the note on ``submit``. The tool
        # name prefix disambiguates the sim in multi-sim mode.
        description=(
            "Synchronous submit. Internally posts a job and blocks until "
            "the sim completes, then returns the outcome directly. Use for "
            "short-eval challenges to avoid burning agent turns on polling."
        ),
        parameters={
            "implementation_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Absolute paths to implementation files in /workspace/. "
                    "Each path must start with /workspace/ and its basename "
                    "must match one of the challenge's declared output.files."
                ),
            },
        },
        required=["implementation_paths"],
    ),
    MCPToolSchema(
        name="check_submission",
        description=(
            "Poll a {sim_name} submission by id. Returns "
            "{submission_id, status, outcome?, detail?, metric?}. "
            "Status progresses queued -> running -> done; once done, the "
            "outcome and metric (if SIM_OK) are populated."
        ),
        parameters={
            "submission_id": {
                "type": "string",
                "description": (
                    "ID returned by a prior {sim_name} submit() call "
                    "(e.g. 'sub_001')."
                ),
            },
        },
        required=["submission_id"],
    ),
    MCPToolSchema(
        name="session_end",
        # No {sim_name} placeholder — session_end is genuinely sim-agnostic
        # (it ends the WHOLE session, all sims, not one). In multi-sim mode
        # the prefixed names (dramsys_session_end / timeloop_session_end)
        # both write the same marker; calling either ends everything.
        description=(
            "Voluntary clean-exit signal. Writes session_end.requested "
            "marker; harness initiates teardown after a grace period "
            "instead of waiting for round_timeout."
        ),
        parameters={
            "reason": {
                "type": "string",
                "description": "Optional reason recorded for the trajectory.",
            },
        },
        required=[],
    ),
    MCPToolSchema(
        name="browse_simulator",
        description=(
            "List files in the {sim_name} SIMULATOR container's source tree "
            "at the "
            "given path. NOT the agent container — /workspace/ paths are "
            "wrong here; use your local Read tool for /workspace/. "
            "Subject to the challenge's source_blocklist. "
            "Path MUST be absolute (begin with '/'). ChampSim source "
            "lives at /work/runtimes/champsim/; workload traces live at "
            "/work/workload_pools/champsim/."
        ),
        parameters={
            "path": {
                "type": "string",
                "description": (
                    "Absolute path inside the simulator container "
                    "(e.g. /work/runtimes/champsim/inc). Relative paths "
                    "are rejected — the container has no useful cwd for "
                    "source navigation."
                ),
            },
        },
        required=["path"],
    ),
    MCPToolSchema(
        name="read_simulator_file",
        description=(
            "Read a file from the {sim_name} SIMULATOR container. NOT the "
            "agent "
            "container — /workspace/ paths are wrong here; use your "
            "local Read tool for /workspace/. Subject to the "
            "challenge's source_blocklist (solution paths blocked). "
            "Path MUST be absolute (begin with '/'). ChampSim headers "
            "you'll commonly want: /work/runtimes/champsim/inc/*.h, "
            "/work/runtimes/champsim/src/*.cc."
        ),
        parameters={
            "path": {
                "type": "string",
                "description": (
                    "Absolute path inside the simulator container "
                    "(e.g. /work/runtimes/champsim/inc/modules.h). "
                    "Relative paths are rejected with a helpful error."
                ),
            },
        },
        required=["path"],
    ),
]


__all__ = ["MCPToolSchema", "TOOLS", "render_sim_name"]
