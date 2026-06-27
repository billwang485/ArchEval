#!/bin/bash
# DRAMSys verify.sh — consolidates all in-container invariants.
#
# Single source of truth: called by
#   1. Dockerfile (bake-time RUN /work/verify.sh)
#   2. DRAMSysPlugin.verify_simulator (runtime, after fresh start + after every cleanup)
#   3. `archbench verify-all` CLI (operator preflight)
#
# Output: prints CHECK_OK or CHECK_FAILED:<reason> per check. Exits 0 if
# all checks pass; nonzero otherwise.
set -uo pipefail

DRAMSYS=/work/runtimes/dramsys
FAILED=0

check() {
    local name="$1"; shift
    if "$@" >/dev/null 2>&1; then
        echo "CHECK_OK: $name"
    else
        echo "CHECK_FAILED: $name"
        FAILED=$((FAILED + 1))
    fi
}

# --- Core scripts baked into image ---
check "build_and_run.sh exists + executable" test -x /work/build_and_run.sh
check "cleanup.sh exists + executable" test -x /work/cleanup.sh

# --- DRAMSys binary present ---
check "DRAMSys binary exists" test -x "$DRAMSYS/build/bin/DRAMSys"

# --- Submission directory exists ---
check "/work/submission exists" test -d /work/submission

# --- No stale agent-submitted configs (cleanup invariant) ---
for stale in \
    "$DRAMSYS/configs/config.json" \
    "$DRAMSYS/configs/mcconfig/mc_config.json" \
    "$DRAMSYS/configs/memspec/memspec.json"; do
    if [ -f "$stale" ]; then
        echo "CHECK_FAILED: stale config: $stale"
        FAILED=$((FAILED + 1))
    else
        echo "CHECK_OK: no stale config $stale"
    fi
done

# --- Summary ---
if [ "$FAILED" -eq 0 ]; then
    echo "VERIFY_OK"
    exit 0
else
    echo "VERIFY_FAILED: $FAILED checks failed"
    exit 1
fi
