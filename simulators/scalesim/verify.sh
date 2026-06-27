#!/bin/bash
# ScaleSim verify.sh — consolidates all in-container invariants.
#
# Single source of truth: called by
#   1. Dockerfile (bake-time RUN /work/verify.sh)
#   2. ScaleSimPlugin.verify_simulator (runtime, after fresh start + after every cleanup)
#   3. `archbench verify-all` CLI (operator preflight)
#
# Output: prints CHECK_OK or CHECK_FAILED:<reason> per check. Exits 0 if
# all checks pass; nonzero otherwise.
set -uo pipefail

SCALESIM=/work/runtimes/scalesim
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

# --- ScaleSim importable ---
if python3 -c "from scalesim import scale_sim" >/dev/null 2>&1; then
    echo "CHECK_OK: scalesim python module importable"
else
    echo "CHECK_FAILED: scalesim python module not importable"
    FAILED=$((FAILED + 1))
fi

# --- Submission directory ---
check "/work/submission exists" test -d /work/submission

# --- Workloads dir (topology CSVs) ---
check "/work/workloads/scalesim exists" test -d /work/workloads/scalesim

# --- No stale outputs ---
stale=$(ls -d "$SCALESIM/outputs" /workspace/outputs 2>/dev/null | head -1)
if [ -z "$stale" ]; then
    echo "CHECK_OK: no stale outputs/"
else
    echo "CHECK_FAILED: stale outputs dir: $stale"
    FAILED=$((FAILED + 1))
fi

# --- Summary ---
if [ "$FAILED" -eq 0 ]; then
    echo "VERIFY_OK"
    exit 0
else
    echo "VERIFY_FAILED: $FAILED checks failed"
    exit 1
fi
