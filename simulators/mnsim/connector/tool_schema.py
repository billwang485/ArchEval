"""mnsim MCP tool schema — SCAFFOLD.

Mirrors ``simulators/champsim/connector/tool_schema.py``. For the
scaffold the tool list is identical to ChampSim's because submit/browse
semantics are universal. Per-sim docstrings differ in places (the
description mentions sim-specific file extensions); these are not yet
forked so the re-export is sufficient.

TODO: if/when an mnsim-specific tool is added, append its
``MCPToolSchema`` entry to ``TOOLS`` below and wire the handler.
"""
from __future__ import annotations

# Re-export the canonical tool list. Anything advertised over
# ``tools/list`` is what's in this list — for now, the standard set.
from simulators.champsim.connector.tool_schema import (  # noqa: F401
    MCPToolSchema,
    TOOLS,
)


__all__ = ["MCPToolSchema", "TOOLS"]
