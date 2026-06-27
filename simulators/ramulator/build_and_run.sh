#!/bin/bash
# mini-architect-bench — Ramulator 2.0 Build & Run
#
# Called by orchestrator after copying agent's code to /work/submission/.
#
# Usage:
#   build_and_run.sh <challenge_name> <component_name>
#
# If component_name is non-empty: C++ component mode (compile + run)
# If component_name is empty:     Config-only mode (validate + run)
#
# Output markers (parsed by evaluator):
#   BUILD_OK / BUILD_FAILED       — compilation/validation result
#   SIMULATION_OK / SIMULATION_FAILED — simulation result
#   ARCHBENCH_JSON_START / ARCHBENCH_JSON_END — optional JSON metrics block
set -uo pipefail

RAMULATOR=/work/runtimes/ramulator
SUBMISSION=/work/submission
CHALLENGE_NAME="${1:-}"
COMPONENT_NAME="${2:-}"

# Find config.yaml: prefer challenge-level config, fall back to submission
CONFIG=""
if [ -f "/work/challenge_config.yaml" ]; then
    CONFIG="/work/challenge_config.yaml"
fi
# Submission may override config (for config-only challenges)
for f in "$SUBMISSION"/config.yaml "$SUBMISSION"/ddr4_custom.yaml "$SUBMISSION"/*.yaml; do
    [ -f "$f" ] && CONFIG="$f" && break
done

if [ -z "$CONFIG" ]; then
    echo "BUILD_FAILED"
    echo "No config.yaml found"
    exit 1
fi

if [ -n "$COMPONENT_NAME" ]; then
    # ========================================
    # C++ component mode
    # ========================================
    echo "=== mini-architect-bench: Compiling ==="

    # Copy submission .cpp/.h files to archbench source directory
    ARCHBENCH_DIR="$RAMULATOR/src/work"
    mkdir -p "$ARCHBENCH_DIR"
    # Clean previous files
    rm -f "$ARCHBENCH_DIR"/*.cpp "$ARCHBENCH_DIR"/*.h
    for f in "$SUBMISSION"/*.cpp "$SUBMISSION"/*.h; do
        [ -f "$f" ] && cp "$f" "$ARCHBENCH_DIR/"
    done

    # Rebuild ramulator
    cd "$RAMULATOR/build"
    cmake .. -DCMAKE_BUILD_TYPE=Release 2>&1
    cmake --build . -j$(nproc) 2>&1
    BUILD_RC=$?

    if [ $BUILD_RC -ne 0 ]; then
        echo "BUILD_FAILED"
        exit 1
    fi
    echo "BUILD_OK"

    echo "=== mini-architect-bench: Simulating ==="
    cd "$(dirname "$CONFIG")"
    LOG=$(mktemp)
    timeout 300 "$RAMULATOR/build/ramulator2" -f "$CONFIG" 2>&1 | tee "$LOG"
    SIM_RC="${PIPESTATUS[0]}"

    if [ "$SIM_RC" -eq 0 ]; then
        echo "SIMULATION_OK"
    else
        echo "SIMULATION_FAILED (exit code $SIM_RC)"
        rm -f "$LOG"
        exit 1
    fi
else
    # ========================================
    # Config-only mode
    # ========================================
    echo "=== mini-architect-bench: Validating config ==="

    python3 -c "import yaml; yaml.safe_load(open('$CONFIG'))" 2>&1
    if [ $? -ne 0 ]; then
        echo "BUILD_FAILED"
        exit 1
    fi
    echo "BUILD_OK"

    echo "=== mini-architect-bench: Simulating ==="
    cd "$(dirname "$CONFIG")"
    LOG=$(mktemp)
    timeout 300 "$RAMULATOR/build/ramulator2" -f "$CONFIG" 2>&1 | tee "$LOG"
    SIM_RC="${PIPESTATUS[0]}"

    if [ "$SIM_RC" -eq 0 ]; then
        echo "SIMULATION_OK"
    else
        echo "SIMULATION_FAILED (exit code $SIM_RC)"
        rm -f "$LOG"
        exit 1
    fi
fi

# --- Optional JSON metrics block ---
# Extract a small JSON summary so downstream parsers can take the fast
# path. parse_output also has a regex fallback for the bare stats.
CYCLES=$(grep -oE "memory_system_cycles[:\s]+[0-9]+" "$LOG" | head -1 | grep -oE "[0-9]+" | head -1)
READS=$(grep -oE "total_num_read_requests[:\s]+[0-9]+" "$LOG" | head -1 | grep -oE "[0-9]+" | head -1)
WRITES=$(grep -oE "total_num_write_requests[:\s]+[0-9]+" "$LOG" | head -1 | grep -oE "[0-9]+" | head -1)
LAT=$(grep -oE "avg_read_latency[_0-9]*[:\s]+[0-9.]+" "$LOG" | head -1 | grep -oE "[0-9.]+$" | head -1)

if [ -n "$CYCLES" ] || [ -n "$READS" ] || [ -n "$WRITES" ] || [ -n "$LAT" ]; then
    echo "ARCHBENCH_JSON_START"
    python3 -c "
import json
m = {}
for k, v, cast in [
    ('cycles', '$CYCLES', int),
    ('read_requests', '$READS', int),
    ('write_requests', '$WRITES', int),
    ('latency_avg', '$LAT', float),
]:
    if v:
        m[k] = cast(v)
print(json.dumps(m))
"
    echo "ARCHBENCH_JSON_END"
fi

rm -f "$LOG"
