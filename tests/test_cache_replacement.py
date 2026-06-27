"""cache_replacement family — yaml + starter + provenance unit tests.

Tests target the L3 tier (verbatim port of the legacy cache_replacement
challenge); common/ shares evaluate.sh + baseline.json + simulator helpers
across all three tiers per the family/tier layout (CLAUDE.md §1.3, §1.17).

Docker-bound tests live in test_stock_lru_equivalence.py (run with
--run-docker). This file stays fast and offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from archbench.core.challenge import load_challenge
from archbench.core.provenance import Provenance
from archbench.simulators import get_plugin

REPO_ROOT = Path(__file__).resolve().parents[1]
FAMILY_DIR = REPO_ROOT / "challenges" / "cache_replacement"
CHALLENGE_DIR = FAMILY_DIR  # the family root IS the L3 challenge (assisted/ layout)

pytestmark = pytest.mark.skipif(
    not CHALLENGE_DIR.exists(),
    reason="public framework release does not bundle challenge corpus",
)


@pytest.fixture(scope="module")
def challenge():
    return load_challenge(CHALLENGE_DIR)


def test_challenge_yaml_loads(challenge):
    assert challenge.id == "cache_replacement_L3"
    assert challenge.simulator == "champsim"
    assert "Metadata <= 256 bytes" in challenge.prompt


def test_all_five_runtimes_declared(challenge):
    """Unified runtimes: block replaces legacy 4-way *_agent fork."""
    assert set(challenge.runtimes.keys()) == {
        "claude_code", "codex", "gemini", "archharness", "mini",
    }


def test_simulator_config_validates(challenge):
    plugin = get_plugin("champsim")
    errs = plugin.validate_challenge(challenge)
    assert errs == [], f"plugin validation: {errs}"


def test_simulator_config_six_traces(challenge):
    traces = challenge.simulator_config.get("traces", [])
    assert len(traces) == 6
    assert all(t.endswith("_chunk0.champsimtrace.xz") for t in traces)
    # Specifically the 6 workloads from the original methodology
    workloads = {t.split("_chunk0")[0] for t in traces}
    assert workloads == {
        "482.sphinx3-1100B", "605.mcf_s-1152B", "605.mcf_s-1536B",
        "471.omnetpp-188B", "470.lbm-1274B", "437.leslie3d-134B",
    }


def test_starter_files_present(challenge):
    assert "candidate.h" in challenge.starter_code
    assert "candidate.cc" in challenge.starter_code


def test_baseline_is_pure_lru(challenge):
    """Guard that the BASELINE is pure LRU. The staged starter/template is now a
    blanked skeleton; the LRU reference that baseline-as-LRU comparison relies on
    lives in the baseline folder (baseline/one_shot, resolved as reference_dir)."""
    ref = challenge.reference_dir
    h = (ref / "candidate.h").read_text()
    cc = (ref / "candidate.cc").read_text()
    assert "LRU" in h or "lru" in h
    assert "lru_position" in cc
    assert "for (long w = 1; w < LLC_WAYS; w++)" in cc, (
        "Baseline is missing the LRU victim-find loop"
    )


def test_baseline_does_not_have_legacy_oob_bug(challenge):
    """Regression guard for the 2026-04 OOB-on-miss bug, checked on the BASELINE
    (baseline/one_shot via reference_dir). The fixed loop bounds `[0, LLC_WAYS)`
    are the structural fix (the buggy version could index `way == LLC_WAYS`)."""
    cc = (challenge.reference_dir / "candidate.cc").read_text()
    assert "for (long w = 0; w < LLC_WAYS; w++)" in cc, (
        "Baseline changed: must keep the canonical `for (long w = 0; w < LLC_WAYS; w++)` loop"
    )


def test_template_is_skeleton_not_baseline(challenge):
    """Leak-fix regression: the L2/L3 staged starter (starter/template) must be a
    blanked skeleton, NOT the LRU baseline. If the baseline logic reappears here,
    the split has regressed and L2/L3 can copy the answer again."""
    cc = challenge.starter_code["candidate.cc"]
    assert "lru_position" not in cc, "template leaks the LRU baseline logic"


# ---------------------------------------------------------------------------
# baseline.json + provenance round-trip (when baseline has been measured)
# ---------------------------------------------------------------------------


def test_baseline_json_provenance_roundtrip():
    """If baseline.json has been measured + stamped, the provenance round-trips."""
    # Flat family layout: baseline.json lives in <family>/evaluation/ (shared across tiers).
    baseline_path = FAMILY_DIR / "evaluation" / "baseline.json"
    if not baseline_path.exists():
        pytest.skip("baseline.json not yet generated (run: archbench baseline <challenge_dir>)")
    data = json.loads(baseline_path.read_text())
    if "provenance" not in data:
        pytest.skip("baseline.json present but provenance not yet stamped")
    prov = Provenance.from_dict(data["provenance"])
    assert len(prov.image_digest.removeprefix("sha256:")) >= 32
    assert len(prov.config_sha256) == 64
    assert len(prov.starter_sha256) == 64
    assert len(prov.trace_sha256) == 64


def test_baseline_json_has_six_per_trace_entries():
    baseline_path = FAMILY_DIR / "evaluation" / "baseline.json"
    if not baseline_path.exists():
        pytest.skip("baseline.json not yet generated")
    data = json.loads(baseline_path.read_text())
    if data.get("average_ipc") is None:
        pytest.skip("baseline.json is a placeholder; not yet measured")
    assert data["num_traces"] == 6
    assert len(data["per_trace"]) == 6


# ---------------------------------------------------------------------------
# config.json invariants
# ---------------------------------------------------------------------------


def test_config_json_llc_matches_simulate_sh():
    """LLC.sets/ways/replacement in config.json must match what simulate.sh
    asserts at runtime (the python preflight inside simulate.sh)."""
    # Flat family layout: config.json lives under <family>/simulator/.
    cfg = json.loads((FAMILY_DIR / "simulator" / "config.json").read_text())
    llc = cfg["LLC"]
    assert llc["sets"] == 2048
    assert llc["ways"] == 16
    assert llc["replacement"] == "candidate"


def test_lru_config_json_uses_builtin_lru():
    """The stock-LRU equivalence test selects lru_config.json; verify it
    actually requests ChampSim's built-in lru module."""
    # Flat family layout: lru_config.json lives under <family>/evaluation/.
    cfg = json.loads((FAMILY_DIR / "evaluation" / "lru_config.json").read_text())
    assert cfg["LLC"]["replacement"] == "lru"
    # Geometry must match config.json (otherwise comparison is meaningless)
    main_cfg = json.loads((FAMILY_DIR / "simulator" / "config.json").read_text())
    for key in ("sets", "ways"):
        assert cfg["LLC"][key] == main_cfg["LLC"][key], (
            f"LLC.{key} differs between config.json and lru_config.json — "
            "stock-LRU equivalence test would be comparing apples to oranges"
        )
