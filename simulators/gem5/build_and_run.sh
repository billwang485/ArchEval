#!/bin/bash
# mini-architect-bench V1 — gem5 Build & Run
#
# Called by orchestrator after copying agent's config.py to /work/submission/.
#
# Usage:
#   build_and_run.sh <workload_binary>
#
# The agent submits a config.py that defines the gem5 system configuration.
# This script runs gem5 with that config, validates the resulting config.ini,
# and extracts stats.
#
# Output markers (parsed by evaluator):
#   BUILD_OK / BUILD_FAILED       — config validation result
#   SIMULATION_OK / SIMULATION_FAILED — simulation result
#   ARCHBENCH_JSON_START / ARCHBENCH_JSON_END — JSON metrics block
set -uo pipefail

SUBMISSION=/work/submission
WORKLOADS=/work/workloads/gem5
WORKDIR=/tmp/gem5_run
WORKLOAD_BINARY="${1:-hello_static}"

# --- Setup ---
rm -rf "$WORKDIR"
mkdir -p "$WORKDIR"

# --- Find config.py in submission ---
CONFIG=""
for f in "$SUBMISSION"/*.py; do
    [ -f "$f" ] && CONFIG="$f" && break
done

if [ -z "$CONFIG" ]; then
    echo "BUILD_FAILED"
    echo "No .py config file found in submission directory"
    exit 1
fi

# --- Resolve workload binary ---
BINARY_PATH="$WORKLOADS/$WORKLOAD_BINARY"
if [ ! -f "$BINARY_PATH" ]; then
    echo "BUILD_FAILED"
    echo "Workload binary not found: $BINARY_PATH"
    exit 1
fi

# --- Validate config (syntax check) ---
echo "=== mini-architect-bench: Validating config ==="
python3 -c "
import py_compile, sys
try:
    py_compile.compile('$CONFIG', doraise=True)
    print('Syntax OK')
except py_compile.PyCompileError as e:
    print(f'Syntax error: {e}', file=sys.stderr)
    sys.exit(1)
"
VALIDATE_RC=$?

if [ $VALIDATE_RC -ne 0 ]; then
    echo "BUILD_FAILED"
    exit 1
fi

echo "BUILD_OK"

# --- Copy config to workdir and set env vars ---
cp "$CONFIG" "$WORKDIR/config.py"

# Export workload path so the config can reference it
export GEM5_WORKLOAD_BINARY="$BINARY_PATH"
export GEM5_WORKDIR="$WORKDIR"

# --- Run gem5 with resource limits ---
echo "=== mini-architect-bench: Simulating ==="
cd "$WORKDIR"

# Resource limits: 4GB virtual memory, 1024 open files, 300s wall clock.
# --kill-after=10 sends SIGKILL if SIGTERM doesn't stop it.
#
# DO NOT set `ulimit -u` here. The per-user process count is enforced
# cumulatively against the HOST user — not the container — so on a
# shared cluster node the user is already past 256 (other podman
# containers, ssh sessions, login shells). gem5 spawning a few threads
# then hits EAGAIN on every fork: "bash: fork: Resource temporarily
# unavailable". Reproducible with `ulimit -u 256; gem5 --help`.
# Suppressing the failure via `2>/dev/null || true` masks the bash
# warning but does NOT undo the limit. See
# `results/HELLO_WORLD_SIMS_20260527_031531.md` § gem5 — BLOCKED.
ulimit -v 4194304 2>/dev/null || true
ulimit -n 1024 2>/dev/null || true
timeout --kill-after=10 300 gem5 -d "$WORKDIR/m5out" "$WORKDIR/config.py" 2>&1
SIM_RC=$?

# Kill any stray gem5 descendants
pkill -P $$ 2>/dev/null || true

if [ $SIM_RC -ne 0 ]; then
    echo "SIMULATION_FAILED (exit code $SIM_RC)"
    if [ -f "$WORKDIR/m5out/stats.txt" ]; then
        echo "=== Partial stats ==="
        head -30 "$WORKDIR/m5out/stats.txt"
    fi
    exit 1
fi

# --- Check for stats.txt ---
STATS="$WORKDIR/m5out/stats.txt"
if [ ! -f "$STATS" ]; then
    echo "SIMULATION_FAILED"
    echo "No stats.txt generated"
    exit 1
fi

# --- Validate config.ini (check the system was configured correctly) ---
CONFIG_INI="$WORKDIR/m5out/config.ini"
if [ -f "$CONFIG_INI" ]; then
    echo "=== mini-architect-bench: Config Validation ==="
    python3 - "$CONFIG_INI" <<'PYEOF'
import configparser, sys, json

ini = configparser.ConfigParser()
ini.read(sys.argv[1])

findings = {}
warnings = []

# Extract actual system configuration from config.ini
# CPU type
for section in ini.sections():
    if section.startswith('system.cpu') and '.' not in section.replace('system.cpu', '', 1):
        cpu_type = ini.get(section, 'type', fallback='unknown')
        findings['cpu_type'] = cpu_type
        break

# Clock
if ini.has_option('system.clk_domain', 'clock'):
    raw = ini.get('system.clk_domain', 'clock')
    findings['clock'] = raw

# Memory mode
if ini.has_option('system', 'mem_mode'):
    findings['mem_mode'] = ini.get('system', 'mem_mode')

# Memory ranges
if ini.has_option('system', 'mem_ranges'):
    findings['mem_ranges'] = ini.get('system', 'mem_ranges')

# DRAM type
for section in ini.sections():
    if 'dram' in section.lower() and ini.has_option(section, 'type'):
        findings['dram_type'] = ini.get(section, 'type')
        break

# Check for caches
has_cache = any('cache' in s.lower() for s in ini.sections()
                if 'icache' in s.lower() or 'dcache' in s.lower() or 'l2' in s.lower())
findings['has_cache'] = has_cache

# Report
for k, v in sorted(findings.items()):
    print(f'  config.{k} = {v}')

print(json.dumps(findings))
PYEOF
fi

echo "SIMULATION_OK"

# --- Extract key metrics from stats.txt ---
echo "=== mini-architect-bench: Metrics ==="
python3 - "$STATS" <<'PYEOF'
import sys, json

stats_path = sys.argv[1]
metrics = {}

with open(stats_path) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#') or line.startswith('-'):
            continue

        parts = line.split()
        if len(parts) >= 2:
            key = parts[0]
            try:
                val = float(parts[1])
            except ValueError:
                continue

            # Core execution metrics
            if key == 'simSeconds':
                metrics['sim_seconds'] = val
            elif key == 'simTicks':
                metrics['sim_ticks'] = int(val)
            elif key == 'simInsts':
                metrics['sim_insts'] = int(val)
            elif key == 'simOps':
                metrics['sim_ops'] = int(val)
            elif key == 'hostSeconds':
                metrics['host_seconds'] = round(val, 2)
            elif key == 'hostInstRate':
                metrics['host_inst_rate'] = int(val)
            # CPU metrics
            elif key == 'system.cpu.cpi':
                metrics['cpi'] = round(val, 6)
            elif key == 'system.cpu.ipc':
                metrics['ipc'] = round(val, 6)
            elif key == 'system.cpu.numCycles':
                metrics['cycles'] = int(val)
            elif key == 'system.cpu.commitStats0.numLoadInsts':
                metrics['load_insts'] = int(val)
            elif key == 'system.cpu.commitStats0.numStoreInsts':
                metrics['store_insts'] = int(val)
            # Memory controller metrics
            elif key == 'system.mem_ctrl.readReqs':
                metrics['mem_read_reqs'] = int(val)
            elif key == 'system.mem_ctrl.writeReqs':
                metrics['mem_write_reqs'] = int(val)
            elif key == 'system.mem_ctrl.bytesReadSys':
                metrics['mem_bytes_read'] = int(val)
            elif key == 'system.mem_ctrl.bytesWrittenSys':
                metrics['mem_bytes_written'] = int(val)
            elif key == 'system.mem_ctrl.avgRdBWSys':
                metrics['mem_avg_rd_bw'] = round(val, 2)
            # Cache metrics (if caches exist)
            elif key == 'system.cpu.dcache.overallMissRate::total':
                metrics['dcache_miss_rate'] = round(val, 6)
            elif key == 'system.cpu.icache.overallMissRate::total':
                metrics['icache_miss_rate'] = round(val, 6)
            elif key == 'system.cpu.dcache.demandAvgMissLatency::total':
                metrics['dcache_avg_miss_latency'] = round(val, 2)

for k, v in sorted(metrics.items()):
    print(f'{k}: {v}')

print('ARCHBENCH_JSON_START')
print(json.dumps(metrics, indent=2))
print('ARCHBENCH_JSON_END')
PYEOF
