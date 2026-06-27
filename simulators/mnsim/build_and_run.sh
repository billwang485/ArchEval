#!/bin/bash
# mini-architect-bench — MNSIM 2.0 Build & Run
#
# Stages the agent's hardware-description config (SimConfig.ini) from
# /work/submission, runs MNSIM 2.0 (via the baked mnsim_hw_run.py wrapper),
# and emits the parsed hardware metrics as a JSON block.
#
# By default ONLY the analytical hardware-modeling path runs
# (latency / area / power / energy) — it is offline, CPU-only, and needs
# no trained weights or dataset. Accuracy simulation (which needs the real
# OneDrive weights + a CIFAR-10 download) is opt-in via --accuracy.
#
# Usage:
#   build_and_run.sh [--nn <name>] [--weights <path>] [--accuracy]
#                    [--timeout <seconds>]
#
# Output markers (parsed by MNSIMPlugin.parse_output):
#   BUILD_OK / BUILD_FAILED            — config staging / validation result
#   SIMULATION_OK / SIMULATION_FAILED  — simulation result
#   ARCHBENCH_JSON_START / ARCHBENCH_JSON_END      — JSON metrics block
set -uo pipefail

MNSIM="${MNSIM_PATH:-/work/runtimes/mnsim}"
SUBMISSION=/work/submission
NN="vgg8"
WEIGHTS=""
DO_ACCURACY=0
SIM_TIMEOUT=600

# --- Parse arguments ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --nn)        NN="$2"; shift 2 ;;
        --weights)   WEIGHTS="$2"; shift 2 ;;
        --accuracy)  DO_ACCURACY=1; shift ;;
        --timeout)   SIM_TIMEOUT="$2"; shift 2 ;;
        *)           echo "Unknown arg: $1" >&2; shift ;;
    esac
done

# --- Find the hardware-description config in the submission ---
# MNSIM's hardware description is an .ini (SimConfig.ini). Accept any
# *.ini in the submission dir; prefer one literally named SimConfig.ini.
CONFIG=""
if [ -f "$SUBMISSION/SimConfig.ini" ]; then
    CONFIG="$SUBMISSION/SimConfig.ini"
else
    for f in "$SUBMISSION"/*.ini; do
        [ -f "$f" ] && CONFIG="$f" && break
    done
fi

if [ -z "$CONFIG" ]; then
    echo "BUILD_FAILED"
    echo "No hardware-description .ini found in $SUBMISSION (expected SimConfig.ini)"
    exit 1
fi

# --- Validate the config is parseable + has the sections MNSIM needs ---
echo "=== mini-architect-bench: Validating hardware config ==="
python3 - "$CONFIG" <<'PYEOF'
import configparser, sys
cfg = sys.argv[1]
c = configparser.ConfigParser()
try:
    c.read(cfg)
except Exception as e:
    print(f"Config not parseable: {e}", file=sys.stderr)
    sys.exit(1)
required = ["Device level", "Crossbar level", "Interface level"]
missing = [s for s in required if s not in c.sections()]
if missing:
    print(f"Config missing required sections: {missing}", file=sys.stderr)
    sys.exit(1)
print("Config sections:", c.sections())
print("Config validation OK")
PYEOF
if [ $? -ne 0 ]; then
    echo "BUILD_FAILED"
    exit 1
fi

echo "BUILD_OK"

# --- Run MNSIM and capture full output ---
echo "=== mini-architect-bench: Simulating (MNSIM 2.0) ==="
# Default = hardware modeling only (latency/area/power/energy): offline,
# CPU-only, NO trained weights needed. --accuracy opts into the accuracy
# simulation (needs a real --weights .pth + a CIFAR-10 download).
MODE="hw"
WEIGHTS_ARGS=()
if [ "$DO_ACCURACY" -eq 1 ]; then
    MODE="accuracy"
    if [ -z "$WEIGHTS" ]; then
        echo "BUILD_FAILED"
        echo "--accuracy requires --weights <path to .pth>"
        exit 1
    fi
    WEIGHTS_ARGS=(-Weights "$WEIGHTS")
fi

# mnsim_hw_run.py mirrors MNSIM main.py's hardware-modeling block but
# constructs the interface with weights_file=None for the hw path. Run from
# the MNSIM dir (some defaults resolve relative to cwd); pass an absolute
# hardware-description path.
cd "$MNSIM" || { echo "SIMULATION_FAILED (cannot cd $MNSIM)"; exit 1; }

RAW=$(timeout "$SIM_TIMEOUT" python3 mnsim_hw_run.py \
        -HWdes "$CONFIG" \
        -NN "$NN" \
        --mode "$MODE" \
        "${WEIGHTS_ARGS[@]}" 2>&1)
SIM_RC=$?
echo "$RAW"

if [ $SIM_RC -ne 0 ]; then
    echo "SIMULATION_FAILED (exit code $SIM_RC)"
    exit 1
fi

# --- Extract summary metrics from MNSIM's text output into JSON ---
# NB: write RAW to a file and pass it as argv — do NOT pipe via stdin while
# also feeding the script through a heredoc (the heredoc owns stdin, so
# sys.stdin.read() would come back empty and the JSON block would be {}).
RAW_FILE="$(mktemp)"
printf '%s' "$RAW" > "$RAW_FILE"
python3 - "$RAW_FILE" <<'PYEOF'
import sys, re, json
with open(sys.argv[1]) as _f:
    raw = _f.read()

def grab(pattern):
    m = re.search(pattern, raw)
    return float(m.group(1)) if m else None

metrics = {}
# Whole-model latency (ns): "Entire latency: <x> ns"
v = grab(r"Entire latency:\s*([0-9.eE+-]+)\s*ns")
if v is not None:
    metrics["latency_ns"] = v
# Total hardware area (um^2): "Hardware area: <x> um^2"
v = grab(r"Hardware area:\s*([0-9.eE+-]+)\s*um\^2")
if v is not None:
    metrics["area_um2"] = v
# Total hardware power (W): "Hardware power: <x> W"
v = grab(r"Hardware power:\s*([0-9.eE+-]+)\s*W")
if v is not None:
    metrics["power_w"] = v
# Total hardware energy (nJ): "Hardware energy: <x> nJ"
v = grab(r"Hardware energy:\s*([0-9.eE+-]+)\s*nJ")
if v is not None:
    metrics["energy_nj"] = v
# Accuracy (only present when accuracy simulation is enabled).
v = grab(r"PIM-based computing accuracy:\s*([0-9.eE+-]+)")
if v is not None:
    metrics["pim_accuracy"] = v
v = grab(r"Original accuracy:\s*([0-9.eE+-]+)")
if v is not None:
    metrics["original_accuracy"] = v

print("ARCHBENCH_JSON_START")
print(json.dumps(metrics, indent=2))
print("ARCHBENCH_JSON_END")
PYEOF
rm -f "$RAW_FILE"

echo "SIMULATION_OK"
