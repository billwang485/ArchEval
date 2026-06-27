#!/bin/bash
# Ramulator verify.sh — consolidates all in-container invariants.
#
# Single source of truth: called by
#   1. Dockerfile (bake-time RUN /work/verify.sh)
#   2. RamulatorPlugin.verify_simulator (runtime, after fresh start + after every cleanup)
#   3. `archbench verify-all` CLI (operator preflight)
#
# Output: prints CHECK_OK or CHECK_FAILED:<reason> per check. Exits 0 if
# all checks pass; nonzero otherwise.
set -uo pipefail

RAMULATOR=/work/runtimes/ramulator
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

# --- Ramulator binary present ---
check "ramulator2 binary exists" test -x "$RAMULATOR/build/ramulator2"

# --- Submission directory exists ---
check "/work/submission exists" test -d /work/submission

# --- Custom archbench/ source subtree present ---
check "src/work/ exists" test -d "$RAMULATOR/src/work"

# --- No stale custom .cpp/.h in src/work (cleanup invariant) ---
stale=$(find "$RAMULATOR/src/work" -maxdepth 1 \( -name '*.cpp' -o -name '*.h' \) 2>/dev/null | head -3)
if [ -z "$stale" ]; then
    echo "CHECK_OK: no stale src/work/*.cpp|.h files"
else
    echo "CHECK_FAILED: stale custom sources in src/work/:"
    echo "$stale" | sed 's/^/  /'
    FAILED=$((FAILED + 1))
fi

# --- Workload traces present (baked into image) ---
trace_count=$(ls /work/workloads/ramulator/ 2>/dev/null | wc -l)
if [ "$trace_count" -ge 1 ]; then
    echo "CHECK_OK: workload traces present ($trace_count files)"
else
    echo "CHECK_FAILED: no workload traces under /work/workloads/ramulator/"
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
