#!/bin/bash
# ChampSim verify.sh — consolidates all in-container invariants.
#
# Single source of truth: called by
#   1. Dockerfile (bake-time RUN /work/verify.sh)
#   2. ChampSimPlugin.verify_simulator (runtime, after fresh start + after every cleanup)
#   3. operator preflight check
#
# Output: prints CHECK_OK or CHECK_FAILED:<reason> per check. Exits 0 if
# all checks pass; nonzero otherwise. Refuses silent fallbacks — every
# check that fails is named explicitly.
set -uo pipefail

CHAMPSIM=/work/runtimes/champsim
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

# --- ChampSim source tree (binary is NOT checked — it's rebuilt per
# submit with the agent's component, and cleanup.sh removes it between
# runs, so its absence is the steady state we expect at verify time). ---
check "config.sh exists + executable" test -x "$CHAMPSIM/config.sh"
check "ChampSim source tree present" test -d "$CHAMPSIM/src"

# --- Component dirs (custom components go in candidate*/ subdirs) ---
for comp_type in branch btb prefetcher replacement; do
    check "${comp_type}/ exists" test -d "$CHAMPSIM/$comp_type"
done

# --- Submission directory (where the connector copies agent code) ---
check "/work/submission exists" test -d /work/submission

# --- Workload traces (per-challenge staged artifact, NOT base-image health) ---
# Staged by plugin.configure_simulator (xz) and decoded by
# export_workload_files — BOTH run AFTER verify in the session lifecycle,
# so they are legitimately absent on a fresh image. WARN, never hard-fail:
# the real gate is configure_simulator, which raises a clear
# FileNotFoundError if a challenge's declared trace is neither in its
# subtraces/ nor on the host pool. (lessons_learned.md §17 — the baked-
# traces image was lost on a podman storage clear; per-challenge staging
# is the canonical model.)
trace_count=$(ls /work/workload_pools/champsim/*.champsimtrace.xz 2>/dev/null | wc -l)
if [ "$trace_count" -ge 1 ]; then
    echo "CHECK_OK: workload traces present ($trace_count files, baked)"
else
    echo "CHECK_WARN: no baked .champsimtrace.xz (expected — staged per-challenge at configure time)"
fi

decoded_count=$(ls /work/workload_pools/champsim/decoded/*.trace.txt 2>/dev/null | wc -l)
if [ "$decoded_count" -ge 1 ]; then
    echo "CHECK_OK: decoded traces present ($decoded_count files, baked)"
else
    echo "CHECK_WARN: no baked decoded traces (expected — produced by export_workload_files after verify)"
fi

# --- No stale agent component dirs (cleanup invariant) ---
stale=$(find "$CHAMPSIM/branch" "$CHAMPSIM/btb" "$CHAMPSIM/prefetcher" "$CHAMPSIM/replacement" \
    -maxdepth 1 -type d -name 'candidate*' 2>/dev/null | head -5)
if [ -z "$stale" ]; then
    echo "CHECK_OK: no stale candidate* component dirs"
else
    echo "CHECK_FAILED: stale candidate* dirs present:"
    echo "$stale" | sed 's/^/  /'
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
