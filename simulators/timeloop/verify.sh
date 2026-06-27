#!/bin/bash
# Timeloop verify.sh — consolidates all in-container invariants.
#
# Single source of truth: called by
#   1. Dockerfile (bake-time RUN /work/verify.sh)
#   2. TimeloopPlugin.verify_simulator (runtime, after fresh start + after every cleanup)
#   3. `archbench verify-all` CLI (operator preflight)
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

# --- Timeloop binaries on PATH ---
if command -v timeloop-mapper >/dev/null 2>&1; then
    echo "CHECK_OK: timeloop-mapper on PATH"
else
    echo "CHECK_FAILED: timeloop-mapper binary not found in PATH"
    FAILED=$((FAILED + 1))
fi
if command -v timeloop-model >/dev/null 2>&1; then
    echo "CHECK_OK: timeloop-model on PATH"
else
    echo "CHECK_FAILED: timeloop-model binary not found in PATH"
    FAILED=$((FAILED + 1))
fi

# --- PyYAML importable (used by build_and_run.sh validation) ---
if python3 -c "import yaml" >/dev/null 2>&1; then
    echo "CHECK_OK: pyyaml importable"
else
    echo "CHECK_FAILED: pyyaml not importable"
    FAILED=$((FAILED + 1))
fi

# --- Submission directory ---
check "/work/submission exists" test -d /work/submission

# --- Workloads dir (problem YAMLs + mapper.yaml) ---
check "/work/workloads/timeloop exists" test -d /work/workloads/timeloop

# --- No stale workdir from previous run ---
if [ ! -d /work/workdir ]; then
    echo "CHECK_OK: no stale workdir"
else
    echo "CHECK_FAILED: stale workdir from previous run"
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
