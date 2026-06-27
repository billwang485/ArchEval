#!/bin/bash
# mini-architect-bench V6 — ChampSim Build & Run
#
# Called by orchestrator after copying agent's code to /work/submission/.
#
# Usage (single component — backward compat):
#   build_and_run.sh <component_dir> <component_name> <warmup> <simulation> \
#       <trace1> [trace2 ...] [--config-tunable]
#
# Usage (multi-component — codesign challenges):
#   build_and_run.sh --multi <warmup> <simulation> \
#       --component <dir> <name> [--component <dir> <name> ...] \
#       <trace1> [trace2 ...] [--config-tunable]
#
# Output markers (parsed by evaluator):
#   BUILD_OK / BUILD_FAILED   — compilation result
#   SIMULATION_OK             — all traces completed
#   ARCHBENCH_TRACES_COUNT=N   — number of traces
#   ARCHBENCH_JSON_START/END   — per-trace JSON metrics
set -euo pipefail

CHAMPSIM=/work/runtimes/champsim
SUBMISSION=/work/submission

# Components stored as "dir:name" pairs
COMPONENTS=()
TRACES=()
WARMUP=""
SIMULATION=""
CONFIG_TUNABLE=false

if [[ "$1" == "--multi" ]]; then
    # --- Multi-component mode ---
    shift
    WARMUP="$1"; shift
    SIMULATION="$1"; shift
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --component) COMPONENTS+=("$2:$3"); shift 3 ;;
            --config-tunable) CONFIG_TUNABLE=true; shift ;;
            *) TRACES+=("$1"); shift ;;
        esac
    done
else
    # --- Single-component mode (backward compat) ---
    COMPONENT_DIR="$1"; shift
    COMPONENT_NAME="$1"; shift
    WARMUP="$1"; shift
    SIMULATION="$1"; shift
    COMPONENTS+=("$COMPONENT_DIR:$COMPONENT_NAME")
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --config-tunable) CONFIG_TUNABLE=true; shift ;;
            *) TRACES+=("$1"); shift ;;
        esac
    done
fi

# --- Copy submitted code into component directories ---
# Collect all component names for file matching
COMP_NAMES=()
for comp in "${COMPONENTS[@]}"; do
    DIR="${comp%%:*}"
    NAME="${comp##*:}"
    COMP_NAMES+=("$NAME")
    mkdir -p "$CHAMPSIM/$DIR/$NAME"
done

if [[ ${#COMPONENTS[@]} -eq 1 ]]; then
    # Single component: copy all .h/.cc files (existing behavior)
    DIR="${COMPONENTS[0]%%:*}"
    NAME="${COMPONENTS[0]##*:}"
    for f in "$SUBMISSION"/*.h "$SUBMISSION"/*.cc; do
        [ -f "$f" ] && cp "$f" "$CHAMPSIM/$DIR/$NAME/"
    done
else
    # Multi-component: route files by component name match
    for f in "$SUBMISSION"/*.h "$SUBMISSION"/*.cc; do
        [ -f "$f" ] || continue
        BASENAME="$(basename "$f")"
        MATCHED=false
        for comp in "${COMPONENTS[@]}"; do
            DIR="${comp%%:*}"
            NAME="${comp##*:}"
            if [[ "$BASENAME" == "${NAME}."* ]]; then
                cp "$f" "$CHAMPSIM/$DIR/$NAME/"
                MATCHED=true
                break
            fi
        done
        # Unmatched files (shared headers) → copy to all component dirs
        if ! $MATCHED; then
            for comp in "${COMPONENTS[@]}"; do
                DIR="${comp%%:*}"
                NAME="${comp##*:}"
                cp "$f" "$CHAMPSIM/$DIR/$NAME/"
            done
        fi
    done
fi

# --- Handle config-tunable challenges (agent-modified config.json) ---
if $CONFIG_TUNABLE && [ -f "$SUBMISSION/config.json" ]; then
    cp "$SUBMISSION/config.json" "$CHAMPSIM/config_challenge.json"
fi

# --- Re-configure + Compile ---
# MUST re-run config.sh so ChampSim picks up the agent's custom component.
# Without this, make uses the baked-in Makefile which only knows the default modules.
echo "=== mini-architect-bench: Configuring ==="
cd "$CHAMPSIM"
rm -rf .csconfig _configuration.mk obj .depend bin
if ! ./config.sh --compile-all-modules config_challenge.json > /tmp/config.log 2>&1; then
    cat /tmp/config.log
    echo "BUILD_FAILED"
    exit 1
fi

echo "=== mini-architect-bench: Compiling ==="
if ! make -j"$(nproc)" bin/champsim 2>&1; then
    echo "BUILD_FAILED"
    exit 1
fi
if [ ! -x bin/champsim ]; then
    echo "BUILD_FAILED"
    exit 1
fi
echo "BUILD_OK"

# --- Simulate each trace ---
TRACE_FAILED=false
for i in "${!TRACES[@]}"; do
    TRACE="/work/workload_pools/champsim/${TRACES[$i]}"
    echo "=== mini-architect-bench: Simulating trace $((i+1))/${#TRACES[@]}: ${TRACES[$i]} ==="
    timeout 300 ./bin/champsim \
        --warmup-instructions "$WARMUP" \
        --simulation-instructions "$SIMULATION" \
        --json "/tmp/result_${i}.json" \
        "$TRACE" 2>&1
    RC=$?
    if [ $RC -eq 124 ]; then
        echo "TRACE_${i}_TIMEOUT"
        TRACE_FAILED=true
    elif [ $RC -ne 0 ]; then
        echo "TRACE_${i}_FAILED (rc=$RC)"
        TRACE_FAILED=true
    else
        echo "TRACE_${i}_DONE"
    fi
done

if $TRACE_FAILED; then
    echo "SIMULATION_FAILED"
    exit 1
fi
echo "SIMULATION_OK"
echo "ARCHBENCH_TRACES_COUNT=${#TRACES[@]}"

# --- Output JSON results ---
for i in "${!TRACES[@]}"; do
    echo "ARCHBENCH_JSON_START"
    cat "/tmp/result_${i}.json"
    echo ""
    echo "ARCHBENCH_JSON_END"
done
