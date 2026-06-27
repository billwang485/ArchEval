#!/bin/bash
# mini-architect-bench — DRAMSys Build & Run
#
# Called by orchestrator after copying agent's code to /work/submission/.
#
# Usage:
#   build_and_run.sh <trace_file>
#
# DRAMSys is config-only (no compilation). This script:
#   1. Copies submitted JSON configs into the DRAMSys configs/ tree
#   2. Validates JSON syntax
#   3. Runs DRAMSys with the main config.json
#   4. Prints raw output (parse_output handles regex extraction)
#
# Output markers (parsed by evaluator):
#   BUILD_OK / BUILD_FAILED       — config validation result
#   SIMULATION_OK / SIMULATION_FAILED — simulation result
#   ARCHBENCH_JSON_START / ARCHBENCH_JSON_END — optional JSON metrics block
set -uo pipefail

DRAMSYS=/work/runtimes/dramsys
SUBMISSION=/work/submission
TRACE="${1:-example.stl}"

# --- Find main config.json in submission ---
CONFIG=""
if [ -f "$SUBMISSION/config.json" ]; then
    CONFIG="$SUBMISSION/config.json"
fi

if [ -z "$CONFIG" ]; then
    echo "BUILD_FAILED"
    echo "No config.json found in submission directory"
    exit 1
fi

# --- Validate JSON syntax ---
echo "=== mini-architect-bench: Validating config ==="
if ! python3 -c "import json; json.load(open('$CONFIG'))" 2>&1; then
    echo "BUILD_FAILED"
    echo "Invalid JSON in config.json"
    exit 1
fi

# Validate mc_config.json if present
if [ -f "$SUBMISSION/mc_config.json" ]; then
    if ! python3 -c "import json; json.load(open('$SUBMISSION/mc_config.json'))" 2>&1; then
        echo "BUILD_FAILED"
        echo "Invalid JSON in mc_config.json"
        exit 1
    fi
fi

# Validate memspec.json if present
if [ -f "$SUBMISSION/memspec.json" ]; then
    if ! python3 -c "import json; json.load(open('$SUBMISSION/memspec.json'))" 2>&1; then
        echo "BUILD_FAILED"
        echo "Invalid JSON in memspec.json"
        exit 1
    fi
fi

echo "BUILD_OK"

# --- Copy submission configs into DRAMSys configs tree ---
echo "=== mini-architect-bench: Setting up configs ==="

# Main config.json goes to configs/ root
cp "$CONFIG" "$DRAMSYS/configs/config.json"

# mc_config.json goes to configs/mcconfig/ (DRAMSys convention)
if [ -f "$SUBMISSION/mc_config.json" ]; then
    mkdir -p "$DRAMSYS/configs/mcconfig/"
    cp "$SUBMISSION/mc_config.json" "$DRAMSYS/configs/mcconfig/"
fi

# memspec.json goes to configs/memspec/ (DRAMSys convention)
if [ -f "$SUBMISSION/memspec.json" ]; then
    mkdir -p "$DRAMSYS/configs/memspec/"
    cp "$SUBMISSION/memspec.json" "$DRAMSYS/configs/memspec/"
fi

# --- Run DRAMSys ---
echo "=== mini-architect-bench: Simulating ==="
cd "$DRAMSYS/configs"

# Capture into a temp log so we can both stream and post-process for JSON.
LOG=$(mktemp)
timeout 300 "$DRAMSYS/build/bin/DRAMSys" \
    "config.json" \
    2>&1 | tee "$LOG"
SIM_RC="${PIPESTATUS[0]}"

if [ "$SIM_RC" -eq 0 ]; then
    echo "SIMULATION_OK"
else
    echo "SIMULATION_FAILED (exit code $SIM_RC)"
    rm -f "$LOG"
    exit 1
fi

# --- Optional JSON metrics block ---
# Extract a small JSON summary so downstream parsers can take the fast
# path. parse_output also has a regex fallback for the textual stats.
AVG_GBPS=$(grep -oE "AVG\s+BW[:\s]+[0-9.]+\s*Gb/s" "$LOG" | head -1 | grep -oE "[0-9.]+" | head -1)
MAX_GBPS=$(grep -oE "MAX\s+BW[:\s]+[0-9.]+\s*Gb/s" "$LOG" | head -1 | grep -oE "[0-9.]+" | head -1)
TOTAL_PS=$(grep -oE "Total\s+Time[:\s]+[0-9.]+\s*ps" "$LOG" | head -1 | grep -oE "[0-9.]+" | head -1)

if [ -n "$AVG_GBPS" ] || [ -n "$MAX_GBPS" ] || [ -n "$TOTAL_PS" ]; then
    echo "ARCHBENCH_JSON_START"
    python3 -c "
import json
m = {}
for k, v in [('bandwidth_gbps', '$AVG_GBPS'), ('max_bandwidth_gbps', '$MAX_GBPS')]:
    if v:
        m[k] = round(float(v), 2)
if '$TOTAL_PS':
    m['total_time_ns'] = round(float('$TOTAL_PS') / 1000.0, 2)
print(json.dumps(m))
"
    echo "ARCHBENCH_JSON_END"
fi

rm -f "$LOG"
