"""Standalone MCP server subprocess.

Run by ``archbench/runtimes/session.py::_start_mcp_server``. Hosts the MCP tools
(submit, submit_and_wait, check_submission, session_end, browse_simulator,
read_simulator_file) on the given port, using the handlers in
``simulators.champsim.connector.handlers``.

The set of tools advertised here is the canonical contract documented in
``simulators/champsim/connector/tool_schema.py``; if you add/remove a
tool, update both sides.

``--results-dir`` is the per-run dir on the host (results/<challenge>/<run>/)
where ``submit_outcomes.jsonl`` and ``session_end.requested`` are persisted.

Multi-simulator
---------------
The argparse accepts the per-sim arguments (``--simulator`` /
``--sim-container`` / ``--sim-name``) repeatably, so a single MCP server can
bind N simulator containers in one session (see
``docs/multi_sim_design.md``). When more than one sim is bound, each sim's
tools are registered under a ``<sim_name>_`` prefix (``dramsys_submit``,
``timeloop_submit``, ...). With exactly one sim — the only configuration
the harness produces today — the prefix is the empty string and the tools
keep their BARE canonical names (``submit``, ``browse_simulator``, ...).
That single-sim path is byte-for-byte unchanged from before namespacing
existed; see ``register_sim_tools`` and ``tests/test_connectors_smoke.py``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Annotated, Any, Optional

from pydantic import Field

from archbench.core.anonymizer import Anonymizer
from archbench.core.challenge import load_challenge
from archbench.core.container import ContainerManager, ContainerConfig
from simulators.champsim.connector.handlers import (
    SubmitContext,
    handle_browse,
    handle_check_submission,
    handle_read,
    handle_session_end,
    handle_submit_and_wait,
    handle_submit_async,
)
from simulators.champsim.connector.tool_schema import TOOLS, render_sim_name
from archbench.simulators import get_plugin


# Look up rich descriptions from the canonical TOOLS list so the
# tool registrations below can't drift from tool_schema.py.
_TOOL_BY_NAME = {t.name: t for t in TOOLS}

def _tool_desc(name: str, sim_name: str = "") -> str:
    return render_sim_name(_TOOL_BY_NAME[name].description, sim_name)

def _param_desc(tool_name: str, param: str, sim_name: str = "") -> str:
    return render_sim_name(
        _TOOL_BY_NAME[tool_name].parameters[param]["description"], sim_name
    )


log = logging.getLogger("archbench.mcp.subprocess")


def register_sim_tools(
    mcp: Any, ctx: SubmitContext, *, prefix: str = "", sim_name: str = "",
    allowed: Optional[set] = None,
) -> None:
    """Register the canonical tools for ONE simulator on ``mcp``.

    ``allowed`` is a per-tier tool allowlist (``Challenge.tier_tools``).
    ``None`` registers every canonical tool (back-compat). A set restricts
    registration to those BARE canonical names — e.g. L2 passes
    ``{submit, submit_and_wait, check_submission, session_end}`` so the agent
    gets the Oracle + lifecycle but NOT browse_simulator / read_simulator_file
    (it explores the real sim itself). This FILTERS registration only; it does
    NOT mutate ``tool_schema.py::TOOLS``, the single source of truth (§1.4).

    Parameters
    ----------
    mcp
        The ``FastMCP`` instance to register against.
    ctx
        The ``SubmitContext`` bound to this sim's container. Each call
        closes its handlers over THIS ctx, so a multi-sim server keeps one
        ``ctx`` per sim and the tool a given namespace dispatches to the
        right container.
    prefix
        Prepended to every tool name. ``""`` (single-sim) keeps the bare
        canonical names; ``"dramsys_"`` (multi-sim) yields ``dramsys_submit``
        etc.
    sim_name
        The sim name used to render ``{sim_name}`` placeholders in the
        descriptions. Empty string (single-sim) collapses the placeholder
        away to the exact pre-namespacing text.

    The single source of truth is still ``tool_schema.py::TOOLS`` (CLAUDE.md
    §1.4); the prefix + ``{sim_name}`` render are registration-time
    decorations, NOT a mutation of TOOLS. Per-parameter descriptions are
    wired through with ``Annotated[T, Field(description=...)]`` so they
    reach the wire (CLAUDE.md §1.5 / the "Registration gotcha").

    Implementation note: the per-parameter ``Annotated[..., Field(...)]``
    objects are attached to each handler's ``__annotations__`` AFTER the
    function is defined, rather than written inline in the ``def``. This is
    required because this module uses ``from __future__ import annotations``:
    inline annotations would be stored as *strings* and FastMCP re-evaluates
    them with ``inspect.signature(fn, eval_str=True)`` against the module
    globals only — where the per-sim ``sim_name`` closure local is NOT
    visible, so the eval would raise ``InvalidSignature``. Attaching the
    already-built ``Annotated`` objects sidesteps the string round-trip
    while still flowing the descriptions through ``Field(description=...)``.
    """

    def submit(implementation_paths) -> str:
        state = handle_submit_async(ctx, implementation_paths)
        return json.dumps(state)

    def submit_and_wait(implementation_paths) -> str:
        return json.dumps(handle_submit_and_wait(ctx, implementation_paths))

    def check_submission(submission_id) -> str:
        return json.dumps(handle_check_submission(ctx, submission_id))

    def session_end(reason="") -> str:
        return json.dumps(handle_session_end(ctx, reason))

    def browse_simulator(path) -> str:
        return handle_browse(ctx, path)

    def read_simulator_file(path) -> str:
        return handle_read(ctx, path)

    # (handler, {param: (python_type, schema_tool_name, schema_param_name)})
    # Tool descriptions (top-level + per-parameter) are sourced from
    # ``tool_schema.py::TOOLS`` so the wire schema the LLM sees carries the
    # full guidance (e.g. "Path MUST be absolute"). Previously this was
    # implicit-from-docstring and dropped the parameter-level descriptions
    # entirely, leading models to try relative paths against
    # read_simulator_file.
    handlers: dict = {
        "submit": (submit, {"implementation_paths": list[str]}),
        "submit_and_wait": (submit_and_wait, {"implementation_paths": list[str]}),
        "check_submission": (check_submission, {"submission_id": str}),
        "session_end": (session_end, {"reason": str}),
        "browse_simulator": (browse_simulator, {"path": str}),
        "read_simulator_file": (read_simulator_file, {"path": str}),
    }

    # Register every canonical tool. Drive the loop off TOOLS (the single
    # source of truth) rather than the local dict so a tool added to the
    # schema without a handler here is a loud KeyError, not a silent drop.
    for schema in TOOLS:
        if allowed is not None and schema.name not in allowed:
            continue  # per-tier allowlist (e.g. L2 drops browse/read)
        fn, param_types = handlers[schema.name]
        annotations: dict = {"return": str}
        for param, py_type in param_types.items():
            annotations[param] = Annotated[
                py_type,
                Field(description=_param_desc(schema.name, param, sim_name)),
            ]
        fn.__annotations__ = annotations
        mcp.add_tool(
            fn,
            name=f"{prefix}{schema.name}",
            description=_tool_desc(schema.name, sim_name),
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--challenge-dir", required=True)
    # Per-sim arguments are repeatable (action="append") to support
    # multi-sim sessions. The common single-sim case passes each exactly
    # once and behaves identically to the pre-multi-sim contract.
    parser.add_argument(
        "--simulator", action="append", required=True,
        help="Simulator plugin name (e.g. champsim). Repeat once per sim "
             "for a multi-sim session, paired positionally with "
             "--sim-container / --sim-name.",
    )
    parser.add_argument(
        "--sim-container", action="append", required=True,
        help="Running sim container id/name. Repeat once per sim, paired "
             "positionally with --simulator / --sim-name.",
    )
    parser.add_argument(
        "--sim-name", action="append", default=None,
        help="Logical name used for the tool-name prefix + {sim_name} "
             "description rendering in multi-sim mode (e.g. 'dramsys'). "
             "Repeat once per sim. Defaults to the --simulator value if "
             "omitted. Ignored for prefixing when only one sim is bound.",
    )
    parser.add_argument("--agent-container", required=True)
    parser.add_argument("--anonymize", default="False")
    parser.add_argument(
        "--results-dir", default=None,
        help="Host path to results/<challenge>/<run>/ — where "
             "submit_outcomes.jsonl and session_end.requested are written.",
    )
    parser.add_argument(
        "--tool", action="append", default=None,
        help="Per-tier MCP tool allowlist (Challenge.tier_tools). Repeatable. "
             "If given, ONLY these canonical tool names are registered "
             "(e.g. submit, submit_and_wait, check_submission, session_end for "
             "L2 — dropping browse_simulator/read_simulator_file). Omit = all.",
    )
    args = parser.parse_args(argv)

    # Per-tier tool allowlist: None = all tools. Validate against TOOLS (the
    # single source of truth) so a typo'd name fails loud, not as a silently
    # empty tool surface.
    allowed_tools = set(args.tool) if args.tool else None
    if allowed_tools is not None:
        known = {t.name for t in TOOLS}
        unknown = allowed_tools - known
        if unknown:
            parser.error(
                f"--tool names not in tool_schema.TOOLS: {sorted(unknown)} "
                f"(known: {sorted(known)})"
            )

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] mcp %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    simulators = args.simulator
    sim_containers = args.sim_container
    # Default each sim_name to its plugin name when not given explicitly.
    sim_names = args.sim_name if args.sim_name is not None else list(simulators)
    if not (len(simulators) == len(sim_containers) == len(sim_names)):
        parser.error(
            "--simulator, --sim-container and (if given) --sim-name must be "
            f"repeated the same number of times; got {len(simulators)} "
            f"simulator(s), {len(sim_containers)} container(s), "
            f"{len(sim_names)} name(s)."
        )

    challenge_dir = Path(args.challenge_dir)
    challenge = load_challenge(challenge_dir)

    agent_mgr = ContainerManager(ContainerConfig(
        image="", container_name=args.agent_container,
    ))
    agent_mgr._running = True

    # Anonymizer is challenge-scoped (one trace mapping per challenge), so it
    # is shared across all sims bound in this session.
    anon = Anonymizer.disabled()
    if args.anonymize == "True" and challenge.simulator == "champsim":
        from simulators.champsim.anonymization.build_anonymizer import (
            load_champsim_anonymizer,
        )
        anon = load_champsim_anonymizer()

    results_dir = Path(args.results_dir) if args.results_dir else None
    if results_dir is not None:
        try:
            results_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.warning("results_dir %s could not be created: %s", results_dir, e)

    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        log.error(
            "mcp package not installed. `pip install 'mcp>=1.0'` then retry."
        )
        return 1

    mcp = FastMCP("archbench", host="127.0.0.1", port=args.port)

    # Prefix is applied ONLY when more than one sim is bound. Single-sim →
    # empty prefix → bare canonical names → zero behavior change.
    multi = len(simulators) > 1
    if multi:
        # Forward guard (reviewer): the tool-name prefix is derived from
        # sim_name, so two sims sharing a name (e.g. two champsim instances
        # both defaulting their sim_name to "champsim") would collide on the
        # prefix AND on submission_id_prefix — silently clobbering each
        # other's registered tools and aliasing their in-flight markers
        # (CLAUDE.md §1.10). Require sim_names to be unique; fail loud.
        if len(set(sim_names)) != len(sim_names):
            parser.error(
                "multi-sim sessions require unique --sim-name values (the "
                "tool-name prefix is derived from it); got "
                f"{sim_names!r}. Pass an explicit --sim-name per sim."
            )
    for sim_name, simulator, sim_container in zip(
        sim_names, simulators, sim_containers,
    ):
        plugin = get_plugin(simulator)
        # Reconstruct a ContainerManager pointing at the already-running
        # sim container (we don't own start/stop here — session.py does).
        sim_mgr = ContainerManager(ContainerConfig(
            image=plugin.docker_image, container_name=sim_container,
        ))
        sim_mgr._running = True
        ctx = SubmitContext(
            challenge=challenge,
            challenge_dir=challenge_dir,
            plugin=plugin,
            agent=agent_mgr,
            sim=sim_mgr,
            anonymizer=anon,
            results_dir=results_dir,
            submission_id_prefix=(f"{sim_name}_" if multi else ""),
        )
        prefix = f"{sim_name}_" if multi else ""
        # Single-sim byte-for-byte invariant (CLAUDE.md §1.4-6): when exactly
        # one sim is bound the prefix MUST be empty so the agent sees the bare
        # canonical tool names (submit, browse_simulator, ...) — identical to
        # the pre-namespacing contract. Assert it here so any future edit that
        # accidentally prefixes a single-sim session fails loud, not silent.
        if not multi:
            assert prefix == "", (
                "single-sim session must register BARE tool names "
                f"(empty prefix); got prefix={prefix!r}"
            )
        register_sim_tools(
            mcp, ctx, prefix=prefix, sim_name=(sim_name if multi else ""),
            allowed=allowed_tools,
        )
        log.info(
            "registered tools for sim %r (container=%s) with prefix %r "
            "(allowlist=%s)",
            sim_name, sim_container, prefix,
            sorted(allowed_tools) if allowed_tools else "ALL",
        )

    # streamable-http is more robust than SSE for long-running tool calls
    # (submit can take 1-7 min). Mirrors the legacy mcp_server.py choice.
    log.info("starting MCP streamable-http on port %d", args.port)
    mcp.run(transport="streamable-http")
    return 0


if __name__ == "__main__":
    sys.exit(main())
