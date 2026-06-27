#!/bin/bash
# build_l2_save.sh <sim>  — load base+mini from pool, build the combined
# -l2agent image (no scaffold strip for non-champsim sims: glob=""), save to
# pool. Per-sim base tag is read from the manifest (gem5=v7, others=v6).
set -uo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"
ENGINE="${ARCHBENCH_CONTAINER_CLI:-podman}"
SIM="${1:?usage: build_l2_save.sh <sim>}"
GLOB="${2-}"   # default empty -> no strip (gem5/mnsim); pass candidate* for champsim
BASE="$(python3.11 -c "from archbench.image_management import manifest as m; print(m.fully_qualified('simulators','${SIM}'))" 2>/dev/null)"
TAG="${BASE##*:}"
BASETAR="docker/archbench-${SIM}-${TAG}.tar"
OUT="localhost/archbench-${SIM}-l2agent:${TAG}"
OUTTAR="docker/archbench-${SIM}-l2agent-${TAG}.tar"
echo "===== [$(date +%T)] L2 build+save $SIM on $(hostname) (base=$BASE glob='${GLOB}') ====="
[ -f "$BASETAR" ] || { echo "MISSING base tar $BASETAR"; exit 2; }
$ENGINE load -i "$BASETAR" >/dev/null && echo "  base loaded ($BASETAR)"
$ENGINE load -i docker/archbench-agent-mini-v6.tar >/dev/null && echo "  mini loaded"
ARCHBENCH_L2_SCAFFOLD_GLOB="$GLOB" bash scripts/build_l2agent_image.sh "$SIM"; rc=$?
echo "  build rc=$rc"
[ "$rc" -ne 0 ] && { echo "L2_SAVE_RESULT $SIM FAIL (build)"; exit 1; }
echo "  sim source present in combined image?"
$ENGINE run --rm "$OUT" sh -c "ls /work/runtimes/${SIM} 2>/dev/null | head -3" | sed 's/^/    /' || true
$ENGINE save "$OUT" -o "${OUTTAR}.partial" && mv -f "${OUTTAR}.partial" "$OUTTAR"
ls -lah "$OUTTAR" | awk '{print "  saved "$NF"  "$5}'
echo "L2_SAVE_RESULT $SIM OK ($OUT)"
