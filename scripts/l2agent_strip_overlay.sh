#!/bin/bash
# l2agent_strip_overlay.sh <sim> — neutralization overlay for an EXISTING l2agent
# image: strip the harness build wrappers from the agent-facing /work (they are
# unused base leftovers carrying ARCHBENCH_* marker/comment strings; the L2 agent
# runs the baked /opt/mini loop and the eval runs in the separate pristine image).
# Whiteout layer => the running container won't show them, so the agent can't cat.
# Disk-safe (podman storage on big local /tmp). Run with: srun --tmp=50G ...
set -uo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"
ENGINE=podman
SIM="${1:?usage: l2agent_strip_overlay.sh <sim>}"
case "$SIM" in gem5) V=v7;; *) V=v6;; esac
TAG="localhost/archbench-${SIM}-l2agent:${V}"
TAR="docker/archbench-${SIM}-l2agent-${V}.tar"
[ -f "$TAR" ] || { echo "STRIP_OVERLAY $SIM: tar MISSING ($TAR)"; exit 1; }
export XDG_DATA_HOME="${TMPDIR:-/tmp}/pod_strip_$$"; mkdir -p "$XDG_DATA_HOME"
CTX="${TMPDIR:-/tmp}/ctx_strip_$$"; mkdir -p "$CTX"
DF="$CTX/Dockerfile"
trap 'rm -rf "$CTX" "$XDG_DATA_HOME" 2>/dev/null || true' EXIT
echo "===== [$(date +%T)] strip wrappers from $TAG on $(hostname) ====="
$ENGINE load -i "$TAR" >/dev/null && echo "  loaded"
cat > "$DF" <<DOCKER
FROM $TAG
RUN rm -f /work/build_and_run.sh /work/cleanup.sh /work/entrypoint.sh /work/verify.sh 2>/dev/null || true
DOCKER
$ENGINE build --network host -t "$TAG" -f "$DF" "$CTX"; rc=$?
[ "$rc" -ne 0 ] && { echo "STRIP_OVERLAY $SIM FAIL (build rc=$rc)"; exit 1; }
echo "  verify (agent /work content; vcpkg 3rd-party excluded):"
$ENGINE run --rm "$TAG" sh -c '
  echo -n "    wrappers remaining: "; ls /work/build_and_run.sh /work/cleanup.sh /work/entrypoint.sh /work/verify.sh 2>/dev/null | tr "\n" " "; echo "(end)"
  echo -n "    archbench/archeval in /work (excl vcpkg deps): "
  grep -rIl -e archbench -e archeval /work 2>/dev/null | grep -vE "vcpkg|buildtrees" | head -5 | tr "\n" " "; echo "(end)"
'
$ENGINE save "$TAG" -o "${TAR}.partial" && mv -f "${TAR}.partial" "$TAR"
echo "STRIP_OVERLAY $SIM OK ($(ls -lah "$TAR" | awk '{print $5}'))"
