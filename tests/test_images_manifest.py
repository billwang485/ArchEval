"""Golden equality test — the K0 safety net (docs/docker_management.md §10).

images.yaml is the single source of truth for image identity. K0 is PURE
ADDITION + READ-ONLY: it must reproduce TODAY's exact image strings and
change nothing. This test is the proof.

For EVERY simulator plugin and EVERY runtime, we assert

    images.fully_qualified(category, key) == <the live docker_image>

comparing against the live `plugin.docker_image` / `runtime.docker_image`
(not a transcribed literal) so any drift between the manifest and the code
is caught immediately. The notable case is claude_code, whose image is
`localhost/archbench-agent:v6` (NO -claude_code suffix) — handled via the
manifest entry's explicit `name: archbench-agent`.
"""

from __future__ import annotations

import pytest

from archbench.image_management import manifest as images_mod
from archbench.runtimes import _REGISTRY as RUNTIME_REGISTRY, get_runtime
from archbench.simulators import _REGISTRY as SIM_REGISTRY, get_plugin


# ---------------------------------------------------------------------------
# The golden equality: manifest reproduces the live docker_image, exactly.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sim", sorted(SIM_REGISTRY.keys()))
def test_simulator_image_matches_plugin(sim):
    """fully_qualified('simulators', <sim>) == plugin.docker_image (live)."""
    plugin = get_plugin(sim)
    assert images_mod.fully_qualified("simulators", sim) == plugin.docker_image


@pytest.mark.parametrize("rt", sorted(RUNTIME_REGISTRY.keys()))
def test_agent_image_matches_runtime(rt):
    """fully_qualified('agents', <rt>) == runtime.docker_image (live).

    session.py consumes runtime.docker_image (not RuntimeSpec.image), so
    THAT is the value the manifest must reproduce.
    """
    runtime = get_runtime(rt)
    assert images_mod.fully_qualified("agents", rt) == runtime.docker_image


def test_claude_code_resolves_to_archbench_agent_no_suffix():
    """The intentional irregularity: claude_code -> archbench-agent (NO suffix)."""
    assert images_mod.fully_qualified("agents", "claude_code") == "localhost/archbench-agent:v6"
    assert get_runtime("claude_code").docker_image == "localhost/archbench-agent:v6"


def test_gem5_tag_is_v7():
    """gem5 is the one simulator NOT on v6 — guard against a copy-paste v6."""
    assert images_mod.fully_qualified("simulators", "gem5") == "localhost/archbench-gem5:v7"


# ---------------------------------------------------------------------------
# Manifest covers every registered plugin/runtime — no silent omissions.
# ---------------------------------------------------------------------------


def test_manifest_covers_every_simulator():
    assert set(images_mod.keys("simulators")) == set(SIM_REGISTRY.keys())


def test_manifest_covers_every_runtime():
    assert set(images_mod.keys("agents")) == set(RUNTIME_REGISTRY.keys())


def test_challenges_category_empty():
    """challenge_centric (challenges) stays interface-only / empty."""
    assert images_mod.keys("challenges") == []


def test_sim_agents_populated_and_match_l2agent_image():
    """K5 populates sim_agents (combined _l2agent images). Each entry's
    fully_qualified() MUST byte-equal _l2agent_image(plugin.docker_image) — the
    K4 default-identity invariant nothing in the simulator_centric path may
    break. champsim is the one combined image today."""
    from archbench.image_management.plan import _l2agent_image

    sa_keys = images_mod.keys("sim_agents")
    assert "champsim" in sa_keys
    for key in sa_keys:
        fq = images_mod.fully_qualified("sim_agents", key)
        assert fq == _l2agent_image(get_plugin(key).docker_image)


# ---------------------------------------------------------------------------
# Module-shape sanity (pure, no docker, stable enumeration).
# ---------------------------------------------------------------------------


def test_registry_default_is_localhost():
    assert images_mod.registry() == "localhost"


def test_iter_images_enumerates_all_populated_entries():
    enumerated = images_mod.iter_images()
    keys = {(cat, key) for cat, key, _ in enumerated}
    for sim in SIM_REGISTRY:
        assert ("simulators", sim) in keys
    for rt in RUNTIME_REGISTRY:
        assert ("agents", rt) in keys
    # every enumerated tag is the localhost/-prefixed fully-qualified form
    for cat, key, fq in enumerated:
        assert fq == images_mod.fully_qualified(cat, key)
        assert fq.startswith("localhost/")


def test_unknown_category_and_key_raise():
    with pytest.raises(KeyError):
        images_mod.fully_qualified("not_a_category", "x")
    with pytest.raises(KeyError):
        images_mod.fully_qualified("simulators", "not_a_sim")
