#!/bin/bash
# =============================================================================
# scripts/smoke_test.sh — end-to-end pipeline smoke test
# =============================================================================
# Runs `pytest tests/test_p5_smoke.py --run-docker -v -s` on the login node
# (or any host with podman + access to the v6 image tarball).
#
# What it verifies:
#   1. archbench verify-all passes for champsim + all 5 agent images
#   2. One full submit cycle: sim+agent containers up, plugin.configure_simulator,
#      handle_submit with the LRU starter, assert SIM_OK + IPC sane
#   3. Provenance round-trip: baseline.json's stamped image_digest matches live :v6
#
# Wall-clock: ~3-5 min on a warm node (image cached).
# Fails fast on any structural violation.
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

echo "=== Smoke test ==="
echo "step 1/2: archbench verify-all"
python3.11 -m archbench.cli verify-all --only champsim
echo
echo "step 2/2: pytest test_p5_smoke.py"
python3.11 -m pytest tests/test_p5_smoke.py -v --run-docker -s
