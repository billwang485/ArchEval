#!/bin/bash
set -euo pipefail

# ================================================================
# mini-architect-bench V1 — ASTRA-sim build_and_run.sh
#
# Usage:
#   build_and_run.sh <workload_prefix> <network_yml> <remote_memory_json> [--congestion-aware]
#
# The agent submits system.json to /work/submission/.
# Workload, network, and remote_memory configs are baked into the image.
#
# Output markers:
#   BUILD_OK / BUILD_FAILED     — config validation result
#   SIMULATION_OK / SIMULATION_FAILED — simulation result
#   ARCHBENCH_JSON_START / ARCHBENCH_JSON_END — JSON metrics block
# ================================================================

WORKLOAD_PREFIX="$1"
NETWORK_YML="$2"
REMOTE_MEMORY_JSON="$3"
CONGESTION_MODE="unaware"

shift 3
while [[ $# -gt 0 ]]; do
    case "$1" in
        --congestion-aware)
            CONGESTION_MODE="aware"
            shift
            ;;
        *)
            echo "WARNING: Unknown arg: $1" >&2
            shift
            ;;
    esac
done

# ================================================================
# Step 1: Validate submission
# ================================================================
SYSTEM_JSON="/work/submission/system.json"

if [[ ! -f "$SYSTEM_JSON" ]]; then
    echo "ERROR: system.json not found in /work/submission/"
    echo "BUILD_FAILED"
    exit 1
fi

# Validate JSON syntax
if ! python3 -c "import json; json.load(open('$SYSTEM_JSON'))" 2>&1; then
    echo "ERROR: system.json is not valid JSON"
    echo "BUILD_FAILED"
    exit 1
fi

# Validate required fields
VALIDATION=$(python3 <<'PYEOF'
import json, sys

with open("/work/submission/system.json") as f:
    cfg = json.load(f)

required = ["scheduling-policy", "all-reduce-implementation", "local-mem-bw"]
missing = [k for k in required if k not in cfg]
if missing:
    print(f"ERROR: Missing required fields: {missing}", file=sys.stderr)
    sys.exit(1)

sp = cfg.get("scheduling-policy", "")
if sp not in ("LIFO", "FIFO"):
    print(f"ERROR: scheduling-policy must be LIFO or FIFO, got: {sp}", file=sys.stderr)
    sys.exit(1)

print("VALIDATION_OK")
PYEOF
)

if [[ "$VALIDATION" != "VALIDATION_OK" ]]; then
    echo "$VALIDATION"
    echo "BUILD_FAILED"
    exit 1
fi

echo "BUILD_OK"

# ================================================================
# Step 2: Resolve paths
# ================================================================
ASTRASIM_DIR="/work/runtimes/astrasim"

if [[ "$CONGESTION_MODE" == "aware" ]]; then
    BINARY="${ASTRASIM_DIR}/build/astra_analytical/build/bin/AstraSim_Analytical_Congestion_Aware"
else
    BINARY="${ASTRASIM_DIR}/build/astra_analytical/build/bin/AstraSim_Analytical_Congestion_Unaware"
fi

if [[ ! -x "$BINARY" ]]; then
    echo "ERROR: ASTRA-sim binary not found: $BINARY"
    echo "SIMULATION_FAILED"
    exit 1
fi

# Resolve workload path (relative to /work/)
if [[ "$WORKLOAD_PREFIX" == /* ]]; then
    WORKLOAD_PATH="$WORKLOAD_PREFIX"
else
    WORKLOAD_PATH="/work/${WORKLOAD_PREFIX}"
fi

# Resolve network config
if [[ "$NETWORK_YML" == /* ]]; then
    NETWORK_PATH="$NETWORK_YML"
else
    NETWORK_PATH="/work/configs/network/${NETWORK_YML}"
fi

# Resolve remote memory config
if [[ "$REMOTE_MEMORY_JSON" == /* ]]; then
    REMOTE_MEMORY_PATH="$REMOTE_MEMORY_JSON"
else
    REMOTE_MEMORY_PATH="/work/configs/remote_memory/${REMOTE_MEMORY_JSON}"
fi

# Verify all config files exist
for cfg_path in "$NETWORK_PATH" "$REMOTE_MEMORY_PATH"; do
    if [[ ! -f "$cfg_path" ]]; then
        echo "ERROR: Config file not found: $cfg_path"
        echo "SIMULATION_FAILED"
        exit 1
    fi
done

# Verify workload files exist (check first NPU's ET file)
if [[ ! -f "${WORKLOAD_PATH}.0.et" ]]; then
    echo "ERROR: Workload trace not found: ${WORKLOAD_PATH}.0.et"
    echo "SIMULATION_FAILED"
    exit 1
fi

# ================================================================
# Step 3: Run ASTRA-sim
# ================================================================
echo "=== mini-architect-bench: Running ASTRA-sim ==="
echo "  Binary: $(basename $BINARY)"
echo "  Workload: $WORKLOAD_PATH"
echo "  System: $SYSTEM_JSON"
echo "  Network: $NETWORK_PATH"
echo "  Remote Memory: $REMOTE_MEMORY_PATH"
echo ""

SIM_OUTPUT=$(timeout 300 "$BINARY" \
    --workload-configuration="$WORKLOAD_PATH" \
    --system-configuration="$SYSTEM_JSON" \
    --network-configuration="$NETWORK_PATH" \
    --remote-memory-configuration="$REMOTE_MEMORY_PATH" \
    2>&1) || {
    RC=$?
    echo "$SIM_OUTPUT"
    if [[ $RC -eq 124 ]]; then
        echo "ERROR: Simulation timed out (300s)"
    else
        echo "ERROR: Simulation failed with exit code $RC"
    fi
    echo "SIMULATION_FAILED"
    exit 1
}

echo "$SIM_OUTPUT"
echo ""
echo "SIMULATION_OK"

# ================================================================
# Step 4: Parse metrics and output JSON
# ================================================================
METRICS_JSON=$(python3 <<PYEOF
import re, json, sys

output = """$SIM_OUTPUT"""

# Parse per-NPU results: "sys[N] finished, X cycles, exposed communication Y cycles."
pattern = r'sys\[(\d+)\]\s+finished,\s+(\d+)\s+cycles.*?exposed\s+communication\s+(\d+)\s+cycles'
matches = re.findall(pattern, output)

if not matches:
    print(json.dumps({"error": "No NPU results found in output"}))
    sys.exit(0)

npu_results = []
for npu_id, total_cycles, exposed_comm in matches:
    npu_results.append({
        "npu_id": int(npu_id),
        "total_cycles": int(total_cycles),
        "exposed_comm_cycles": int(exposed_comm),
    })

# Aggregate metrics
total_cycles_list = [r["total_cycles"] for r in npu_results]
exposed_comm_list = [r["exposed_comm_cycles"] for r in npu_results]

metrics = {
    "total_cycles": max(total_cycles_list),
    "min_cycles": min(total_cycles_list),
    "avg_cycles": sum(total_cycles_list) / len(total_cycles_list),
    "exposed_comm_cycles": max(exposed_comm_list),
    "num_npus": len(npu_results),
    "per_npu": npu_results,
}

print(json.dumps(metrics))
PYEOF
)

echo ""
echo "ARCHBENCH_JSON_START"
echo "$METRICS_JSON"
echo "ARCHBENCH_JSON_END"
