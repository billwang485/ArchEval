"""Pin test — the default-identical PROOF for PHASE K3 (docs/docker_management.md §7-K3).

K3 flips the dependency: every per-plugin / per-runtime `docker_image`
property now CALLS `images.fully_qualified(...)` instead of returning a
hardcoded string. The contract is DEFAULT-IDENTICAL: every resolved
string must stay byte-for-byte what it was before K3.

This test PINS the 13 literals. K0's golden test
(tests/test_images_manifest.py) asserts `fully_qualified == docker_image`,
which after K3 is *tautological* for that direction (docker_image now IS
fully_qualified). This test is the independent anchor: it transcribes the
exact strings, so a manifest edit that drifts a tag (e.g. bumps champsim
to v7) is caught here even though K0's golden test would still pass.

We instantiate each plugin/runtime via the live registries and assert
`.docker_image == <the literal>`.
"""

from __future__ import annotations

import pytest

from archbench.runtimes import get_runtime
from archbench.simulators import get_plugin

# The pinned, current-reality image strings. gem5 is v7; every other sim
# is v6. claude_code is the no-suffix special case (archbench-agent, NOT
# archbench-agent-claude_code).
SIMULATOR_IMAGES = {
    "champsim": "localhost/archbench-champsim:v6",
    "dramsys": "localhost/archbench-dramsys:v6",
    "ramulator": "localhost/archbench-ramulator:v6",
    "scalesim": "localhost/archbench-scalesim:v6",
    "timeloop": "localhost/archbench-timeloop:v6",
    "astrasim": "localhost/archbench-astrasim:v6",
    "mnsim": "localhost/archbench-mnsim:v6",
    "gem5": "localhost/archbench-gem5:v7",
}

AGENT_IMAGES = {
    "mini": "localhost/archbench-agent-mini:v6",
    "gemini": "localhost/archbench-agent-gemini:v6",
    "codex": "localhost/archbench-agent-codex:v6",
    "archharness": "localhost/archbench-agent-archharness:v6",
    "claude_code": "localhost/archbench-agent:v6",
}


@pytest.mark.parametrize("sim,expected", sorted(SIMULATOR_IMAGES.items()))
def test_simulator_docker_image_pinned(sim, expected):
    """plugin.docker_image == its exact pre-K3 literal."""
    assert get_plugin(sim).docker_image == expected


@pytest.mark.parametrize("rt,expected", sorted(AGENT_IMAGES.items()))
def test_agent_docker_image_pinned(rt, expected):
    """runtime.docker_image == its exact pre-K3 literal."""
    assert get_runtime(rt).docker_image == expected


def test_claude_code_has_no_suffix():
    """The intentional irregularity: claude_code -> archbench-agent (NO suffix)."""
    assert get_runtime("claude_code").docker_image == "localhost/archbench-agent:v6"


def test_gem5_tag_is_v7():
    """gem5 is the one simulator NOT on v6 — guard against a copy-paste v6."""
    assert get_plugin("gem5").docker_image == "localhost/archbench-gem5:v7"
