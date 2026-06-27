"""Regression tests for the L1/L2/L3 assistance-tier contract (2026-06).

The three tiers share one Oracle (``common/evaluation/evaluate.sh`` against one
``baseline.json``) and differ only in scaffolding + Oracle-shot count:

- L1: full scaffold, the Oracle is the agent's in-loop tool (many submits).
- L2: NO scaffold; the agent runs in a simulator development image with source
  + dependencies already present, gets ONLY the Oracle + lifecycle MCP tools
  (``tier_tools`` — no browse/read), and ONE scored shot.
- L3: NO scaffold (api_stub); the agent writes its own surrogate, ONE shot.

These tests pin the loader keys, the connector tool-allowlist, the sim-dev image
resolution, and the per-tier budgets. They are unit-level (no container) so they
run in the default pytest set.
"""

from __future__ import annotations

import pathlib

import pytest

from archbench.core.challenge import load_challenge

REPO = pathlib.Path(__file__).resolve().parents[1]
BP = REPO / "challenges" / "branch_predictor"
CHAMPSIM_L2_DIRS = [
    REPO / "challenges" / name / "assisted" / "L2"
    for name in (
        "branch_predictor",
        "btb",
        "cache_replacement",
        "compose_bp_btb",
        "compose_replacement_prefetcher",
        "l1d_prefetcher",
    )
]

pytestmark = pytest.mark.skipif(
    not BP.exists(),
    reason="public framework release does not bundle challenge corpus",
)


# ---------------------------------------------------------------------------
# 1. New Challenge fields default safely (no behavior change for old challenges)
# ---------------------------------------------------------------------------


def test_new_tier_fields_default_safe():
    """A non-L2 challenge keeps the managed-MCP default."""
    c = load_challenge(BP)  # family root = L3
    assert c.session_profile == "managed_mcp"
    assert c.agent_in_sim_image is False
    assert c.tier_tools is None


# ---------------------------------------------------------------------------
# 2. Per-tier budgets: L1 iterates (>1), L2/L3 are one-shot (==1)
# ---------------------------------------------------------------------------


def test_tier_submit_budgets():
    l3 = load_challenge(BP)                              # root = L3
    l1 = load_challenge(BP / "assisted" / "L1")
    l2 = load_challenge(BP / "assisted" / "L2")
    assert l1.eval.max_submissions > 1, "L1 must allow iteration"
    assert l2.eval.max_submissions == 1, "L2 is one Oracle shot"
    assert l3.eval.max_submissions == 1, "L3 is one Oracle shot"


# ---------------------------------------------------------------------------
# 3. L2 loads as sim-dev-env with the Oracle-only tool allowlist
# ---------------------------------------------------------------------------


def test_l2_is_sim_dev_env_with_oracle_only_tools():
    l2 = load_challenge(BP / "assisted" / "L2")
    assert l2.session_profile == "sim_dev_env"
    assert l2.agent_in_sim_image is False
    assert l2.starter_visibility == "none"
    # Oracle + lifecycle only; the introspection tools are dropped (the agent
    # explores the simulator source tree itself).
    assert l2.tier_tools is not None
    assert "submit" in l2.tier_tools
    assert "session_end" in l2.tier_tools
    assert "browse_simulator" not in l2.tier_tools
    assert "read_simulator_file" not in l2.tier_tools


def test_champsim_l2_overlays_match_sim_dev_contract():
    forbidden_prompt_fragments = (
        "self-provision",
        "simulator_context",
        "Docker/build context",
        "inside an image you build",
        "prebuilt ChampSim simulator image",
        "environment you provision",
    )
    for challenge_dir in CHAMPSIM_L2_DIRS:
        l2 = load_challenge(challenge_dir)
        text = (challenge_dir / "challenge.yaml").read_text()
        assert l2.session_profile == "sim_dev_env", challenge_dir
        assert l2.agent_in_sim_image is False, challenge_dir
        assert l2.starter_visibility == "none", challenge_dir
        assert l2.tier_tools == [
            "submit",
            "submit_and_wait",
            "check_submission",
            "session_end",
        ], challenge_dir
        for fragment in forbidden_prompt_fragments:
            assert fragment not in text, (challenge_dir, fragment)


# ---------------------------------------------------------------------------
# 4. The connector actually filters registration to tier_tools
# ---------------------------------------------------------------------------


def test_connector_allowlist_filters_browse_and_read():
    from simulators.champsim.connector import server_subprocess as S

    class _FakeMCP:
        def __init__(self):
            self.names: list[str] = []

        def add_tool(self, fn, name, description):  # noqa: ANN001
            self.names.append(name)

    all_names = {t.name for t in S.TOOLS}

    # allowed=None registers every canonical tool (back-compat).
    m_all = _FakeMCP()
    S.register_sim_tools(m_all, ctx=None, allowed=None)
    assert set(m_all.names) == all_names

    # The L2 allowlist registers only the Oracle + lifecycle tools.
    l2_tools = {"submit", "submit_and_wait", "check_submission", "session_end"}
    m_l2 = _FakeMCP()
    S.register_sim_tools(m_l2, ctx=None, allowed=l2_tools)
    assert set(m_l2.names) == l2_tools
    assert "browse_simulator" not in m_l2.names
    assert "read_simulator_file" not in m_l2.names


# ---------------------------------------------------------------------------
# 5. The sim-dev agent-image name is derived (tag preserved)
# ---------------------------------------------------------------------------


def test_sim_dev_agent_image_name_resolution():
    from archbench.runtimes.session import _l2agent_image

    assert _l2agent_image("localhost/archbench-champsim:v6") == "localhost/archbench-champsim-l2agent:v6"
    assert _l2agent_image("localhost/archbench-gem5:v7") == "localhost/archbench-gem5-l2agent:v7"
    # no tag → suffix only
    assert _l2agent_image("localhost/archbench-champsim") == "localhost/archbench-champsim-l2agent"
