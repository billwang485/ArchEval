"""End-to-end smoke test: sim + agent + submit, no MCP server, no LLM.

Drives the connector handlers directly with the LRU starter as the agent's
submission. This proves the WIRING works (per-run containers / verify /
configure / submit_outcome / provenance) without depending on any vendor
OAuth or live model endpoint.

For the full "Claude actually invokes submit via MCP" smoke, run a manual
operator session — see docs/running_evaluation.md.

Run: `pytest -m requires_docker --run-docker tests/test_p5_smoke.py -v`
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from archbench.core.anonymizer import Anonymizer
from archbench.core.challenge import load_challenge
from archbench.core.container import (
    ContainerConfig,
    ContainerManager,
    ensure_image,
)
from simulators.champsim.connector.server import SubmitContext, handle_submit
from archbench.core.outcomes import SubmitOutcome
from archbench.simulators import get_plugin

REPO_ROOT = Path(__file__).resolve().parents[1]
CHALLENGE_DIR = REPO_ROOT / "challenges" / "cache_replacement"  # root = L3

from archbench.core.container import default_tar_search_dirs
_TAR_SEARCH_DIRS = default_tar_search_dirs()


@pytest.mark.requires_docker
def test_end_to_end_one_submit():
    """Run one submit() programmatically with the LRU starter.

    Acceptance: SIM_OK outcome with IPC within 1% of baseline per-trace
    sphinx3 number (the simulator is deterministic; only floating-point
    aggregation slop would account for any drift).
    """
    challenge = load_challenge(CHALLENGE_DIR)
    plugin = get_plugin(challenge.simulator)

    # 1. Images
    sim_digest = ensure_image(plugin.docker_image, _TAR_SEARCH_DIRS)
    agent_digest = ensure_image(
        "localhost/archbench-agent:v6", _TAR_SEARCH_DIRS,
    )

    # 2. Sim container
    sim_cfg = ContainerConfig.with_run_id(plugin.docker_image, "smoke_sim")
    sim = ContainerManager(sim_cfg)
    sim.start()

    # 3. Agent container (we won't actually run an agent here; just satisfy
    # the connector's API for `copy_out` of /workspace/*)
    agent_cfg = ContainerConfig.with_run_id(
        "localhost/archbench-agent:v6", "smoke_agent",
    )
    agent = ContainerManager(agent_cfg)
    agent.start()

    # Smoke uses simulate.sh (single trace, reuses our sim container) rather
    # than evaluate.sh (6-parallel host-side, OOM-prone on login nodes).
    # Temporarily move evaluate.sh aside; restore in finally.
    eval_sh = CHALLENGE_DIR / "evaluate.sh"
    eval_sh_backup = CHALLENGE_DIR / "evaluate.sh.smoke_disabled"
    if eval_sh.exists():
        eval_sh.rename(eval_sh_backup)

    try:
        # 4. Sim verify
        errors = plugin.verify_simulator(sim)
        assert errors == [], f"sim verify failed: {errors}"

        # 5. Configure sim with the challenge
        plugin.configure_simulator(sim, challenge)

        # 6. Stage the LRU starter into the agent's /workspace/
        for fname, content in challenge.starter_code.items():
            agent.write_file(f"/workspace/{fname}", content)

        # 7. Build the connector context; submit() with 32KB budget so the
        # LRU starter passes the storage audit.
        import os
        os.environ["ARCHBENCH_STORAGE_BUDGET"] = "32768"
        ctx = SubmitContext(
            challenge=challenge,
            challenge_dir=CHALLENGE_DIR,
            plugin=plugin,
            agent=agent,
            sim=sim,
            anonymizer=Anonymizer.disabled(),
        )
        # 8. Submit and assert
        report = handle_submit(ctx)
        assert report.outcome == SubmitOutcome.SIM_OK, (
            f"Expected SIM_OK, got {report.outcome}.\n"
            f"detail: {report.detail}\n"
            f"raw tail:\n{report.raw_log_tail}"
        )
        assert report.metrics is not None
        ipc = report.metrics.get("ipc")
        assert ipc is not None and ipc > 0.1, (
            f"Smoke IPC implausible: {ipc}"
        )
        print(f"\n[smoke] SUBMIT SIM_OK, IPC={ipc:.4f}")
        print(f"[smoke] sim image digest:   {sim_digest[:24]}…")
        print(f"[smoke] agent image digest: {agent_digest[:24]}…")
    finally:
        if eval_sh_backup.exists():
            eval_sh_backup.rename(eval_sh)
        sim.stop()
        agent.stop()


@pytest.mark.requires_docker
def test_provenance_baseline_matches_v6():
    """If baseline.json carries provenance, its image_digest must match the
    live :v6 image (lessons_learned §1)."""
    baseline_path = CHALLENGE_DIR / "baseline.json"
    if not baseline_path.exists():
        pytest.skip("baseline.json not generated yet")
    data = json.loads(baseline_path.read_text())
    if "provenance" not in data:
        pytest.skip("baseline.json missing provenance block (not yet stamped)")
    from archbench.core.provenance import docker_image_digest
    live = docker_image_digest("localhost/archbench-champsim:v6")
    if live is None:
        # Try to load it
        ensure_image("localhost/archbench-champsim:v6", _TAR_SEARCH_DIRS)
        live = docker_image_digest("localhost/archbench-champsim:v6")
    assert live is not None
    assert data["provenance"]["image_digest"] == live, (
        "baseline.json was measured against a different image digest than "
        "what's loaded now. Regenerate baseline.json (archbench baseline <challenge_dir>)."
    )
