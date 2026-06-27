#!/bin/bash
# load_sim_image.sh — turn a legacy ARCHEVAL simulator tar into a usable
# archbench-* image.
#
# What it does:
#   1. podman load the tar       (yields localhost/archeval-<sim>:v6)
#   2. retag to localhost/archbench-<sim>:v6
#   3. inject /work → /archeval symlink (renamed plugin code expects /work/)
#   4. overlay our NEW verify.sh, build_and_run.sh, cleanup.sh from
#      simulators/<sim>/ on top of the legacy scripts. The new scripts:
#       - emit ARCHBENCH_JSON_START/END instead of ARCHEVAL_JSON_*
#       - add a verify.sh (legacy had none — verify was inline Python)
#       - keep /work/runtimes/<sim>/ paths intact (resolved via symlink)
#   5. smoke-test by running /work/verify.sh inside the patched image.
#
# Usage:
#   ./scripts/load_sim_image.sh <sim_name> [tar_path]
#
#   sim_name  = champsim | dramsys | ramulator | scalesim | timeloop
#               (astrasim/gem5 have no legacy tar — build from Dockerfile.)
#   tar_path  = optional; defaults to $ARCHBENCH_LEGACY_TAR_DIR/<sim>/archeval-<sim>-v6.tar
#               (ARCHBENCH_LEGACY_TAR_DIR is the colon-separated tar search dir also
#                used by archbench/core/container.py). Pass tar_path explicitly to override.
#
# Idempotent: re-running re-patches the existing image cleanly.

set -euo pipefail

# Container engine: ARCHBENCH_CONTAINER_CLI override, else prefer docker then
# podman (mirrors archbench/core/engine.py::container_engine; docker-first keeps
# behavior identical on a box where docker works).
ENGINE="${ARCHBENCH_CONTAINER_CLI:-$(command -v docker >/dev/null 2>&1 && echo docker || echo podman)}"

SIM="${1:?usage: $0 <sim_name> [tar_path]}"
# Default the tar to the first entry of ARCHBENCH_LEGACY_TAR_DIR (the same colon-list
# archbench/core/container.py searches). Empty if unset → handled below.
DEFAULT_TAR="${ARCHBENCH_LEGACY_TAR_DIR:+${ARCHBENCH_LEGACY_TAR_DIR%%:*}/${SIM}/archeval-${SIM}-v6.tar}"
TAR="${2:-$DEFAULT_TAR}"
[ -n "$TAR" ] || { echo "[load_sim_image] ERROR: no tar path. Pass [tar_path] or set ARCHBENCH_LEGACY_TAR_DIR." >&2; exit 1; }

OLD_TAG="localhost/archeval-${SIM}:v6"
NEW_TAG="localhost/archbench-${SIM}:v6"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SIM_DIR="${REPO_ROOT}/simulators/${SIM}"

log() { echo "[load_sim_image] $*" >&2; }

[ -f "$TAR" ]      || { log "ERROR: tar not found: $TAR"; exit 1; }
[ -d "$SIM_DIR" ]  || { log "ERROR: sim dir not found: $SIM_DIR"; exit 1; }
for f in verify.sh build_and_run.sh cleanup.sh; do
    [ -f "${SIM_DIR}/${f}" ] || { log "ERROR: missing ${SIM_DIR}/${f}"; exit 1; }
done

# --- 1. Load + tag (skip if NEW_TAG already exists) ---
if "$ENGINE" image exists "$NEW_TAG" 2>/dev/null; then
    log "$NEW_TAG already present — will re-patch in place."
else
    log "Loading $TAR (this can take 5-10 min for the big tars)..."
    "$ENGINE" load -i "$TAR"
    log "Retagging $OLD_TAG → $NEW_TAG"
    "$ENGINE" tag "$OLD_TAG" "$NEW_TAG"
fi

# --- 2. Start patch container ---
PATCH_NAME="archbench_patch_${SIM}_$$"
log "Starting patch container ${PATCH_NAME}..."
"$ENGINE" run --name "$PATCH_NAME" -d --entrypoint sleep "$NEW_TAG" 120 >/dev/null
trap '"$ENGINE" rm -f "$PATCH_NAME" >/dev/null 2>&1 || true' EXIT

# --- 3. Symlink /work → /archeval and per-sim layout patches ---
log "Ensuring /work → /archeval symlink + layout patches..."
"$ENGINE" exec "$PATCH_NAME" sh -c '
    set -e
    # /work → /archeval (renamed plugin code expects /work/...)
    if [ ! -e /work ] || [ ! -L /work ]; then
        rm -rf /work
        ln -sf /archeval /work
    fi
    # Ensure submission dir exists (some legacy images omit it)
    mkdir -p /archeval/submission
'

# Sim-specific layout patches (the legacy image was built before our
# `src/archeval/` → `src/work/` rename for Ramulator's custom component
# subtree; symlink so the new verify.sh / cleanup.sh paths resolve).
case "$SIM" in
    ramulator)
        log "  ramulator: symlinking src/work → src/archeval"
        "$ENGINE" exec "$PATCH_NAME" sh -c '
            d=/archeval/runtimes/ramulator/src
            if [ -d "$d/archeval" ] && [ ! -e "$d/work" ]; then
                ln -sf archeval "$d/work"
            fi
        '
        ;;
esac

# --- 4. Overlay our new scripts ---
log "Overlaying new verify.sh / build_and_run.sh / cleanup.sh..."
for f in verify.sh build_and_run.sh cleanup.sh; do
    "$ENGINE" cp "${SIM_DIR}/${f}" "${PATCH_NAME}:/work/${f}"
done
"$ENGINE" exec "$PATCH_NAME" chmod +x /work/verify.sh /work/build_and_run.sh /work/cleanup.sh

# --- 5. Smoke-test verify.sh ---
log "Smoke-testing /work/verify.sh..."
SMOKE_OUT="$("$ENGINE" exec "$PATCH_NAME" /work/verify.sh 2>&1 || true)"
echo "$SMOKE_OUT" | sed 's/^/  /'
if echo "$SMOKE_OUT" | grep -q "^VERIFY_OK"; then
    log "VERIFY_OK from verify.sh"
elif echo "$SMOKE_OUT" | grep -q "^VERIFY_FAILED"; then
    log "WARNING: verify.sh reports failures (see above). Image still committed;"
    log "         these are likely fixable by adjusting verify.sh expectations."
else
    log "WARNING: verify.sh did not print VERIFY_OK or VERIFY_FAILED."
fi

# --- 6. Commit ---
# Clear the legacy ENTRYPOINT and set CMD=["sleep","infinity"]. The
# frozen archeval tarballs ship Entrypoint=[sleep] CMD=[120], so when
# ContainerManager.start() runs `podman run <img> sleep infinity` it
# becomes `sleep sleep infinity` → "invalid time interval" → the sim
# container exits in ~12 ms and the orchestrator sees CONTAINER_DEAD
# before verify.sh can run. (lessons_learned.md §17.)
log "Committing patched image (clearing legacy ENTRYPOINT)..."
"$ENGINE" commit \
    --change 'ENTRYPOINT []' \
    --change 'CMD ["sleep", "infinity"]' \
    "$PATCH_NAME" "$NEW_TAG" >/dev/null

"$ENGINE" rm -f "$PATCH_NAME" >/dev/null
trap - EXIT

log "OK: $NEW_TAG is patched and ready."
log "    Run \`podman run --rm --entrypoint /work/verify.sh $NEW_TAG\` to re-verify."

# Freeze the patched image as a stable, content-pinned tar so FUTURE loads
# (especially on a fresh node, where the node-local image store is empty) are
# DETERMINISTIC: ensure_image reloads this exact digest instead of re-running
# this script and `podman commit`-ing a NEW digest each time (which would drift
# every baseline's image_digest provenance — §1.7). Save ONCE; never overwrite
# an existing frozen artifact, or we'd re-drift the digest the baselines pin.
SLUG="${NEW_TAG##*/}"            # localhost/archbench-champsim:v6 -> archbench-champsim:v6
SLUG="${SLUG%:*}-${SLUG##*:}"    # archbench-champsim:v6 -> archbench-champsim-v6
STABLE_TAR="${REPO_ROOT}/docker/${SLUG}.tar"
if [ -f "$STABLE_TAR" ]; then
    log "Stable tar already present ($STABLE_TAR) — NOT overwriting (preserves the"
    log "    digest the baselines pin). Delete it manually only if intentionally re-pinning."
else
    log "Freezing patched image -> $STABLE_TAR (future ensure_image loads are deterministic)..."
    if "$ENGINE" save -o "$STABLE_TAR" "$NEW_TAG"; then
        log "    saved $(du -h "$STABLE_TAR" 2>/dev/null | cut -f1) — this tar is gitignored; keep it on shared storage."
    else
        log "    WARN: podman save failed; image is loaded but not frozen to a stable tar."
    fi
fi
