"""MCP connector — Streamable HTTP MCP server bridging agents and the harness.

Public surface:
  server.py             -- handler re-exports + ``serve`` stub
  server_subprocess.py  -- argparse entry point that boots a FastMCP server
  tool_schema.py        -- canonical tool list (single source of truth)
  handlers/             -- per-tool implementations (submit, browse, ...)
  info.yaml             -- connector card (name, port range, supported tools)

See ``simulators/champsim/connector/README.md`` for the high-level integration story.
"""
