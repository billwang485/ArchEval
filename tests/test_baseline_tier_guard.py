"""Regression tests for the family/tier baseline-reference bug the gibbon L3
test flight surfaced (2026-06-02). See lessons §22.

The baseline is a FAMILY-level constant — the canonical "beat this" reference,
identical across tiers, measured on the full-scaffold reference design (NACIM
for gibbon_codesign). Per-tier ``starter/`` only controls what is STAGED to the
agent (full / none / api_stub). Two coupled bugs:

1. ``archbench baseline`` measured the baseline on whatever tier you pointed it at. On
   an ``api_stub`` tier (a throwaway schema stub, gibbon L3 acc 0.60) that
   produced a degenerate 1e30 baseline AND overwrote the SHARED baseline.json.
   Fix: ``cmd_baseline`` refuses any tier whose ``starter_visibility != full``.

2. The session provenance gate hashed the per-tier staged starter against the
   baseline's ``starter_sha256``. For ``api_stub`` that forced the baseline to
   be measured on the stub (else a starter drift would refuse the run). Fix:
   skip the starter check for ``api_stub`` too (it already skipped ``none``);
   the other three sha fields still run.
"""

from __future__ import annotations

import argparse
import json
import pathlib

import pytest

from archbench.core.challenge import load_challenge

REPO = pathlib.Path(__file__).resolve().parents[1]
GIBBON = REPO / "challenges" / "gibbon_codesign"

pytestmark = pytest.mark.skipif(
    not GIBBON.exists(),
    reason="public framework release does not bundle challenge corpus",
)


# ---------------------------------------------------------------------------
# 1. cmd_baseline refuses non-full tiers (the corruption footgun)
# ---------------------------------------------------------------------------


def _baseline_rc(tier: str) -> int:
    from archbench.cli import cmd_baseline

    args = argparse.Namespace(challenge_dir=str(GIBBON / "tiers" / tier))
    return cmd_baseline(args)


def test_cmd_baseline_refuses_api_stub_tier():
    """L3 (api_stub) must be refused BEFORE any sim work — its stub starter is
    not the baseline reference and would corrupt the shared baseline.json."""
    assert _baseline_rc("L3") == 1


def test_cmd_baseline_refuses_none_tier():
    """L2 (none) has no starter at all — also not a valid baseline source."""
    assert _baseline_rc("L2") == 1


def test_full_tier_is_not_refused_by_the_guard():
    """L1 (full) must pass the starter_visibility guard. We don't run the whole
    baseline here (needs the sim image); we only assert the guard itself does
    not reject `full` — by checking the resolved visibility the guard keys on."""
    c = load_challenge(GIBBON / "assisted" / "L1")
    assert (getattr(c, "starter_visibility", "full") or "full") == "full"


# ---------------------------------------------------------------------------
# 2. provenance gate skips the starter check for api_stub
# ---------------------------------------------------------------------------


def test_provenance_skips_starter_for_api_stub():
    """The baseline is measured on the full L1 tier (NACIM), so its
    starter_sha256 is the NACIM hash — which differs from L3's api_stub staged
    starter. On the OLD code (api_stub not skipped) that mismatch surfaced as a
    starter_sha256 drift and refused the L3 run; now the starter check is
    skipped for api_stub, so no starter drift is reported."""
    from archbench.runtimes.session import _check_baseline_provenance

    c = load_challenge(GIBBON)  # assisted/ layout: family root IS the L3 challenge
    baseline_json = GIBBON / "evaluation" / "baseline.json"
    prov = json.loads(baseline_json.read_text()).get("provenance", {})
    if not prov.get("image_digest"):
        # baseline not stamped in this checkout — nothing to assert against.
        import pytest

        pytest.skip("baseline.json has no stamped provenance to check")

    # Pass the baseline's OWN image digest so image_digest never drifts; we are
    # isolating the starter behavior. config/trace either match or are absent.
    drifts = _check_baseline_provenance(c, prov["image_digest"])
    assert not any("starter_sha256" in d for d in drifts), (
        "api_stub tier must skip the starter_sha256 check; got: %r" % drifts
    )
