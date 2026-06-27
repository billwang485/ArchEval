"""ChampSim integration tests — require docker + the :v6 image.

Run with: pytest -m requires_docker --run-docker tests/test_champsim_integration.py

What's covered here:
  - ensure_image resolves :v6 from the NFS tarball.
  - verify.sh runs cleanly inside a fresh container.
  - cleanup.sh resets state, and verify still passes after.

What's NOT covered here (lands in P3 once cache_replacement is ported):
  - stock-LRU bit-for-bit equivalence test against ChampSim's built-in
    `lru` module — that's the structural fix from
    `docs/lessons_learned.md §2`. It needs a real challenge starter to
    compile, which arrives with P3.
"""

from pathlib import Path

import pytest

from archbench.core.container import (
    ContainerConfig,
    ContainerManager,
    ensure_image,
    get_image_digest,
)
from simulators.champsim import ChampSimPlugin


from archbench.core.container import default_tar_search_dirs
_LEGACY_TAR_DIRS = default_tar_search_dirs()


@pytest.mark.requires_docker
def test_v6_image_loadable():
    """ensure_image returns a digest for :v6, loading from NFS if needed.

    Digest format varies by engine (docker prefixes 'sha256:'; podman
    sometimes doesn't). Just assert it's non-empty hex-like and stable
    across calls.
    """
    digest = ensure_image(
        "localhost/archbench-champsim:v6",
        tar_search_dirs=_LEGACY_TAR_DIRS,
    )
    bare = digest.removeprefix("sha256:")
    assert len(bare) >= 32, f"digest too short: {digest!r}"
    assert all(c in "0123456789abcdef" for c in bare.lower()), \
        f"digest not hex: {digest!r}"
    # Stable across ensure_image + a direct inspect
    assert get_image_digest("localhost/archbench-champsim:v6") == digest


@pytest.mark.requires_docker
def test_verify_simulator_on_fresh_container():
    """A freshly started :v6 container passes plugin.verify_simulator."""
    plugin = ChampSimPlugin()
    ensure_image(plugin.docker_image, tar_search_dirs=_LEGACY_TAR_DIRS)

    cfg = ContainerConfig.with_run_id(plugin.docker_image, "p2_verify")
    sim = ContainerManager(cfg)
    sim.start()
    try:
        errors = plugin.verify_simulator(sim)
        assert errors == [], f"verify.sh complained: {errors}"
    finally:
        sim.stop()


@pytest.mark.requires_docker
def test_cleanup_then_verify_still_ok():
    """After cleanup_simulator(), verify_simulator() must still pass.

    The contract from plugin_base.py: cleanup is idempotent + leaves
    the container in a verify-passing state.
    """
    plugin = ChampSimPlugin()
    ensure_image(plugin.docker_image, tar_search_dirs=_LEGACY_TAR_DIRS)

    cfg = ContainerConfig.with_run_id(plugin.docker_image, "p2_cleanup")
    sim = ContainerManager(cfg)
    sim.start()
    try:
        plugin.cleanup_simulator(sim)              # should not raise
        errors_after = plugin.verify_simulator(sim)
        assert errors_after == [], (
            f"verify failed after cleanup: {errors_after}"
        )
        # And cleanup is idempotent
        plugin.cleanup_simulator(sim)
        plugin.cleanup_simulator(sim)
        assert plugin.verify_simulator(sim) == []
    finally:
        sim.stop()


@pytest.mark.requires_docker
def test_cleanup_removes_stale_candidate_dir():
    """If a stale candidate* component dir is present, verify catches it,
    cleanup removes it, and re-verify is OK. Regression: legacy plugin
    had inline Python that re-implemented this; we now route through one
    cleanup.sh."""
    plugin = ChampSimPlugin()
    ensure_image(plugin.docker_image, tar_search_dirs=_LEGACY_TAR_DIRS)

    cfg = ContainerConfig.with_run_id(plugin.docker_image, "p2_stale")
    sim = ContainerManager(cfg)
    sim.start()
    try:
        # Plant a stale candidate* dir under replacement/
        sim.exec(
            "mkdir -p /work/runtimes/champsim/replacement/candidate_BOGUS && "
            "touch /work/runtimes/champsim/replacement/candidate_BOGUS/leftover.h",
            timeout=10,
        )
        errors_before = plugin.verify_simulator(sim)
        assert any("candidate_BOGUS" in e or "stale" in e for e in errors_before), (
            f"verify did not catch the stale dir: {errors_before}"
        )

        # Cleanup removes it
        plugin.cleanup_simulator(sim)
        errors_after = plugin.verify_simulator(sim)
        assert errors_after == [], (
            f"cleanup did not remove the stale dir; verify still failing: "
            f"{errors_after}"
        )
    finally:
        sim.stop()
