"""gem5 MCP connector — SCAFFOLD.

Mirror of ``simulators/champsim/connector/`` for the gem5 simulator.
The MCP protocol surface (submit, submit_and_wait, check_submission,
session_end, browse_simulator, read_simulator_file) is identical
between simulators because submit semantics are universal — the
connector's job is to validate inputs, spawn a worker thread that
invokes the plugin's ``run_submit``, and surface outcomes to the
agent.

For now this package re-exports the ChampSim implementation verbatim;
the only gem5-specific surface is the tool-schema doc strings
(file extensions / submission shape) which can diverge later if needed.

TODO: split out a per-sim tool_schema.py if/when gem5 wants
sim-specific browse tools.
"""
