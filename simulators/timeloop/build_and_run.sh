#!/bin/bash
# mini-architect-bench V1 — Timeloop Build & Run
#
# Called by session after copying agent's files to /work/submission/.
#
# Usage:
#   build_and_run.sh <problem_yaml>
#
# Timeloop is config-only (no compilation). This script:
#   1. Sets up workdir
#   2. Finds submission files (arch.yaml and/or mapping.yaml)
#   3. Validates YAML syntax and structure
#   4. Runs timeloop-mapper
#   5. Prints raw output + stats (parse_output handles regex extraction)
#
# Submission modes:
#   - Architecture only: agent submits arch.yaml (mapper auto-searches)
#   - Mapping only: agent submits mapping.yaml, uses challenge's fixed arch
#   - Both: agent submits both arch and mapping
#
# Output markers (parsed by evaluator):
#   BUILD_OK / BUILD_FAILED       — config validation result
#   SIMULATION_OK / SIMULATION_FAILED — simulation result
set -uo pipefail

SUBMISSION=/work/submission
WORKLOADS=/work/workloads/timeloop
COMPONENTS=/work/components
CHALLENGE_DIR=/work/challenge_config
WORKDIR=/work/workdir
PROBLEM="${1:-simple_conv.yaml}"

# --- Setup workdir ---
rm -rf "$WORKDIR"
mkdir -p "$WORKDIR"
cd "$WORKDIR"

# --- Detect submission type ---
# Look for mapping.yaml and arch files separately
MAPPING_FILE=""
ARCH_FILE=""

# Check for explicit mapping.yaml
if [ -f "$SUBMISSION/mapping.yaml" ] || [ -f "$SUBMISSION/mapping.yml" ]; then
    MAPPING_FILE=$(ls "$SUBMISSION"/mapping.yaml "$SUBMISSION"/mapping.yml 2>/dev/null | head -1)
fi

# Check for architecture files (anything with 'arch' in name, or first yaml if no mapping)
for f in "$SUBMISSION"/arch*.yaml "$SUBMISSION"/arch*.yml "$SUBMISSION"/architecture*.yaml; do
    if [ -f "$f" ]; then
        ARCH_FILE="$f"
        break
    fi
done

# If no explicit arch file and no mapping, take the first yaml as arch
if [ -z "$ARCH_FILE" ] && [ -z "$MAPPING_FILE" ]; then
    for f in "$SUBMISSION"/*.yaml "$SUBMISSION"/*.yml; do
        if [ -f "$f" ]; then
            ARCH_FILE="$f"
            break
        fi
    done
fi

# If only mapping (no arch), use challenge's fixed architecture
if [ -z "$ARCH_FILE" ] && [ -n "$MAPPING_FILE" ]; then
    # Look for pre-configured arch in challenge config directory
    for f in "$CHALLENGE_DIR"/arch*.yaml "$CHALLENGE_DIR"/architecture*.yaml; do
        if [ -f "$f" ]; then
            ARCH_FILE="$f"
            echo "Using challenge's fixed architecture: $f"
            break
        fi
    done
fi

if [ -z "$ARCH_FILE" ] && [ -z "$MAPPING_FILE" ]; then
    echo "BUILD_FAILED"
    echo "No .yaml file found in submission directory"
    exit 1
fi

# Copy files to workdir
if [ -n "$ARCH_FILE" ]; then
    cp "$ARCH_FILE" "$WORKDIR/arch.yaml"
fi
if [ -n "$MAPPING_FILE" ]; then
    cp "$MAPPING_FILE" "$WORKDIR/mapping.yaml"
fi

# --- Validate YAML syntax and structure ---
echo "=== mini-architect-bench: Validating config ==="

# Validate architecture (required)
if [ -f "$WORKDIR/arch.yaml" ]; then
    python3 -c "
import yaml, sys
with open('$WORKDIR/arch.yaml') as f:
    data = yaml.safe_load(f)
if not data:
    print('Empty YAML file')
    sys.exit(1)
if 'architecture' not in data:
    print('Missing top-level architecture key')
    sys.exit(1)
arch = data['architecture']
if 'version' not in arch:
    print('Missing architecture.version')
    sys.exit(1)
if 'subtree' not in arch and 'nodes' not in arch:
    print('Missing architecture.subtree or architecture.nodes')
    sys.exit(1)
print('Architecture validation OK')
print('Version:', arch['version'])
" 2>&1
    if [ $? -ne 0 ]; then
        echo "BUILD_FAILED"
        exit 1
    fi
fi

# Validate mapping (if present)
if [ -f "$WORKDIR/mapping.yaml" ]; then
    python3 -c "
import yaml, sys
with open('$WORKDIR/mapping.yaml') as f:
    data = yaml.safe_load(f)
if not data:
    print('Empty mapping YAML')
    sys.exit(1)
if 'mapping' not in data:
    print('Missing top-level mapping key')
    sys.exit(1)
print('Mapping validation OK')
" 2>&1
    if [ $? -ne 0 ]; then
        echo "BUILD_FAILED"
        exit 1
    fi
fi

echo "BUILD_OK"

# --- Check problem and mapper exist ---
if [ ! -f "$WORKLOADS/$PROBLEM" ]; then
    echo "SIMULATION_FAILED"
    echo "Problem file not found: $WORKLOADS/$PROBLEM"
    exit 1
fi

if [ ! -f "$WORKLOADS/mapper.yaml" ]; then
    echo "SIMULATION_FAILED"
    echo "Mapper config not found: $WORKLOADS/mapper.yaml"
    exit 1
fi

# --- Build component args ---
COMP_ARGS=""
if [ -d "$COMPONENTS" ]; then
    for comp in "$COMPONENTS"/*.yaml; do
        [ -f "$comp" ] && COMP_ARGS="$COMP_ARGS $comp"
    done
fi

# --- Build input file list ---
INPUT_FILES=""
[ -f "$WORKDIR/arch.yaml" ] && INPUT_FILES="$WORKDIR/arch.yaml"
[ -f "$WORKDIR/mapping.yaml" ] && INPUT_FILES="$INPUT_FILES $WORKDIR/mapping.yaml"

# --- Run Timeloop Mapper ---
echo "=== mini-architect-bench: Running Timeloop Mapper ==="
timeout 180 timeloop-mapper \
    $INPUT_FILES \
    "$WORKLOADS/$PROBLEM" \
    "$WORKLOADS/mapper.yaml" \
    $COMP_ARGS \
    2>&1
SIM_RC=$?

if [ $SIM_RC -eq 0 ]; then
    # Print detailed stats (Cycles, Energy, Utilization, fJ/Compute breakdown)
    # so that the evaluator can extract metrics.
    STATS_FILE="$WORKDIR/timeloop-mapper.stats.txt"
    if [ -f "$STATS_FILE" ]; then
        echo ""
        echo "=== mini-architect-bench: Detailed Stats ==="
        sed -n '/^Summary Stats/,$ p' "$STATS_FILE"
    fi
    # Wrap detailed stats in ARCHBENCH_JSON markers so parse_output can also
    # locate the metrics section if the regex-only fallback fails.
    if [ -f "$STATS_FILE" ]; then
        echo "ARCHBENCH_JSON_START"
        # Emit a minimal JSON payload mirroring the regex extraction;
        # parse_output reads the regex fields directly, so this block
        # exists primarily as a marker for evaluate.sh aggregation.
        echo "{\"stats_file\": \"$STATS_FILE\"}"
        echo "ARCHBENCH_JSON_END"
    fi
    echo "SIMULATION_OK"
else
    echo "SIMULATION_FAILED (exit code $SIM_RC)"
fi
