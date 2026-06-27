#!/bin/bash
# mini-architect-bench V1 — SCALE-Sim Build & Run
#
# Called by orchestrator after copying agent's config.cfg to /work/submission/.
#
# Usage:
#   build_and_run.sh <topology> [--constraints <max_pe> <max_sram_kb> <max_bw>]
#                               [--timeout <seconds>]
#
# Output markers (parsed by evaluator):
#   BUILD_OK / BUILD_FAILED       — config validation result
#   SIMULATION_OK / SIMULATION_FAILED — simulation result
#   ARCHBENCH_JSON_START/END            — JSON metrics block
set -uo pipefail

SCALESIM=/work/runtimes/scalesim
SUBMISSION=/work/submission
TOPOLOGY=""
MAX_PE=0
MAX_SRAM_KB=0
MAX_BW=0
SIM_TIMEOUT=120

# --- Parse arguments ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --constraints)
            MAX_PE="$2"; MAX_SRAM_KB="$3"; MAX_BW="$4"
            shift 4
            ;;
        --timeout)
            SIM_TIMEOUT="$2"
            shift 2
            ;;
        *)
            TOPOLOGY="$1"
            shift
            ;;
    esac
done

if [ -z "$TOPOLOGY" ]; then
    echo "BUILD_FAILED"
    echo "No topology specified"
    exit 1
fi

TOPOLOGY_PATH="/work/workloads/scalesim/$TOPOLOGY"
if [ ! -f "$TOPOLOGY_PATH" ]; then
    echo "BUILD_FAILED"
    echo "Topology file not found: $TOPOLOGY_PATH"
    exit 1
fi

# --- Find config.cfg in submission ---
CONFIG=""
for f in "$SUBMISSION"/*.cfg; do
    [ -f "$f" ] && CONFIG="$f" && break
done

if [ -z "$CONFIG" ]; then
    echo "BUILD_FAILED"
    echo "No .cfg file found in submission directory"
    exit 1
fi

# --- Validate config structure and extract info ---
echo "=== mini-architect-bench: Validating config ==="
python3 - "$CONFIG" /tmp/archbench_config_info.json <<'PYEOF'
import configparser, sys, json

config_path = sys.argv[1]
info_path = sys.argv[2]

c = configparser.ConfigParser()
c.read(config_path)
sections = c.sections()

if 'architecture_presets' not in sections:
    print('Missing [architecture_presets] section', file=sys.stderr)
    sys.exit(1)
if 'general' not in sections:
    print('Missing [general] section', file=sys.stderr)
    sys.exit(1)

arch = dict(c['architecture_presets'])
print('Config sections:', sections)
print('Architecture:', json.dumps(arch, indent=2))

# Extract key values for constraint checking
info = {}
info['ArrayHeight'] = int(arch.get('arrayheight', 0))
info['ArrayWidth'] = int(arch.get('arraywidth', 0))
info['IfmapSramSzkB'] = int(arch.get('ifmapsramszkb', 0))
info['FilterSramSzkB'] = int(arch.get('filtersramszkb', 0))
info['OfmapSramSzkB'] = int(arch.get('ofmapsramszkb', 0))
info['Bandwidth'] = int(arch.get('bandwidth', 10))
info['Dataflow'] = arch.get('dataflow', 'ws')

with open(info_path, 'w') as f:
    json.dump(info, f)

print('Config validation OK')
PYEOF
VALIDATE_RC=$?

if [ $VALIDATE_RC -ne 0 ]; then
    echo "BUILD_FAILED"
    exit 1
fi

# --- Check hardware constraints (if specified) ---
if [ "$MAX_PE" -gt 0 ] || [ "$MAX_SRAM_KB" -gt 0 ] || [ "$MAX_BW" -gt 0 ]; then
    python3 - /tmp/archbench_config_info.json "$MAX_PE" "$MAX_SRAM_KB" "$MAX_BW" <<'PYEOF'
import json, sys

info_path = sys.argv[1]
max_pe = int(sys.argv[2])
max_sram = int(sys.argv[3])
max_bw = int(sys.argv[4])

with open(info_path) as f:
    info = json.load(f)

violations = []
pe_count = info['ArrayHeight'] * info['ArrayWidth']
total_sram = info['IfmapSramSzkB'] + info['FilterSramSzkB'] + info['OfmapSramSzkB']
bw = info['Bandwidth']

if max_pe > 0 and pe_count > max_pe:
    violations.append(f'PE count {pe_count} exceeds max {max_pe}')
if max_sram > 0 and total_sram > max_sram:
    violations.append(f'Total SRAM {total_sram}KB exceeds max {max_sram}KB')
if max_bw > 0 and bw > max_bw:
    violations.append(f'Bandwidth {bw} exceeds max {max_bw}')

if violations:
    for v in violations:
        print(f'CONSTRAINT VIOLATION: {v}')
    sys.exit(1)

# Print hardware cost for composite scoring
hw_cost = (pe_count * bw) / 1000.0
print(f'Hardware Cost: {hw_cost:.4f}')
print(f'PE Count: {pe_count}, Total SRAM: {total_sram}KB, Bandwidth: {bw}')
PYEOF
    CONSTRAINT_RC=$?

    if [ $CONSTRAINT_RC -ne 0 ]; then
        echo "BUILD_FAILED"
        exit 1
    fi
fi

echo "BUILD_OK"

# --- Run SCALE-Sim and extract metrics ---
echo "=== mini-architect-bench: Simulating ==="
cd "$SCALESIM"
timeout "$SIM_TIMEOUT" python3 - "$CONFIG" "$TOPOLOGY_PATH" <<'PYEOF'
import sys, os, json

config_path = sys.argv[1]
topology_path = sys.argv[2]

# ScaleSim v3 API — manual setup to handle missing layout file gracefully
from scalesim.scale_config import scale_config
from scalesim.topology_utils import topologies
from scalesim.layout_utils import layouts
from scalesim.simulator import simulator

# 1. Parse config
conf = scale_config()
conf.read_conf_file(config_path)
conf.set_topology_file(topology_path)

# 2. Parse topology
topo = topologies()
topo.load_arrays(topofile=topology_path, mnk_inputs=False)

# 3. Layout — ScaleSim v3 requires it but config may not specify one.
#    Generate a default layout file from topology dimensions.
layout_obj = layouts()
layout_path = conf.get_layout_path()
if layout_path and os.path.exists(layout_path):
    layout_obj.load_arrays(layoutfile=layout_path, mnk_inputs=False)
else:
    # Create a default layout (all 1s) matching topology layers
    import tempfile
    num_layers = topo.get_num_layers()
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, dir='/tmp') as f:
        f.write('Layer name, H, W\n')
        for i in range(num_layers):
            name = topo.get_layer_names()[i] if hasattr(topo, 'get_layer_names') else f'layer_{i}'
            dims = topo.get_layer_params(i)  # Returns array of params
            f.write(f'{name},' + ','.join(['1'] * (len(dims))) + ',\n')
        default_layout = f.name
    layout_obj.load_arrays(layoutfile=default_layout, mnk_inputs=False)

# 4. Set up output directory
out_path = '/tmp/scalesim_output'
os.makedirs(out_path, exist_ok=True)

# 5. Run simulation
runner = simulator()
runner.set_params(
    config_obj=conf,
    topo_obj=topo,
    layout_obj=layout_obj,
    top_path=out_path,
    verbosity=True,
    save_trace=False,
)
runner.run()

# 6. Collect per-layer metrics and aggregate
total_cycles = 0
total_compute = 0
total_stall = 0
total_util = 0.0
total_map_eff = 0.0
num_layers = topo.get_num_layers()

for layer_obj in runner.single_layer_sim_object_list:
    items = layer_obj.get_compute_report_items()
    cyc = int(items[0])
    comp = int(items[1])
    stall = int(items[2])
    util = float(items[3])
    meff = float(items[4])

    total_cycles += cyc
    total_compute += comp
    total_stall += stall
    total_util += util
    total_map_eff += meff

# Averages
if num_layers > 0:
    avg_util = round(total_util / num_layers, 2)
    avg_map_eff = round(total_map_eff / num_layers, 2)
else:
    avg_util = 0.0
    avg_map_eff = 0.0

print("SIMULATION_OK")

metrics = {}
if total_cycles > 0:
    metrics['total_cycles'] = total_cycles
if total_compute > 0:
    metrics['compute_cycles'] = total_compute
if total_stall > 0:
    metrics['stall_cycles'] = total_stall
if avg_util > 0:
    metrics['utilization'] = avg_util
if avg_map_eff > 0:
    metrics['mapping_efficiency'] = avg_map_eff

for k, v in metrics.items():
    print(f'{k}: {v}')

print('ARCHBENCH_JSON_START')
print(json.dumps(metrics, indent=2))
print('ARCHBENCH_JSON_END')
PYEOF
SIM_RC=$?

if [ $SIM_RC -ne 0 ]; then
    echo "SIMULATION_FAILED (exit code $SIM_RC)"
    exit 1
fi
