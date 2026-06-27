#!/bin/bash
# ASTRA-sim verify.sh — consolidates all in-container invariants.
#
# Single source of truth: called by
#   1. Dockerfile (bake-time RUN /work/verify.sh)
#   2. AstraSimPlugin.verify_simulator (runtime, after fresh start + after every cleanup)
#   3. `archbench verify-all` CLI (operator preflight)
#
# Output: prints CHECK_OK or CHECK_FAILED:<reason> per check. Exits 0 if
# all checks pass; nonzero otherwise.
set -uo pipefail

ASTRASIM=/work/runtimes/astrasim
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

# --- Submission directory ---
check "/work/submission exists" test -d /work/submission

# --- ASTRA-sim binaries ---
check "AstraSim_Analytical_Congestion_Unaware binary present" \
    test -x "$ASTRASIM/build/astra_analytical/build/bin/AstraSim_Analytical_Congestion_Unaware"
check "AstraSim_Analytical_Congestion_Aware binary present" \
    test -x "$ASTRASIM/build/astra_analytical/build/bin/AstraSim_Analytical_Congestion_Aware"

# --- Bundled configs (network / remote_memory / system) ---
check "/work/configs/network is populated" \
    bash -c "ls /work/configs/network/*.yml >/dev/null 2>&1"
check "/work/configs/remote_memory is populated" \
    bash -c "ls /work/configs/remote_memory/*.json >/dev/null 2>&1"

# --- Workload microbenchmarks (at least one ET file) ---
if ls /work/workloads/astrasim/workload/microbenchmarks/all_reduce/*/all_reduce.0.et >/dev/null 2>&1; then
    echo "CHECK_OK: at least one microbenchmark .et file present"
else
    echo "CHECK_FAILED: no microbenchmark .et files under /work/workloads/astrasim/"
    FAILED=$((FAILED + 1))
fi

# --- No stale submission files ---
stale=$(ls /work/submission/ 2>/dev/null | head -1)
if [ -z "$stale" ]; then
    echo "CHECK_OK: no stale submission files"
else
    echo "CHECK_FAILED: stale submission files: $stale"
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
