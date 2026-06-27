"""Hello-world contract tests for every per-sim MCP connector.

Locks in CLAUDE.md §1.2 (per-sim connector layout) and §1.4 (single
source of truth tool_schema.TOOLS). Each sim that ships a
``simulators/<sim>/connector/`` directory MUST:

  - Import cleanly.
  - Advertise exactly the canonical 6 tools (submit, submit_and_wait,
    check_submission, session_end, browse_simulator, read_simulator_file).
  - Expose a callable ``server_subprocess.main`` with the same argparse
    shape as ChampSim's reference implementation.
  - Re-export the universal handler API surface from
    ``simulators.champsim.connector.handlers``.

These are CONTRACT tests — they do NOT bring up a sim container or
make real MCP calls (see ``scripts/smoke_all_connectors.sh`` for a CLI
runner; full container-roundtrip is in
``tests/test_champsim_integration.py`` which is skipped without
podman).

The bottom half of this file also covers the multi-simulator
tool-namespacing plumbing (``docs/multi_sim_design.md``): it registers
the canonical tools against a real ``FastMCP`` once in single-sim mode
(asserting BARE names + byte-for-byte descriptions — the compatibility
guarantee) and once in multi-sim mode (asserting the prefixed union and
that a call routes to the right per-sim ``SubmitContext``). These calls
exercise only the no-container tools (``session_end`` writes a marker,
``check_submission`` looks up an id) so they still run without docker.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

# Discover sims by walking simulators/ rather than hardcoding — that
# way the test grows naturally as new sims are scaffolded.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SIMS = sorted(
    d.name
    for d in (_REPO_ROOT / "simulators").iterdir()
    if d.is_dir() and (d / "connector").is_dir() and not d.name.startswith("_")
)

CANONICAL_TOOLS = {
    "submit",
    "submit_and_wait",
    "check_submission",
    "session_end",
    "browse_simulator",
    "read_simulator_file",
}


@pytest.mark.parametrize("sim", _SIMS)
def test_connector_tool_schema_matches_canonical(sim: str) -> None:
    """Each sim's connector advertises exactly the 6 canonical tools.

    Per CLAUDE.md §1.4, ``tool_schema.TOOLS`` is the single source of
    truth. The scaffolded sims re-export ChampSim's TOOLS verbatim.
    Once a sim grows a sim-specific tool (e.g. Timeloop's
    ``inspect_dataflow_yaml``), the assertion below should be updated
    to allow a superset of CANONICAL_TOOLS — never a subset.
    """
    mod = importlib.import_module(f"simulators.{sim}.connector.tool_schema")
    got = {t.name for t in mod.TOOLS}
    missing = CANONICAL_TOOLS - got
    assert not missing, (
        f"{sim} connector is missing canonical tools: {missing}. "
        "Re-export simulators.champsim.connector.tool_schema.TOOLS or "
        "extend with additional MCPToolSchema entries on top of the canonical set."
    )


@pytest.mark.parametrize("sim", _SIMS)
def test_connector_server_module_loads(sim: str) -> None:
    """Each sim's connector exposes server + server_subprocess + handlers.

    Per CLAUDE.md §1.2, the per-sim connector layout is fixed. This
    test catches silent breakage from a refactor (e.g. someone deletes
    server_subprocess.py thinking it was unused).
    """
    importlib.import_module(f"simulators.{sim}.connector.server")
    importlib.import_module(f"simulators.{sim}.connector.server_subprocess")
    importlib.import_module(f"simulators.{sim}.connector.handlers")


@pytest.mark.parametrize("sim", _SIMS)
def test_connector_main_is_callable(sim: str) -> None:
    """``server_subprocess.main`` is the FastMCP entry point.

    Re-exports must keep this symbol importable so that
    ``archbench/runtimes/session.py::_start_mcp_server`` can do
    ``python -m simulators.<sim>.connector.server_subprocess``.
    """
    mod = importlib.import_module(f"simulators.{sim}.connector.server_subprocess")
    assert callable(getattr(mod, "main", None)), (
        f"{sim}/connector/server_subprocess.py must expose a callable main()"
    )


@pytest.mark.parametrize("sim", _SIMS)
def test_connector_handlers_reexport(sim: str) -> None:
    """Each sim's handlers package re-exports the universal handler API.

    Lock-in for CLAUDE.md §1.4 — runtimes (MCP clients) and the
    session orchestrator both depend on these names being present.
    A scaffold sim that just re-exports champsim's handlers must
    forward all of these.
    """
    mod = importlib.import_module(f"simulators.{sim}.connector.handlers")
    expected = {
        "SubmissionState", "SubmitContext",
        "handle_submit", "handle_submit_async", "handle_submit_and_wait",
        "handle_check_submission", "handle_session_end",
        "handle_browse", "handle_read",
    }
    missing = expected - set(dir(mod))
    assert not missing, (
        f"{sim}/connector/handlers/__init__.py is missing re-exports: {missing}. "
        "Mirror simulators/timeloop/connector/handlers/__init__.py."
    )


def test_submit_tool_routes_implementation_paths_not_wait(monkeypatch) -> None:
    """Regression: the FastMCP-registered ``submit`` tool must forward
    ``implementation_paths`` to ``handle_submit_async`` and reject a bare
    ``{"wait": True}`` payload.

    Incident (2026-05-28, dramsys_ddr4_controller_tuning): a stale baked
    ``archbench-agent-mini:v6`` image shipped a pre-Phase-B
    ``/opt/mini/main.py`` that special-cased ``submit`` as
    ``mcp.call("submit", {"wait": True})`` — discarding the LLM's
    ``implementation_paths`` and sending ``{"wait": True}`` instead. The
    server's Pydantic ``submitArguments`` model rejected every one of the
    190 calls with::

        1 validation error for submitArguments
        implementation_paths
          Field required [type=missing, input_value={'wait': True}, ...]

    The host runtime (runtimes/mini/src/main.py) was already fixed to
    forward args verbatim; the bug was purely the stale image. This test
    locks the SERVER-side contract so any future runtime that resurrects
    the ``{"wait": True}`` shortcut fails loudly here instead of in a
    multi-hour smoke run. It drives the EXACT registration used in
    production via ``register_sim_tools`` (no copy that could drift).
    """
    import anyio

    from simulators.champsim.connector import server_subprocess as srv

    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.shared.exceptions import McpError  # noqa: F401
    except ImportError:
        pytest.skip("mcp package not installed")

    # Record what handle_submit_async actually receives (patched in the
    # server_subprocess namespace, which is where register_sim_tools looks it up).
    seen: dict = {}

    def _fake_async(ctx, implementation_paths=None):
        seen["implementation_paths"] = implementation_paths
        return {"submission_id": "sub_001", "status": "queued"}

    monkeypatch.setattr(srv, "handle_submit_async", _fake_async)

    mcp = FastMCP("test")
    # ctx is never dereferenced by the patched handler, so a sentinel is fine.
    # Default prefix="" / sim_name="" → bare single-sim registration, the
    # exact wire shape production uses for a one-sim session.
    srv.register_sim_tools(mcp, ctx=object())  # type: ignore[arg-type]

    # 1. Correct payload: implementation_paths must reach the handler verbatim.
    paths = ["/workspace/config.json", "/workspace/mc_config.json"]
    anyio.run(mcp.call_tool, "submit", {"implementation_paths": paths})
    assert seen.get("implementation_paths") == paths, (
        "submit() dropped implementation_paths before the handler saw them"
    )

    # 2. Legacy bug payload: {"wait": True} must be REJECTED, not silently
    #    routed (it carries no implementation_paths). This is the exact
    #    shape the stale image sent.
    seen.clear()
    with pytest.raises(Exception) as excinfo:
        anyio.run(mcp.call_tool, "submit", {"wait": True})
    assert "implementation_paths" in str(excinfo.value), (
        "submit({'wait': True}) should fail on the required "
        f"implementation_paths field; got: {excinfo.value}"
    )
    assert "implementation_paths" not in seen, (
        "handle_submit_async must NOT be invoked for an invalid payload"
    )


def test_all_known_sims_have_connectors() -> None:
    """At least the production champsim + scaffolds for all other sims."""
    assert "champsim" in _SIMS, "ChampSim must have a connector — production sim"
    # Scaffolds — these may grow real implementations later but for now
    # are required to at least expose the universal connector surface.
    scaffolds = {"astrasim", "dramsys", "gem5", "ramulator", "scalesim", "timeloop"}
    actual = set(_SIMS)
    missing = scaffolds - actual
    assert not missing, (
        f"Expected scaffolded connectors for {missing}. "
        "If a sim is intentionally connector-less, update this test."
    )


# ===========================================================================
# Multi-simulator tool-namespacing (docs/multi_sim_design.md §1-4)
#
# These tests register the canonical tools against a REAL FastMCP via
# ``server_subprocess.register_sim_tools`` and inspect the resulting
# ``tools/list`` + call a tool. No sim container is started: the only
# tools we call (``session_end``, ``check_submission``) don't touch a
# container.
# ===========================================================================

from archbench.core.anonymizer import Anonymizer  # noqa: E402
from archbench.core.challenge import Challenge, EvalConfig  # noqa: E402
from simulators.champsim.connector.handlers import SubmitContext  # noqa: E402
from simulators.champsim.connector.server_subprocess import (  # noqa: E402
    register_sim_tools,
)
from simulators.champsim.connector.tool_schema import (  # noqa: E402
    TOOLS,
    render_sim_name,
)


@dataclass
class _FakeContainer:
    """Stand-in for ContainerManager — only ``.name`` is touched by the
    no-container tools we exercise here."""

    name_: str = "fake"

    @property
    def name(self) -> str:
        return self.name_


@dataclass
class _FakePlugin:
    """Stand-in for SimulatorPlugin. ``docker_image`` is read by
    ``server_subprocess.main`` (not used in these register-only tests, but
    kept for parity); ``submission_files`` lets handle_submit validate."""

    docker_image: str = "fake:latest"
    submission_files_: list[str] = field(default_factory=lambda: ["main.cc"])

    def submission_files(self, _challenge):
        return list(self.submission_files_)


def _make_ctx(tmp_path: Path, *, submission_id_prefix: str = "") -> SubmitContext:
    """Minimal SubmitContext with fakes (no docker)."""
    ch = Challenge(
        id="t", name="t", simulator="champsim", prompt="",
        starter_files=["main.cc"], output_files=["main.cc"],
        eval=EvalConfig(metric="ipc", max_submissions=5, max_code_lines=1000),
        simulator_config={},
        source_blocklist=[],
        challenge_dir=tmp_path,
    )
    return SubmitContext(
        challenge=ch,
        challenge_dir=tmp_path,
        plugin=_FakePlugin(),
        agent=_FakeContainer(name_="agent"),
        sim=_FakeContainer(name_="sim"),
        anonymizer=Anonymizer.disabled(),
        results_dir=tmp_path,
        submission_id_prefix=submission_id_prefix,
    )


def _new_mcp():
    """A fresh FastMCP instance, or skip if the mcp package isn't installed."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:  # pragma: no cover - mcp is a hard dep in CI
        pytest.skip("mcp package not installed")
    return FastMCP("archbench-test", host="127.0.0.1", port=0)


def _list_tools(mcp) -> list:
    """Synchronously drain FastMCP's async ``list_tools``."""
    return asyncio.run(mcp.list_tools())


def _call_tool(mcp, name: str, args: dict) -> str:
    """Synchronously call a tool; return the text payload of the first block."""
    result = asyncio.run(mcp.call_tool(name, args))
    # FastMCP returns (list[TextContent], structured_dict).
    blocks = result[0]
    return blocks[0].text


# ---------------------------------------------------------------------------
# Single-sim — BARE names + byte-for-byte descriptions (the compat guarantee)
# ---------------------------------------------------------------------------


def test_single_sim_registers_bare_tool_names(tmp_path) -> None:
    """🔴 CLAUDE.md §1.4-6 compatibility guarantee.

    With one sim bound the registration uses an EMPTY prefix, so the tool
    names the agent sees are exactly the bare canonical 6 — no
    ``champsim_`` decoration, no behavior change from before multi-sim
    namespacing existed.
    """
    mcp = _new_mcp()
    ctx = _make_ctx(tmp_path)
    register_sim_tools(mcp, ctx, prefix="", sim_name="")

    names = {t.name for t in _list_tools(mcp)}
    assert names == CANONICAL_TOOLS, (
        f"single-sim mode must register the bare canonical names; got {names}"
    )


def test_single_sim_descriptions_are_byte_for_byte_unchanged(tmp_path) -> None:
    """Single-sim render (sim_name="") must reproduce the pre-namespacing
    wire text exactly — no leftover ``{sim_name}`` token and no double
    spaces from the collapsed placeholder.

    This is the description half of the compatibility guarantee: an agent
    in single-sim mode sees identical tool + parameter docs.
    """
    mcp = _new_mcp()
    ctx = _make_ctx(tmp_path)
    register_sim_tools(mcp, ctx, prefix="", sim_name="")

    by_name = {t.name: t for t in _list_tools(mcp)}
    schema_by_name = {t.name: t for t in TOOLS}
    for name, tool in by_name.items():
        # Top-level description matches the schema rendered with "".
        assert tool.description == render_sim_name(
            schema_by_name[name].description, "",
        )
        assert "{sim_name}" not in tool.description
        assert "  " not in tool.description, (
            f"{name}: double space leaked from empty {{sim_name}} placeholder"
        )
        # Parameter descriptions reach the wire (CLAUDE.md §1.5 — the
        # Registration gotcha). Every declared param must carry its
        # rendered description in the inputSchema.
        props = tool.inputSchema.get("properties", {})
        for param, pspec in schema_by_name[name].parameters.items():
            assert param in props, f"{name}.{param} missing from inputSchema"
            assert props[param].get("description") == render_sim_name(
                pspec["description"], "",
            ), f"{name}.{param} description not wired through to FastMCP"


# ---------------------------------------------------------------------------
# Multi-sim — prefixed union + per-namespace dispatch
# ---------------------------------------------------------------------------


def test_multi_sim_registers_prefixed_union(tmp_path) -> None:
    """Two sims on one FastMCP → the tool list is the prefixed union.

    Mirrors the canonical multi-sim example in docs/multi_sim_design.md:
    ``dramsys_submit`` + ``timeloop_submit`` + ... — 12 tools, none of
    them bare.
    """
    mcp = _new_mcp()
    ctx_a = _make_ctx(tmp_path / "a", submission_id_prefix="dramsys_")
    ctx_b = _make_ctx(tmp_path / "b", submission_id_prefix="timeloop_")
    register_sim_tools(mcp, ctx_a, prefix="dramsys_", sim_name="dramsys")
    register_sim_tools(mcp, ctx_b, prefix="timeloop_", sim_name="timeloop")

    names = {t.name for t in _list_tools(mcp)}
    expected = (
        {f"dramsys_{t}" for t in CANONICAL_TOOLS}
        | {f"timeloop_{t}" for t in CANONICAL_TOOLS}
    )
    assert names == expected, f"multi-sim union mismatch; got {sorted(names)}"
    # No bare canonical name leaks through in multi-sim mode.
    assert not (names & CANONICAL_TOOLS), (
        "bare (unprefixed) tool names must not appear when >1 sim is bound"
    )
    # And the descriptions are sim-named (placeholder rendered, not empty).
    by_name = {t.name: t for t in _list_tools(mcp)}
    assert "dramsys" in by_name["dramsys_browse_simulator"].description
    assert "timeloop" in by_name["timeloop_browse_simulator"].description


def test_multi_sim_calls_route_to_correct_sim(tmp_path) -> None:
    """Calling one tool from EACH namespace dispatches to that sim's ctx.

    Proof of routing: each sim's ``session_end`` writes its marker into
    that sim's own ``results_dir``; and ``check_submission`` is answered by
    the namespace it was called on. Both are no-container tools, so this
    runs without docker.
    """
    mcp = _new_mcp()
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    ctx_a = _make_ctx(dir_a, submission_id_prefix="dramsys_")
    ctx_b = _make_ctx(dir_b, submission_id_prefix="timeloop_")
    register_sim_tools(mcp, ctx_a, prefix="dramsys_", sim_name="dramsys")
    register_sim_tools(mcp, ctx_b, prefix="timeloop_", sim_name="timeloop")

    # session_end on namespace A writes A's marker, not B's.
    out_a = json.loads(_call_tool(mcp, "dramsys_session_end", {"reason": "a-done"}))
    assert out_a["status"] == "ok"
    assert (dir_a / "session_end.requested").exists()
    assert not (dir_b / "session_end.requested").exists()

    # check_submission on namespace B is answered by B's ctx (unknown id
    # → a well-formed "unknown" reply, proving the tool is wired + callable).
    out_b = json.loads(
        _call_tool(mcp, "timeloop_check_submission", {"submission_id": "x"})
    )
    assert out_b["status"] == "unknown"
    assert out_b["submission_id"] == "x"


def test_multi_sim_submission_ids_do_not_collide(tmp_path) -> None:
    """Per-sim ``submission_id_prefix`` makes ids globally unique.

    Two sims sharing one ``.in_flight/`` dir + ``submit_outcomes.jsonl``
    must mint distinct ids (CLAUDE.md §1.10) — else the grace-period could
    clear the wrong sim's in-flight marker. Single-sim leaves the prefix
    empty so ids stay ``sub_001``.
    """
    from simulators.champsim.connector.handlers.submit import _next_submission_id

    ctx_default = _make_ctx(tmp_path / "d")  # single-sim: empty prefix
    ctx_a = _make_ctx(tmp_path / "a", submission_id_prefix="dramsys_")
    ctx_b = _make_ctx(tmp_path / "b", submission_id_prefix="timeloop_")

    assert _next_submission_id(ctx_default) == "sub_001"
    assert _next_submission_id(ctx_a) == "dramsys_sub_001"
    assert _next_submission_id(ctx_b) == "timeloop_sub_001"
