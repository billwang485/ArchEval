"""Regression tests for two bugs the gibbon_codesign L1 test flight surfaced
(2026-06-01).

1. **mnsim plugin ignored the challenge's declared deliverable.**
   ``MNSIMPlugin.submission_files`` hard-returned ``["SimConfig.ini"]``, so the
   gibbon co-design challenge (deliverable ``design.json``) rejected EVERY
   submit with "Basename 'design.json' not in expected submission files
   ['SimConfig.ini']" → 13/13 build_fail. The fix honours
   ``challenge.output_files`` and only falls back to SimConfig.ini when the
   challenge declares no deliverable of its own (keeps mnsim_pim working).

2. **simulator_metric's evaluate.sh fallback was not family/tier aware.**
   ``_find_evaluate_sh`` only checked ``<challenge_dir>/{evaluation,eval,root}``;
   for a family/tier challenge the script lives under the shared
   ``common/evaluation/`` one level up, so the post-session metric re-evaluation
   errored "no evaluate.sh under tiers/L1". The new ``_resolve_evaluate_sh``
   resolves it via ``resolved_dirs`` first, falling back to the legacy search.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

from archbench.core.challenge import load_challenge

REPO = pathlib.Path(__file__).resolve().parents[1]
GIBBON = REPO / "challenges" / "gibbon_codesign"
MNSIM_PIM = REPO / "challenges" / "mnsim_pim"


# ---------------------------------------------------------------------------
# 1. MNSIMPlugin.submission_files honours the challenge deliverable
# ---------------------------------------------------------------------------


def _mnsim_plugin():
    from simulators.mnsim.plugin import MNSIMPlugin

    return MNSIMPlugin()


class _FakeChallenge:
    def __init__(self, output_files):
        self.output_files = output_files


def test_mnsim_submission_files_honours_output_files():
    """A co-design challenge declaring design.json must get design.json — NOT
    the SimConfig.ini default (the 13/13 build_fail root cause)."""
    assert _mnsim_plugin().submission_files(
        _FakeChallenge(["design.json"])
    ) == ["design.json"]


def test_mnsim_submission_files_falls_back_to_simconfig():
    """Back-compat: a config-tuning challenge that declares no deliverable
    (mnsim_pim) still defaults to MNSIM's hardware-description file."""
    p = _mnsim_plugin()
    assert p.submission_files(_FakeChallenge([])) == ["SimConfig.ini"]
    assert p.submission_files(_FakeChallenge(None)) == ["SimConfig.ini"]


def test_mnsim_real_challenges_resolve_expected_deliverables():
    """End-to-end through load_challenge: gibbon tiers -> design.json,
    mnsim_pim -> SimConfig.ini."""
    if not GIBBON.exists() or not MNSIM_PIM.exists():
        pytest.skip("public framework release does not bundle challenge corpus")
    p = _mnsim_plugin()
    # assisted/ layout: L3 = family root; L1/L2 = assisted/<tier>.
    for path in ("challenges/gibbon_codesign",                 # L3 (root)
                 "challenges/gibbon_codesign/assisted/L1"):     # L1
        c = load_challenge(REPO / path)
        assert p.submission_files(c) == ["design.json"], path
    c = load_challenge(REPO / "challenges" / "mnsim_pim")
    assert p.submission_files(c) == ["SimConfig.ini"]


# ---------------------------------------------------------------------------
# 2. _resolve_evaluate_sh is family/tier aware
# ---------------------------------------------------------------------------


def _load_simulator_metric_module():
    p = REPO / "evaluators" / "simulator_metric" / "evaluator.py"
    spec = importlib.util.spec_from_file_location("simulator_metric_under_test", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_resolve_evaluate_sh_finds_common_for_tier():
    """For a family/tier challenge the resolver must find the shared
    <family>/evaluation/evaluate.sh — the legacy _find_evaluate_sh returns None
    here (it only checks under the tier dir)."""
    if not GIBBON.exists():
        pytest.skip("public framework release does not bundle challenge corpus")
    mod = _load_simulator_metric_module()
    c = load_challenge(REPO / "challenges" / "gibbon_codesign" / "assisted" / "L1")

    resolved = mod._resolve_evaluate_sh(c)
    assert resolved is not None
    assert resolved.name == "evaluate.sh"
    assert resolved.exists()
    # The SHARED evaluate.sh at the family root's evaluation/, NOT a per-tier one.
    assert "/evaluation/evaluate.sh" in str(resolved)
    assert "/assisted/" not in str(resolved)

    # The legacy helper, given only the tier dir, must MISS it (this is the bug
    # the resolver fixes — proves the test would fail on the old code path).
    assert mod._find_evaluate_sh(c.challenge_dir) is None
