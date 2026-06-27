"""Stock-LRU bit-equivalence regression test (lessons_learned §2 structural fix).

Past incident: the starter `candidate` was a copy of LRU, but a
1D-array refactor introduced an off-by-one OOB write. The bug was
copy-pasted into 10 classical reference policies (SRRIP/DRRIP/SHiP), so
every "theoretical limit" in the challenge yaml was wrong.

The structural fix: compile and run the in-repo starter side-by-side with
ChampSim's built-in `lru` module on the same trace; cycles + instructions
must be bit-identical. If they differ, the starter has a semantic bug.

Run: `pytest -m requires_docker --run-docker tests/test_stock_lru_equivalence.py`
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FAMILY_DIR = REPO_ROOT / "challenges" / "cache_replacement"
# Phase J layout: simulate.sh under common/simulator/, starter/ under each tier.
SIMULATE_SH = FAMILY_DIR / "common" / "simulator" / "simulate.sh"
STARTER_DIR = FAMILY_DIR / "starter"  # root L3 starter (assisted/ layout)


def _run_simulate(trace: str, config: str, expected_replacement: str) -> dict:
    """Invoke simulate.sh; return parsed ChampSim result JSON.

    Both configs (config.json and lru_config.json) share LLC sets/ways/
    geometry; only `replacement` differs. With config.json we use the
    starter `candidate`; with lru_config.json we use ChampSim's
    built-in `lru`.
    """
    env = {
        **os.environ,
        "ARCHBENCH_TRACE": trace,
        "ARCHBENCH_CONFIG": config,
        "ARCHBENCH_EXPECTED_REPLACEMENT": expected_replacement,
        "ARCHBENCH_STORAGE_BUDGET": "32768",  # starter LRU needs 32KB
        "ARCHBENCH_WARMUP_M": "1",            # 1M warmup
        "ARCHBENCH_SIM_M": "2",               # 2M sim (fast)
    }
    result = subprocess.run(
        [str(SIMULATE_SH), str(STARTER_DIR)],
        capture_output=True, text=True, timeout=600, env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"simulate.sh failed (rc={result.returncode}, "
            f"config={config}). stderr tail:\n{result.stderr[-2000:]}"
        )
    return json.loads(result.stdout)


@pytest.mark.requires_docker
def test_starter_is_bit_equivalent_to_champsim_builtin_lru():
    """Starter LRU and ChampSim built-in lru must produce identical cycles.

    If this fails, the starter has a semantic bug — exactly the failure
    mode the legacy starter had (1D-array OOB on miss). DO NOT relax
    this test to a percentage tolerance; LRU is deterministic and any
    drift is a bug.
    """
    trace = "482.sphinx3-1100B_chunk0.champsimtrace.xz"

    stock = _run_simulate(
        trace=trace,
        config="lru_config.json",
        expected_replacement="lru",
    )
    starter = _run_simulate(
        trace=trace,
        config="config.json",
        expected_replacement="candidate",
    )

    stock_core = stock[0]["roi"]["cores"][0] if isinstance(stock, list) else stock["roi"]["cores"][0]
    starter_core = starter[0]["roi"]["cores"][0] if isinstance(starter, list) else starter["roi"]["cores"][0]

    assert stock_core["instructions"] == starter_core["instructions"], (
        f"Instruction counts differ:\n"
        f"  ChampSim lru:        {stock_core['instructions']}\n"
        f"  Starter (LRU clone): {starter_core['instructions']}\n"
        "Different instruction count means ChampSim is using different code "
        "paths — the starter must be missing or compiled wrong."
    )
    assert stock_core["cycles"] == starter_core["cycles"], (
        f"Cycle counts differ (bit-equivalence VIOLATED):\n"
        f"  ChampSim lru:        {stock_core['cycles']:>12} cycles\n"
        f"  Starter (LRU clone): {starter_core['cycles']:>12} cycles\n"
        f"  delta:               {starter_core['cycles'] - stock_core['cycles']:>+12}\n"
        "Starter has a semantic bug. See docs/lessons_learned.md §2."
    )


@pytest.mark.requires_docker
def test_starter_llc_misses_match_stock_lru():
    """Same chunk0, LLC miss counts must match exactly between starter and stock LRU."""
    trace = "482.sphinx3-1100B_chunk0.champsimtrace.xz"

    stock = _run_simulate(
        trace=trace, config="lru_config.json", expected_replacement="lru",
    )
    starter = _run_simulate(
        trace=trace, config="config.json", expected_replacement="candidate",
    )

    def _llc_stats(j: dict) -> tuple[int, int]:
        roi = (j[0] if isinstance(j, list) else j)["roi"]
        llc = roi["LLC"]
        hits = 0
        misses = 0
        for at in ("LOAD", "RFO", "PREFETCH", "WRITE", "TRANSLATION"):
            d = llc.get(at, {})
            for v in d.get("hit", [0]):
                hits += int(v)
            for v in d.get("miss", [0]):
                misses += int(v)
        return hits, misses

    stock_h, stock_m = _llc_stats(stock)
    starter_h, starter_m = _llc_stats(starter)
    assert stock_m == starter_m, (
        f"LLC miss counts differ:\n"
        f"  ChampSim lru:        {stock_m:>10} misses\n"
        f"  Starter (LRU clone): {starter_m:>10} misses\n"
        f"  delta:               {starter_m - stock_m:>+10}\n"
        "Different miss counts = starter has wrong eviction logic."
    )
    assert stock_h == starter_h, (
        f"LLC hit counts differ ({stock_h} vs {starter_h})"
    )
