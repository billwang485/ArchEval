#!/bin/bash
# gem5 verify.sh — consolidates all in-container invariants.
#
# Single source of truth: called by
#   1. Dockerfile (bake-time RUN /work/verify.sh)
#   2. GEM5Plugin.verify_simulator (runtime, after fresh start + after every cleanup)
#   3. operator preflight check
#
# Output: prints CHECK_OK or CHECK_FAILED:<reason> per check. Exits 0 if
# all checks pass; nonzero otherwise.
set -uo pipefail

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

# --- Workload binary (baked into image) ---
check "hello_static workload binary present" test -x /work/workloads/gem5/hello_static

# --- gem5 binary is on PATH and responsive ---
if gem5 -B 2>&1 | head -1 | grep -q "Build information"; then
    echo "CHECK_OK: gem5 binary responds"
else
    echo "CHECK_FAILED: gem5 binary not working"
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
