#!/bin/bash
# mini-architect-bench V1 — SCALE-Sim Cleanup
#
# Resets simulator container to pristine state between challenges.
# Called by ScaleSimPlugin.cleanup_simulator. Must be idempotent.
#
# After cleanup, the container state must be identical to a freshly
# started container — no leftover submission files or simulator outputs.
set -euo pipefail

# --- Clear submission directory ---
rm -rf /work/submission
mkdir -p /work/submission

# --- Clear any ScaleSim output directories ---
# ScaleSim writes outputs to the cwd or a specified directory
rm -rf /work/runtimes/scalesim/outputs 2>/dev/null || true
rm -rf /workspace/outputs 2>/dev/null || true

# --- Clear temp files ---
rm -f /tmp/*.cfg /tmp/*.csv /tmp/result_*.json /tmp/archbench_config_info.json /tmp/scalesim_output 2>/dev/null || true
rm -rf /tmp/scalesim_output 2>/dev/null || true

# --- Verify clean state ---
if [ "$(ls -A /work/submission 2>/dev/null)" ]; then
    echo "CLEANUP_FAILED: stale files in submission/"
    exit 1
fi

echo "CLEANUP_OK"
