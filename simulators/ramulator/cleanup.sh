#!/bin/bash
# mini-architect-bench — Ramulator Cleanup
#
# Resets simulator container to pristine state between challenges.
# Called by RamulatorPlugin.cleanup_simulator. Must be idempotent.
#
# After cleanup, the container state must be identical to a freshly
# started container — no leftover sources, build artifacts, submissions,
# or temp files.
set -euo pipefail

RAMULATOR=/work/runtimes/ramulator

# --- Remove agent-submitted custom components ---
# These are .cpp/.h files copied into src/work/ via build_and_run.sh.
rm -f "$RAMULATOR"/src/work/*.cpp "$RAMULATOR"/src/work/*.h

# --- Clear submission directory ---
rm -rf /work/submission
mkdir -p /work/submission

# --- Remove challenge-staged config ---
rm -f /work/challenge_config.yaml

# --- Clear temp files ---
rm -f /tmp/*.json /tmp/*.log /tmp/*.yaml

# --- Verify clean state ---
STALE=$(find "$RAMULATOR/src/work" -maxdepth 1 \( -name '*.cpp' -o -name '*.h' \) 2>/dev/null | head -1)
if [ -n "$STALE" ]; then
    echo "CLEANUP_FAILED: stale source in src/work/: $STALE"
    exit 1
fi

echo "CLEANUP_OK"
