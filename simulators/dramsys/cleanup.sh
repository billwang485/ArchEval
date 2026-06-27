#!/bin/bash
# mini-architect-bench — DRAMSys Cleanup
#
# Resets simulator container to pristine state between challenges.
# Called by DRAMSysPlugin.cleanup_simulator. Must be idempotent.
#
# After cleanup, the container state must be identical to a freshly
# started container — no leftover configs, submissions, or results.
set -euo pipefail

DRAMSYS=/work/runtimes/dramsys

# --- Remove agent-submitted config from configs/ root ---
# DRAMSys ships its own configs; we only added config.json via build_and_run.sh
rm -f "$DRAMSYS/configs/config.json"

# --- Remove agent-submitted mc_config.json ---
# Only remove the specific file we copied; preserve DRAMSys built-in mcconfigs
rm -f "$DRAMSYS/configs/mcconfig/mc_config.json"

# --- Remove agent-submitted memspec.json ---
# Only remove the specific file we copied; preserve DRAMSys built-in memspecs
rm -f "$DRAMSYS/configs/memspec/memspec.json"

# --- Clear submission directory ---
rm -rf /work/submission
mkdir -p /work/submission

# --- Clear temp files ---
rm -f /tmp/*.json /tmp/*.log

# --- Verify clean state ---
if [ -f "$DRAMSYS/configs/config.json" ]; then
    echo "CLEANUP_FAILED: stale config.json in configs/"
    exit 1
fi

if [ -f "$DRAMSYS/configs/mcconfig/mc_config.json" ]; then
    echo "CLEANUP_FAILED: stale mc_config.json in configs/mcconfig/"
    exit 1
fi

if [ -f "$DRAMSYS/configs/memspec/memspec.json" ]; then
    echo "CLEANUP_FAILED: stale memspec.json in configs/memspec/"
    exit 1
fi

echo "CLEANUP_OK"
