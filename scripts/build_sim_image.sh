#!/bin/bash
# build_sim_image.sh — build a simulator image from its Dockerfile.
#
# Use this when no legacy tar exists (astrasim, gem5) or when you want a
# clean rebuild instead of patching a legacy image.
#
# Usage:
#   ./scripts/build_sim_image.sh <sim_name>
#
# Notes / cost estimates (rough, on a typical sapphire/seas_compute node):
#   astrasim  ~30-60 min  (Boost + Protobuf + ASTRA-Sim from source)
#   gem5      ~5-10 min   (pulls upstream devcontainer; gem5 prebuilt)
#   champsim  ~20-40 min  (clones ChampSim + decodes ~11 GB of traces)
#   dramsys   ~15-25 min  (CMake build of DRAMSys + sqlite)
#   ramulator ~10-15 min  (CMake build of ramulator2)
#   scalesim  ~3-5 min    (pure-Python; pip install -e .)
#   timeloop  ~5-10 min   (pulls timeloopaccelergy/accelergy-timeloop-infrastructure)
#
# For the 5 sims that have legacy v6 tars (champsim/dramsys/ramulator/
# scalesim/timeloop), prefer `./scripts/load_sim_image.sh <sim>` — it's
# faster and bakes in the same set of scripts.

set -euo pipefail

# Container engine: ARCHBENCH_CONTAINER_CLI override, else prefer docker then
# podman (mirrors archbench/core/engine.py::container_engine; docker-first keeps
# behavior identical on a box where docker works).
ENGINE="${ARCHBENCH_CONTAINER_CLI:-$(command -v docker >/dev/null 2>&1 && echo docker || echo podman)}"

SIM="${1:?usage: $0 <sim_name>}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SIM_DIR="${REPO_ROOT}/simulators/${SIM}"
# Tag is PER-SIM (gem5 is v7, others v6); read from the manifest (images.yaml,
# single source of truth) so it never drifts. Fallback v6 if unreadable.
TAG="$(python3.11 -c "from archbench.image_management import manifest as m; print(m.fully_qualified('simulators','${SIM}'))" 2>/dev/null || echo "localhost/archbench-${SIM}:v6")"

[ -d "$SIM_DIR" ] || { echo "ERROR: no such sim dir: $SIM_DIR" >&2; exit 1; }
[ -f "${SIM_DIR}/Dockerfile" ] || { echo "ERROR: no Dockerfile in $SIM_DIR" >&2; exit 1; }

echo "[build_sim_image] Building $TAG from $SIM_DIR/Dockerfile ..."
echo "[build_sim_image] Build context = $REPO_ROOT (so 'COPY simulators/$SIM/...' works)"
cd "$REPO_ROOT"
"$ENGINE" build -t "$TAG" -f "simulators/${SIM}/Dockerfile" .

echo "[build_sim_image] Smoke-testing /work/verify.sh..."
"$ENGINE" run --rm --entrypoint /work/verify.sh "$TAG"

echo "[build_sim_image] OK: $TAG built and verified."
