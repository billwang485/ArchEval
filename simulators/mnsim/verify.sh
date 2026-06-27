#!/bin/bash
# MNSIM verify.sh — consolidates all in-container invariants.
#
# Single source of truth: called by
#   1. Dockerfile (bake-time RUN /work/verify.sh)
#   2. MNSIMPlugin.verify_simulator (runtime, after fresh start + cleanup)
#   3. operator preflight check
#
# Output: prints CHECK_OK or CHECK_FAILED:<reason> per check. Exits 0 if
# all checks pass; nonzero otherwise. Prints VERIFY_OK on full success.
set -uo pipefail

MNSIM="${MNSIM_PATH:-/work/runtimes/mnsim}"
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

# --- MNSIM checkout + entry present ---
check "MNSIM main.py present" test -f "$MNSIM/main.py"
check "MNSIM package importable" python3 -c "import MNSIM"
check "default SimConfig.ini present" test -f "$MNSIM/SimConfig.ini"
check "hardware-modeling runner present" test -f "$MNSIM/mnsim_hw_run.py"

# --- torch importable (CPU) ---
if python3 -c "import torch; assert torch.tensor([1.0]).sum().item() == 1.0" >/dev/null 2>&1; then
    echo "CHECK_OK: torch importable + functional (CPU)"
else
    echo "CHECK_FAILED: torch not importable / not functional"
    FAILED=$((FAILED + 1))
fi

# --- Submission directory ---
check "/work/submission exists" test -d /work/submission

# NOTE: This verifier intentionally runs only the cheap structural checks
# above (scripts present, MNSIM/torch importable, default config present). It
# does NOT run an end-to-end hardware-modeling smoke. The real MNSIM hw path
# (vgg8 latency/area/power/energy) is exercised by the challenge's
# evaluation/evaluate.sh + the agent's submit, and the baseline run uses it on
# the starter config. An e2e run here (~minutes) violated the plugin's
# verify_simulator-must-be-fast contract and was killed by the in-session exec
# cap, aborting otherwise-valid sessions. Keep this check fast (<5s).

# --- Summary ---
if [ "$FAILED" -eq 0 ]; then
    echo "VERIFY_OK"
    exit 0
else
    echo "VERIFY_FAILED: $FAILED checks failed"
    exit 1
fi
