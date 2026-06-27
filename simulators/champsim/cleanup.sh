#!/bin/bash
# mini-architect-bench V6 — ChampSim Cleanup
#
# Resets simulator container to pristine state between challenges.
# Called by SimulatorPool.checkin(). Must be idempotent.
#
# After cleanup, the container state must be identical to a freshly
# started container — no leftover source, objects, configs, or results.
set -euo pipefail

CHAMPSIM=/work/runtimes/champsim

# --- Remove ALL build artifacts ---
rm -rf "$CHAMPSIM/.csconfig" \
       "$CHAMPSIM/_configuration.mk" \
       "$CHAMPSIM/obj" \
       "$CHAMPSIM/.depend" \
       "$CHAMPSIM/bin"

# --- Remove agent-created component source files ---
# These are the custom .h/.cc files injected per challenge.
# We find and remove any candidate* directories in component dirs,
# preserving built-in ChampSim components (bimodal, lru, etc.).
for comp_type in branch btb prefetcher replacement; do
    comp_dir="$CHAMPSIM/$comp_type"
    [ -d "$comp_dir" ] || continue
    for subdir in "$comp_dir"/candidate*; do
        [ -d "$subdir" ] && rm -rf "$subdir"
    done
done

# --- Clear challenge config ---
rm -f "$CHAMPSIM/config_challenge.json"

# --- Clear submission directory ---
rm -rf /work/submission
mkdir -p /work/submission

# --- Clear ALL temp files ---
rm -f /tmp/result_*.json \
      /tmp/make.log \
      /tmp/config.log \
      /tmp/config_challenge.json \
      /tmp/*.h /tmp/*.cc

# --- Verify clean state ---
# Check that no candidate component dirs remain
STALE=$(find "$CHAMPSIM/branch" "$CHAMPSIM/btb" "$CHAMPSIM/prefetcher" "$CHAMPSIM/replacement" \
    -maxdepth 1 -type d -name 'candidate*' 2>/dev/null | head -1)
if [ -n "$STALE" ]; then
    echo "CLEANUP_FAILED: stale component dir: $STALE"
    exit 1
fi

# Check that no build artifacts remain
for artifact in .csconfig _configuration.mk obj bin; do
    if [ -e "$CHAMPSIM/$artifact" ]; then
        echo "CLEANUP_FAILED: stale artifact: $artifact"
        exit 1
    fi
done

echo "CLEANUP_OK"
