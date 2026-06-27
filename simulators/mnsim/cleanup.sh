#!/bin/bash
# mini-architect-bench — MNSIM Cleanup
#
# Resets the simulator container to pristine state between submits.
# Called by MNSIMPlugin.cleanup_simulator. Must be idempotent.
#
# After cleanup, container state must match a freshly started container:
# no leftover submission files and no stale MNSIM outputs/logs.
set -euo pipefail

MNSIM="${MNSIM_PATH:-/work/runtimes/mnsim}"

# --- Clear submission directory ---
rm -rf /work/submission
mkdir -p /work/submission

# --- Clear MNSIM-generated artifacts (logs, NoC scratch, downloaded data) ---
rm -rf "$MNSIM/logs" 2>/dev/null || true
rm -rf "$MNSIM/runs" 2>/dev/null || true
rm -f  "$MNSIM/inj_rate.txt" 2>/dev/null || true
rm -rf "$MNSIM/MNSIM/Interface/cifar10" \
       "$MNSIM/MNSIM/Interface/cifar100" \
       "$MNSIM/MNSIM/Interface/datasets" 2>/dev/null || true

# --- Clear temp files ---
rm -f /tmp/*.ini 2>/dev/null || true
rm -rf /tmp/tmp.* 2>/dev/null || true

# --- Verify clean state ---
if [ "$(ls -A /work/submission 2>/dev/null)" ]; then
    echo "CLEANUP_FAILED: stale files in submission/"
    exit 1
fi

echo "CLEANUP_OK"
