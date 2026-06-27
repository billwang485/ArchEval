"""ChampSimPlugin — unit tests that don't need docker.

The stock-LRU integration test lives in tests/test_stock_lru_equivalence.py
and is `pytest -m integration` only (skipped by default).
"""

import json
import textwrap
from pathlib import Path

import pytest

from archbench.core.challenge import Challenge, EvalConfig
from archbench.simulators import get_plugin
from simulators.champsim import ChampSimPlugin


@pytest.fixture
def plugin():
    return ChampSimPlugin()


# ---------------------------------------------------------------------------
# Identity / registry
# ---------------------------------------------------------------------------


def test_plugin_identity(plugin):
    assert plugin.name == "champsim"
    assert plugin.docker_image == "localhost/archbench-champsim:v6"
    # Default tar name resolves to the conventional NFS path slug
    assert plugin.docker_tar_name == "archbench-champsim-v6.tar"


def test_registry_returns_instance():
    p = get_plugin("champsim")
    assert isinstance(p, ChampSimPlugin)


# ---------------------------------------------------------------------------
# validate_challenge — catches yaml errors before any container starts
# ---------------------------------------------------------------------------


def _ch(simulator_config: dict, **kwargs) -> Challenge:
    defaults = dict(
        id="t", name="t", simulator="champsim", prompt="",
        starter_files=["main.cc"], output_files=["main.cc"],
        eval=EvalConfig(),
        simulator_config=simulator_config,
        challenge_dir=Path("/tmp/nonexistent"),
    )
    defaults.update(kwargs)
    return Challenge(**defaults)


def test_validate_missing_warmup(plugin):
    errs = plugin.validate_challenge(_ch({
        "component_dir": "replacement",
        "component_name": "candidate",
        "simulation": 500_000_000,
        "traces": ["x.champsimtrace.xz"],
    }))
    assert any("warmup" in e for e in errs)


def test_validate_missing_trace(plugin):
    errs = plugin.validate_challenge(_ch({
        "component_dir": "replacement",
        "component_name": "candidate",
        "warmup": 1000, "simulation": 10000,
    }))
    assert any("trace" in e for e in errs)


def test_validate_multi_component_ok(plugin):
    errs = plugin.validate_challenge(_ch({
        "components": [
            {"dir": "branch", "name": "archbench_a"},
            {"dir": "btb", "name": "archbench_b"},
        ],
        "warmup": 1000, "simulation": 10000,
        "traces": ["x.champsimtrace.xz"],
    }))
    assert errs == []


def test_validate_multi_component_missing_name(plugin):
    errs = plugin.validate_challenge(_ch({
        "components": [{"dir": "branch"}],  # missing 'name'
        "warmup": 1000, "simulation": 10000,
        "traces": ["x.champsimtrace.xz"],
    }))
    assert any("'name'" in e for e in errs)


# ---------------------------------------------------------------------------
# default_source_blocklist — agent can't read its own solution
# ---------------------------------------------------------------------------


def test_blocklist_includes_solution_path(plugin):
    ch = _ch({
        "component_dir": "replacement",
        "component_name": "candidate",
        "warmup": 1, "simulation": 1,
        "traces": ["x.xz"],
    })
    blocked = plugin.default_source_blocklist(ch)
    assert any("/replacement/candidate" in p for p in blocked)
    # Default super class blocks /work/challenges/*/solution/*
    assert any("/solution" in p for p in blocked)


# ---------------------------------------------------------------------------
# submission_files
# ---------------------------------------------------------------------------


def test_submission_files_includes_config_when_tunable(plugin):
    ch = _ch(
        {"config_tunable": True,
         "component_dir": "replacement",
         "component_name": "candidate",
         "warmup": 1, "simulation": 1, "traces": ["x.xz"]},
        output_files=["candidate.h", "candidate.cc"],
    )
    files = plugin.submission_files(ch)
    assert "config.json" in files


def test_submission_files_omits_config_when_not_tunable(plugin):
    ch = _ch(
        {"component_dir": "replacement", "component_name": "candidate",
         "warmup": 1, "simulation": 1, "traces": ["x.xz"]},
        output_files=["candidate.h", "candidate.cc"],
    )
    assert "config.json" not in plugin.submission_files(ch)


# ---------------------------------------------------------------------------
# parse_output — the JSON marker extraction is the most failure-prone bit
# ---------------------------------------------------------------------------


def _make_single_trace_output(ipc: float, mpki: float = 5.0) -> str:
    inst = 1_000_000
    cyc = int(inst / ipc)
    mis = int(mpki * inst / 1000)
    payload = {
        "roi": {
            "cores": [{
                "instructions": inst,
                "cycles": cyc,
                "mispredict": {"BRANCH_CONDITIONAL": mis},
            }],
            "LLC": {
                "LOAD": {"hit": [100], "miss": [10]},
                "RFO": {"hit": [50], "miss": [5]},
            },
        }
    }
    return (
        "BUILD_OK\n"
        "=== Simulating ===\n"
        "SIMULATION_OK\n"
        "ARCHBENCH_TRACES_COUNT=1\n"
        "ARCHBENCH_JSON_START\n"
        + json.dumps(payload) + "\n"
        "ARCHBENCH_JSON_END\n"
    )


def test_parse_output_single_trace(plugin):
    raw = _make_single_trace_output(ipc=0.5113)
    metrics = plugin.parse_output(raw)
    assert metrics is not None
    assert metrics["ipc"] == 0.5113
    assert metrics["instructions"] == 1_000_000
    assert "LLC_hit_rate" in metrics
    assert "hit_rate" in metrics
    assert metrics["speedup"] == metrics["ipc"]


def test_parse_output_no_simulation_ok_marker(plugin):
    """If SIMULATION_OK is missing, refuse — connector emits BUILD_FAIL/TIMEOUT."""
    raw = "BUILD_FAILED\nMissing header\n"
    assert plugin.parse_output(raw) is None


def test_parse_output_truncation_in_json_block_is_unsafe(plugin):
    """Past bug: half-truncated JSON parsed as if complete."""
    raw = (
        "SIMULATION_OK\n"
        "ARCHBENCH_JSON_START\n"
        '{"roi": {"cores": [{"instructions": 1000'
        "\n... [truncated 500 chars] ...\n"
        "ARCHBENCH_JSON_END\n"
    )
    assert plugin.parse_output(raw) is None


def test_parse_output_multi_trace_averages_and_keeps_per_trace(plugin):
    inst = 1_000_000
    blocks = []
    for ipc in (0.40, 0.60, 0.80):
        cyc = int(inst / ipc)
        blocks.append({
            "roi": {
                "cores": [{
                    "instructions": inst, "cycles": cyc,
                    "mispredict": {},
                }],
            },
        })
    raw = "SIMULATION_OK\nARCHBENCH_TRACES_COUNT=3\n"
    for b in blocks:
        raw += f"ARCHBENCH_JSON_START\n{json.dumps(b)}\nARCHBENCH_JSON_END\n"
    metrics = plugin.parse_output(raw)
    assert metrics is not None
    assert "_per_trace" in metrics
    assert len(metrics["_per_trace"]) == 3
    # Average of (0.40, 0.60, 0.80) = 0.60
    assert metrics["ipc"] == 0.60
    # Per-trace numbers preserved
    per_ipcs = sorted(m["ipc"] for m in metrics["_per_trace"])
    assert per_ipcs == [0.40, 0.60, 0.80]


def test_parse_output_no_json_block(plugin):
    raw = "SIMULATION_OK\nbut no markers"
    assert plugin.parse_output(raw) is None


# ---------------------------------------------------------------------------
# anonymizer_mapping helper
# ---------------------------------------------------------------------------


def test_anonymizer_mapping_loads_three_pairs_per_entry(tmp_path):
    """Each trace_mapping.json entry → 3 forward pairs (xz, txt, base)."""
    from simulators.champsim.anonymization.build_anonymizer import (
        load_champsim_anonymizer,
    )
    mapping = tmp_path / "trace_mapping.json"
    mapping.write_text(json.dumps({
        "482.sphinx3-1100B.champsimtrace.xz": "trace_abc.champsimtrace.xz",
    }))
    anon = load_champsim_anonymizer(mapping)
    assert anon.enabled
    # All three shapes scrub correctly
    assert anon.scrub_outbound("Use 482.sphinx3-1100B.champsimtrace.xz") == \
        "Use trace_abc.champsimtrace.xz"
    assert anon.scrub_outbound("Decoded 482.sphinx3-1100B.trace.txt") == \
        "Decoded trace_abc.trace.txt"
    assert anon.scrub_outbound("Base 482.sphinx3-1100B is done") == \
        "Base trace_abc is done"


def test_anonymizer_mapping_missing_file_raises(tmp_path):
    from simulators.champsim.anonymization.build_anonymizer import (
        load_champsim_anonymizer,
    )
    with pytest.raises(FileNotFoundError):
        load_champsim_anonymizer(tmp_path / "does_not_exist.json")
