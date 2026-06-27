"""P0 sanity test: every public symbol of archbench.core resolves."""


def test_top_level_package():
    import archbench
    assert hasattr(archbench, "__version__")


def test_core_imports():
    from archbench.core import (
        SimulatorPlugin,
        AgentRuntime,
        RuntimeAuth,
        Provenance,
        SubmitOutcome,
        OutcomeReport,
        Anonymizer,
    )
    # ABCs cannot be instantiated directly
    import pytest
    with pytest.raises(TypeError):
        SimulatorPlugin()  # type: ignore[abstract]
    with pytest.raises(TypeError):
        AgentRuntime()  # type: ignore[abstract]


def test_simulator_registry_after_p2():
    """ChampSim is the canonical first simulator. Phase H added several
    scaffold simulators (astrasim/dramsys/gem5/scalesim/timeloop) for
    future accelerator work; assert the BUILT-OUT simulator (champsim)
    is present and the scaffolds load without crashing.
    """
    from archbench.simulators import _REGISTRY, get_plugin
    assert "champsim" in _REGISTRY
    plugin = get_plugin("champsim")
    assert plugin.name == "champsim"
    # Scaffolds are allowed but not required to be functional.
    for sim in _REGISTRY:
        assert isinstance(sim, str) and sim


def test_runtime_registry_after_p4():
    """After P4, five runtimes are registered (claude_code, codex,
    gemini, archharness, mini)."""
    from archbench.runtimes import _REGISTRY, get_runtime
    assert set(_REGISTRY.keys()) == {
        "claude_code", "codex", "gemini", "archharness", "mini",
    }
    rt = get_runtime("claude_code")
    assert rt.name == "claude_code"
    assert rt.docker_image == "localhost/archbench-agent:v6"


def test_cli_entry_point_runs():
    from archbench.cli import main
    # Calling with no args prints help and returns 0
    assert main([]) == 0
