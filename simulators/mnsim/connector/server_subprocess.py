"""mnsim MCP server entry point — SCAFFOLD.

Mirrors ``simulators/champsim/connector/server_subprocess.py``. For
the scaffold this module simply re-exports the ChampSim subprocess
entry point and tags the underlying ``SubmitContext`` as "mnsim". The
argparse + FastMCP registration is identical because the
``SimulatorPlugin`` interface is the only sim-specific seam.

TODO: if an mnsim-only tool is added, register it here via an
additional ``@mcp.tool()`` decorator (using the
``Annotated[T, Field(description=_param_desc(...))]`` pattern — never a
bare ``@mcp.tool()`` over a docstring; see docs/mcp_connector.md
"Registration gotcha") and a handler under
``simulators/mnsim/connector/handlers/``.
"""
from __future__ import annotations

# The ChampSim subprocess entry point is parametrized by a
# SimulatorPlugin passed through the SubmitContext. For now we
# re-export its main() so mnsim sessions can launch this module as
# their MCP server. If/when the schema diverges, fork the body of
# main() here.
from simulators.champsim.connector.server_subprocess import (  # noqa: F401
    main,
)


if __name__ == "__main__":
    # Same argparse contract as champsim/connector/server_subprocess.py:
    #   --port, --challenge-dir, --sim-container, --agent-container,
    #   --results-dir [, --plugin mnsim].
    main()
