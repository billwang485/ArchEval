"""Unit tests for the PURE image resolver — PHASE K4
(docs/docker_management.md §1, §3, §4, §7-K4).

These pin the DEFAULT-IDENTICAL contract (the hard requirement): with no new
YAML keys, every challenge resolves to today's EXACT images. They also pin the
three agent_image_mode branches, the legacy-alias mapping, the pseudo-path
resolution, and the raise-on-unknown loader divergence (§1.2). Unit-level (no
container) so they run in the default pytest set.
"""

from __future__ import annotations

import pathlib

import pytest

from archbench.core.challenge import Challenge, EvalConfig, load_challenge
from archbench.image_management.plan import ImagePlan, _l2agent_image, resolve_images

REPO = pathlib.Path(__file__).resolve().parents[1]
BP = REPO / "challenges" / "branch_predictor"          # family root = L3 (managed_mcp)
BP_L2 = BP / "assisted" / "L2"                          # sim_dev_env overlay
_NO_BUNDLED_CHALLENGES = "public framework release does not bundle challenge corpus"


# --- stub plugin / runtime (resolve_images only reads .docker_image) --------


class _StubPlugin:
    docker_image = "localhost/archbench-champsim:v6"


class _StubRuntime:
    docker_image = "localhost/archbench-agent-mini:v6"


def _challenge(**overrides) -> Challenge:
    """A minimal Challenge with sensible defaults; override any field."""
    base = dict(
        id="t", name="t", simulator="champsim", prompt="",
        starter_files=[], output_files=[],
        eval=EvalConfig(baseline_file="baseline.json"),
        simulator_config={},
    )
    base.update(overrides)
    return Challenge(**base)


# ---------------------------------------------------------------------------
# 1. The three agent_image_mode branches
# ---------------------------------------------------------------------------


def test_agent_centric_resolves_to_runtime_image():
    plan = resolve_images(
        _challenge(agent_image_mode="agent_centric"),
        _StubPlugin(), _StubRuntime(),
    )
    assert plan.agent_image == _StubRuntime.docker_image
    assert plan.agent_image == "localhost/archbench-agent-mini:v6"
    assert plan.agent_image_mode == "agent_centric"


def test_simulator_centric_resolves_to_l2agent_image():
    plan = resolve_images(
        _challenge(agent_image_mode="simulator_centric"),
        _StubPlugin(), _StubRuntime(),
    )
    assert plan.agent_image == _l2agent_image(_StubPlugin.docker_image)
    assert plan.agent_image == "localhost/archbench-champsim-l2agent:v6"
    assert plan.agent_image_mode == "simulator_centric"


def test_challenge_centric_agent_image_is_none():
    plan = resolve_images(
        _challenge(agent_image_mode="challenge_centric"),
        _StubPlugin(), _StubRuntime(),
    )
    assert plan.agent_image is None
    assert plan.agent_image_mode == "challenge_centric"


# ---------------------------------------------------------------------------
# 2. DEFAULT-IDENTICAL — the hard requirement
# ---------------------------------------------------------------------------


def test_default_identical_plain_challenge():
    """A plain challenge (no new keys) -> agent == runtime.docker_image AND
    eval == plugin.docker_image (today's exact behavior)."""
    plan = resolve_images(_challenge(), _StubPlugin(), _StubRuntime())
    assert plan.agent_image == _StubRuntime.docker_image
    assert plan.simulator_image == _StubPlugin.docker_image
    assert plan.evaluation_sim_image == _StubPlugin.docker_image  # eval == sim
    assert plan.agent_image_mode == "agent_centric"
    assert isinstance(plan, ImagePlan)


def test_eval_defaults_to_simulator_image():
    """evaluation_sim_image absent -> defaults to plugin.docker_image."""
    plan = resolve_images(
        _challenge(evaluation_sim_image=None), _StubPlugin(), _StubRuntime(),
    )
    assert plan.evaluation_sim_image == _StubPlugin.docker_image


# ---------------------------------------------------------------------------
# 3. evaluation_sim_image pseudo-path resolution
# ---------------------------------------------------------------------------


def test_eval_pseudo_path_resolves_via_manifest():
    """'sim/champsim' resolves to the manifest's localhost/archbench-champsim:v6."""
    plan = resolve_images(
        _challenge(evaluation_sim_image="sim/champsim"),
        _StubPlugin(), _StubRuntime(),
    )
    assert plan.evaluation_sim_image == "localhost/archbench-champsim:v6"


def test_eval_literal_tag_used_verbatim():
    """A literal tag with a ':' is NOT mistaken for a pseudo-path."""
    plan = resolve_images(
        _challenge(evaluation_sim_image="localhost/archbench-champsim:v6-patched"),
        _StubPlugin(), _StubRuntime(),
    )
    assert plan.evaluation_sim_image == "localhost/archbench-champsim:v6-patched"


def test_eval_plugin_default_explicit_form():
    """'plugin:default' is the explicit form of the default."""
    plan = resolve_images(
        _challenge(evaluation_sim_image="plugin:default"),
        _StubPlugin(), _StubRuntime(),
    )
    assert plan.evaluation_sim_image == _StubPlugin.docker_image


def test_eval_simulators_canonical_category_also_resolves():
    """The canonical category name 'simulators/...' works too (alias-free)."""
    plan = resolve_images(
        _challenge(evaluation_sim_image="simulators/gem5"),
        _StubPlugin(), _StubRuntime(),
    )
    assert plan.evaluation_sim_image == "localhost/archbench-gem5:v7"


# ---------------------------------------------------------------------------
# 4. Legacy aliases resolve to agent_image_mode (via the real loader)
# ---------------------------------------------------------------------------


def test_legacy_managed_mcp_maps_to_agent_centric():
    if not BP.exists():
        pytest.skip(_NO_BUNDLED_CHALLENGES)
    c = load_challenge(BP)  # family root = L3, managed_mcp
    assert c.session_profile == "managed_mcp"
    assert c.agent_image_mode == "agent_centric"
    plan = resolve_images(c, _StubPlugin(), _StubRuntime())
    assert plan.agent_image == _StubRuntime.docker_image
    assert plan.agent_image_mode == "agent_centric"


def test_legacy_sim_dev_env_maps_to_simulator_centric():
    if not BP_L2.exists():
        pytest.skip(_NO_BUNDLED_CHALLENGES)
    c = load_challenge(BP_L2)  # sim_dev_env overlay
    assert c.session_profile == "sim_dev_env"
    assert c.agent_image_mode == "simulator_centric"
    plan = resolve_images(c, _StubPlugin(), _StubRuntime())
    assert plan.agent_image == _l2agent_image(_StubPlugin.docker_image)
    assert plan.agent_image_mode == "simulator_centric"


def test_legacy_agent_in_sim_image_bool_maps_to_simulator_centric(tmp_path):
    """agent_in_sim_image: true (legacy bool) -> simulator_centric."""
    ch_dir = tmp_path / "legacy_l2"
    ch_dir.mkdir()
    (ch_dir / "challenge.yaml").write_text(
        "id: legacy_l2\n"
        "name: legacy_l2\n"
        "simulator: champsim\n"
        "agent_in_sim_image: true\n"
        "starter_visibility: none\n"   # simulator_centric requires none (§1.17)
        "prompt: do the thing\n"
    )
    c = load_challenge(ch_dir)
    assert c.agent_in_sim_image is True
    assert c.session_profile == "sim_dev_env"
    assert c.agent_image_mode == "simulator_centric"


# ---------------------------------------------------------------------------
# 5. Explicit agent_image_mode wins + back-fills session_profile
# ---------------------------------------------------------------------------


def test_explicit_agent_image_mode_backfills_session_profile(tmp_path):
    ch_dir = tmp_path / "explicit"
    ch_dir.mkdir()
    (ch_dir / "challenge.yaml").write_text(
        "id: explicit\n"
        "name: explicit\n"
        "simulator: champsim\n"
        "agent_image_mode: simulator_centric\n"
        "starter_visibility: none\n"   # simulator_centric requires none (§1.17)
        "prompt: do the thing\n"
    )
    c = load_challenge(ch_dir)
    assert c.agent_image_mode == "simulator_centric"
    # Back-filled so the two stay consistent (the L2 tests read session_profile).
    assert c.session_profile == "sim_dev_env"


def test_explicit_challenge_centric_loads_and_resolves_none(tmp_path):
    """challenge_centric is RECOGNIZED at load (enum valid); resolve yields
    agent_image=None (session.py would short-circuit with NotImplementedError)."""
    ch_dir = tmp_path / "dockerc"
    ch_dir.mkdir()
    (ch_dir / "challenge.yaml").write_text(
        "id: dockerc\n"
        "name: dockerc\n"
        "simulator: champsim\n"
        "agent_image_mode: challenge_centric\n"
        "prompt: do the thing\n"
    )
    c = load_challenge(ch_dir)  # must NOT raise — challenge_centric is valid
    assert c.agent_image_mode == "challenge_centric"
    plan = resolve_images(c, _StubPlugin(), _StubRuntime())
    assert plan.agent_image is None
    assert plan.agent_image_mode == "challenge_centric"


# ---------------------------------------------------------------------------
# 6. Unknown agent_image_mode RAISES at load (the deliberate divergence, §1.2)
# ---------------------------------------------------------------------------


def test_unknown_agent_image_mode_raises_at_load(tmp_path):
    ch_dir = tmp_path / "bogus"
    ch_dir.mkdir()
    (ch_dir / "challenge.yaml").write_text(
        "id: bogus\n"
        "name: bogus\n"
        "simulator: champsim\n"
        "agent_image_mode: simulator_centricc\n"  # typo — must NOT degrade
        "prompt: do the thing\n"
    )
    with pytest.raises(ValueError, match="agent_image_mode"):
        load_challenge(ch_dir)
