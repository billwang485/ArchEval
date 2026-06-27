#!/bin/bash
# mini-architect-bench V1 — Timeloop Cleanup
#
# Resets simulator container to pristine state between challenges.
# Called by TimeloopPlugin.cleanup_simulator. Must be idempotent.
#
# After cleanup, the container state must be identical to a freshly
# started container — no leftover submissions, workdirs, or results.
set -euo pipefail

# --- Clear submission directory ---
rm -rf /work/submission
mkdir -p /work/submission

# --- Clear workdir (created by build_and_run.sh) ---
rm -rf /work/workdir

# --- Clear any leftover timeloop output in CWD ---
# timeloop-mapper writes output files (*.map.yaml, *.stats.txt) to cwd
rm -f /workspace/timeloop-mapper.* 2>/dev/null || true
rm -f /workspace/*.map.yaml 2>/dev/null || true
rm -f /workspace/*.stats.txt 2>/dev/null || true

# --- Clear temp files ---
rm -f /tmp/*.yaml /tmp/*.log 2>/dev/null || true

# --- Verify clean state ---
if [ -d /work/workdir ]; then
    echo "CLEANUP_FAILED: stale workdir"
    exit 1
fi

if [ "$(ls -A /work/submission 2>/dev/null)" ]; then
    echo "CLEANUP_FAILED: stale files in submission/"
    exit 1
fi

echo "CLEANUP_OK"
