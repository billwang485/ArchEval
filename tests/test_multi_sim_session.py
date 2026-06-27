"""Multi-simulator consumer-side wiring (docs/multi_sim_design.md §1, §3).

Covers the two pieces of Stage A:

  1. ``load_challenge`` parses a 2-sim challenge (``simulators: [a, b]``) into
     ``challenge.simulator == a`` (primary), ``challenge.extra_simulators ==
     [b]``, and the ordered ``challenge.simulators == [a, b]`` property.
     Single-sim challenges (``simulator: a``) stay ``[a]`` — back-compat.

  2. ``session._start_mcp_server`` assembles the repeated per-sim connector
     args (``--sim-name`` / ``--simulator`` / ``--sim-container``) in lockstep
     for the primary sim (from ``ctx``) plus every entry in ``extra_sims``.
     This is the consumer side of the plumbing the connector
     (``server_subprocess.register_sim_tools``) consumes to register the
     ``<sim>_``-prefixed tools. We capture the spawned argv by stubbing
     ``subprocess.Popen`` — no container, no real MCP server.

These are pure-wiring tests; the full container round-trip is the live smoke
(``archbench run challenges/dramsys_ramulator_crossval mini``), not a unit test.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass

import pytest

from archbench.core.challenge import load_challenge


# ---------------------------------------------------------------------------
# 1. load_challenge multi-sim parse
# ---------------------------------------------------------------------------


def _write_challenge(tmp_path, yaml_text, starter_files=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "challenge.yaml").write_text(textwrap.dedent(yaml_text).lstrip())
    starter = tmp_path / "starter"
    starter.mkdir(exist_ok=True)
    for fname, content in (starter_files or {"config.json": "{}"}).items():
        (starter / fname).write_text(content)
    return tmp_path


def test_multi_sim_challenge_parses_primary_and_extras(tmp_path):
    """``simulators: [dramsys, ramulator]`` → primary dramsys + extra ramulator."""
    _write_challenge(tmp_path, """
        id: crossval
        name: crossval
        simulators: [dramsys, ramulator]
        prompt: ""
        input: {starter_files: [config.json]}
        output: {files: [config.json]}
        simulator_config: {trace: example.stl}
    """)
    ch = load_challenge(tmp_path)
    assert ch.simulator == "dramsys"            # primary = first
    assert ch.extra_simulators == ["ramulator"]  # the rest
    assert ch.simulators == ["dramsys", "ramulator"]  # ordered full list


def test_extra_simulators_alongside_scalar_simulator(tmp_path):
    """The ``extra_simulators:`` alias works alongside a scalar ``simulator:``."""
    _write_challenge(tmp_path, """
        id: crossval2
        name: crossval2
        simulator: dramsys
        extra_simulators: [ramulator]
        prompt: ""
        input: {starter_files: [config.json]}
        output: {files: [config.json]}
        simulator_config: {trace: example.stl}
    """)
    ch = load_challenge(tmp_path)
    assert ch.simulators == ["dramsys", "ramulator"]


def test_single_sim_challenge_has_no_extras(tmp_path):
    """Back-compat: a scalar ``simulator:`` yields ``simulators == [it]``."""
    _write_challenge(tmp_path, """
        id: single
        name: single
        simulator: champsim
        prompt: ""
        input: {starter_files: [config.json]}
        output: {files: [config.json]}
        simulator_config: {script: simulate.sh}
    """)
    ch = load_challenge(tmp_path)
    assert ch.simulator == "champsim"
    assert ch.extra_simulators == []
    assert ch.simulators == ["champsim"]


def test_simulators_list_dedups_and_preserves_order(tmp_path):
    """Duplicate names collapse but order is preserved (primary stays first)."""
    _write_challenge(tmp_path, """
        id: dup
        name: dup
        simulators: [dramsys, dramsys, ramulator, ramulator]
        prompt: ""
        input: {starter_files: [config.json]}
        output: {files: [config.json]}
        simulator_config: {trace: example.stl}
    """)
    ch = load_challenge(tmp_path)
    assert ch.simulators == ["dramsys", "ramulator"]


# ---------------------------------------------------------------------------
# 2. session._start_mcp_server assembles the per-sim connector args
# ---------------------------------------------------------------------------


@dataclass
class _FakeContainer:
    name: str


@dataclass
class _FakeChallenge:
    simulator: str
    challenge_dir: object


@dataclass
class _FakeAnon:
    enabled: bool = False


@dataclass
class _FakeCtx:
    challenge: object
    challenge_dir: object
    agent: object
    sim: object
    anonymizer: object


@dataclass
class _FakeManager:
    """Stand-in for ContainerManager — only ``.name`` is read by
    ``_start_mcp_server`` when it builds the extra-sim args."""

    name: str


def _capture_mcp_argv(monkeypatch, tmp_path, *, extra_sims):
    """Run ``_start_mcp_server`` with subprocess.Popen stubbed; return argv."""
    from archbench.runtimes import session as sess

    captured: dict = {}

    class _StubPopen:
        def __init__(self, cmd, *a, **kw):
            captured["cmd"] = cmd
            self.pid = 4321

        def poll(self):
            return None

    monkeypatch.setattr(sess.subprocess, "Popen", _StubPopen)

    ctx = _FakeCtx(
        challenge=_FakeChallenge(simulator="dramsys", challenge_dir=tmp_path),
        challenge_dir=tmp_path,
        agent=_FakeContainer(name="agent_ctr"),
        sim=_FakeContainer(name="dramsys_ctr"),
        anonymizer=_FakeAnon(enabled=False),
    )
    sess._start_mcp_server(
        ctx, 9999, tmp_path / "mcp.log", tmp_path, extra_sims=extra_sims,
    )
    return captured["cmd"]


def _grouped_sim_args(cmd):
    """Extract [(sim_name, simulator, sim_container), ...] from the argv.

    The connector pairs --sim-name/--simulator/--sim-container positionally,
    so they appear interleaved but in lockstep. Return them as ordered tuples
    keyed by the order each --sim-name was emitted.
    """
    names, sims, ctrs = [], [], []
    i = 0
    while i < len(cmd):
        if cmd[i] == "--sim-name":
            names.append(cmd[i + 1]); i += 2
        elif cmd[i] == "--simulator":
            sims.append(cmd[i + 1]); i += 2
        elif cmd[i] == "--sim-container":
            ctrs.append(cmd[i + 1]); i += 2
        else:
            i += 1
    return list(zip(names, sims, ctrs))


def test_start_mcp_server_single_sim_emits_one_sim_triple(monkeypatch, tmp_path):
    """No extra_sims → exactly one (sim_name, simulator, container) triple,
    all derived from ctx. This is the single-sim wire shape (unchanged)."""
    cmd = _capture_mcp_argv(monkeypatch, tmp_path, extra_sims=None)
    triples = _grouped_sim_args(cmd)
    assert triples == [("dramsys", "dramsys", "dramsys_ctr")]


def test_start_mcp_server_multi_sim_emits_a_triple_per_sim(monkeypatch, tmp_path):
    """Primary (from ctx) + each extra_sims entry → one triple per sim, in
    lockstep. This is the consumer side of the multi-sim plumbing: the
    connector reads these repeated args and registers <sim>_-prefixed tools."""
    extra = [("ramulator", "ramulator", _FakeManager(name="ramulator_ctr"))]
    cmd = _capture_mcp_argv(monkeypatch, tmp_path, extra_sims=extra)
    triples = _grouped_sim_args(cmd)
    assert triples == [
        ("dramsys", "dramsys", "dramsys_ctr"),
        ("ramulator", "ramulator", "ramulator_ctr"),
    ]
    # Equal counts of each per-sim flag (paired positionally — the connector
    # parser.error()s if they ever diverge).
    assert cmd.count("--sim-name") == cmd.count("--simulator") == cmd.count("--sim-container") == 2


# ---------------------------------------------------------------------------
# mini-runtime is_multi_sim detection (runtimes/mini/src/main.py).
# Regression for the bug where `len(submit | submit_and_wait) > 1` flagged
# EVERY single-sim run as multi-sim (a single sim advertises both), which
# disabled the single-sim early-stop. The fix counts only `_submit`-suffixed
# tools (one per sim), mirroring the server's `len(simulators) > 1`.
# ---------------------------------------------------------------------------


def _load_mini_main():
    """Import runtimes/mini/src/main.py by path (not a package; loaded under a
    non-__main__ name so its `if __name__ == '__main__'` entrypoint stays dormant)."""
    import importlib.util
    import pathlib
    p = (pathlib.Path(__file__).resolve().parents[1]
         / "runtimes" / "mini" / "src" / "main.py")
    spec = importlib.util.spec_from_file_location("mini_main_under_test", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_is_multi_sim_false_for_single_sim():
    """The GATE: single-sim must be detected as single-sim. A single sim's
    TOOLS advertises BOTH `submit` and `submit_and_wait`; counting their union
    would give 2 → wrongly multi-sim → single-sim early-stop disabled."""
    main = _load_mini_main()
    single = {"submit", "submit_and_wait", "check_submission",
              "session_end", "browse_simulator", "read_simulator_file"}
    assert main._is_multi_sim(single) is False


def test_is_multi_sim_true_for_two_sims():
    main = _load_mini_main()
    multi = {
        "dramsys_submit", "dramsys_submit_and_wait", "dramsys_check_submission",
        "dramsys_session_end", "dramsys_browse_simulator",
        "dramsys_read_simulator_file",
        "ramulator_submit", "ramulator_submit_and_wait",
        "ramulator_check_submission", "ramulator_session_end",
        "ramulator_browse_simulator", "ramulator_read_simulator_file",
    }
    assert main._is_multi_sim(multi) is True
