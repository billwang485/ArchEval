"""Protocol v3 staging: the family starter goes to /workspace/starter/ for
every tier, and additionally to the /workspace/ root for L1 ONLY.

Exercises the real AgentRuntime.stage_workspace via a fake agent that records
where each starter file is written. L1's root copy gives it a ~baseline floor;
L2/L3 author from an empty root (the ablation signal).
"""
from pathlib import Path

import pytest

from archbench.core.challenge import load_challenge
from archbench.core.runtime_base import AgentRuntime


BP = Path("challenges/branch_predictor")
pytestmark = pytest.mark.skipif(
    not BP.exists(),
    reason="public framework release does not bundle challenge corpus",
)


class _FakeAgent:
    """Records write_file destinations; every other container op is a benign
    no-op returning ("", 0) so the surrounding staging steps don't fail."""
    name = "fake"

    def __init__(self):
        self.writes = []

    def write_file(self, fname, content, base_dir=None):
        self.writes.append((base_dir, fname))

    def __getattr__(self, _name):
        return lambda *a, **k: ("", 0)


def _starter_dest_dirs(challenge_path):
    """Destinations of the STARTER files only (validate.py and other advisory
    helpers also land at /workspace root for every tier — exclude them)."""
    c = load_challenge(challenge_path)
    a = _FakeAgent()
    # stage_workspace's default impl does not touch `self`; call it unbound.
    AgentRuntime.stage_workspace(None, a, c)
    starter_names = set(c.starter_code or {})
    return {bd for bd, fn in a.writes if fn in starter_names}


def test_l1_stages_starter_to_workspace_root():
    dirs = _starter_dest_dirs("challenges/branch_predictor/assisted/L1")
    assert "/workspace/starter" in dirs
    assert "/workspace" in dirs  # L1 authoring position → floor ~ baseline


def test_l2_starter_reference_only():
    dirs = _starter_dest_dirs("challenges/branch_predictor/assisted/L2")
    assert "/workspace/starter" in dirs
    assert "/workspace" not in dirs  # L2 authors from an empty root


def test_l3_starter_reference_only():
    dirs = _starter_dest_dirs("challenges/branch_predictor")  # family root = L3
    assert "/workspace/starter" in dirs
    assert "/workspace" not in dirs  # L3 authors from an empty root
