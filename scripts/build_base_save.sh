#!/bin/bash
# build_base_save.sh <sim>  — rebuild a sim BASE image from its /work
# Dockerfile, verify the container is neutral (the agent-visible runtimes
# root is /work), and save it to the pool. Heavy: a full sim build. Run on
# a compute node.
set -uo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"
ENGINE="${ARCHBENCH_CONTAINER_CLI:-podman}"
SIM="${1:?usage: build_base_save.sh <sim>}"
BASE="$(python3.11 -c "from archbench.image_management import manifest as m; print(m.fully_qualified('simulators','${SIM}'))" 2>/dev/null)"
TAG="${BASE##*:}"
OUTTAR="docker/archbench-${SIM}-${TAG}.tar"
echo "===== [$(date +%T)] base rebuild $SIM ($BASE) on $(hostname) ====="

bash scripts/build_sim_image.sh "$SIM"; rc=$?
echo "  build rc=$rc"
[ "$rc" -ne 0 ] && { echo "BASE_BUILD $SIM FAIL (build)"; exit 1; }

echo "  neutrality check (the agent's L2 view): /work present?"
$ENGINE run --rm "$BASE" sh -c 'test -d /work && echo "    /work: present" || echo "    /work: MISSING"'
echo "  sim source under /work/runtimes/$SIM:"
$ENGINE run --rm "$BASE" sh -c "ls /work/runtimes/${SIM} 2>/dev/null | head -4" | sed 's/^/    /' || true

$ENGINE save "$BASE" -o "${OUTTAR}.partial" && mv -f "${OUTTAR}.partial" "$OUTTAR"
ls -lah "$OUTTAR" | awk '{print "  saved "$NF"  "$5}'
echo "BASE_BUILD $SIM OK ($BASE  tag=$TAG)"
