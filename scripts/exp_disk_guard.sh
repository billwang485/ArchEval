#!/bin/bash
# exp_disk_guard.sh — disk-full prevention for multi-session experiments.
#
# WHY: podman's graphroot lives on /tmp (a ~69G scratch LV here), and an
# experiment that spawns many sim/eval containers can fill it — especially if
# the run is killed (ENOSPC, SIGKILL) BEFORE the per-run atexit `podman rm`
# (CLAUDE.md §1.8) fires, leaving orphan containers whose writable overlay
# layers (ChampSim intermediates etc.) never get reaped. Once /tmp is 100%
# full, even the harness can't write command output, so recovery needs manual
# `! podman rm -f ...`. This guard makes that far less likely.
#
# Source it (`. scripts/exp_disk_guard.sh`) then call:
#   guard_precheck   <min_free_gb>   # abort (exit 1) if /tmp free < min, after reaping orphans
#   guard_reap                       # remove ALL stopped/exited containers (orphan reaper)
#   guard_df                         # print one-line /tmp usage

guard_df() { df -BG /tmp | awk 'NR==2{print "/tmp free="$4" used="$3"/"$2}'; }

guard_reap() {
    # Remove exited/created containers (orphans). Running containers are left
    # alone. Safe between experiment batches and at start.
    local ids
    ids="$(podman ps -aq --filter status=exited --filter status=created 2>/dev/null)"
    [ -n "$ids" ] && podman rm -f $ids >/dev/null 2>&1
    podman container prune -f >/dev/null 2>&1 || true
}

guard_precheck() {
    local min_gb="${1:-15}"
    guard_reap
    local free_gb
    free_gb="$(df -BG /tmp | awk 'NR==2{gsub("G","",$4); print $4}')"
    if [ "${free_gb:-0}" -lt "$min_gb" ]; then
        echo "GUARD ABORT: /tmp free ${free_gb}G < required ${min_gb}G even after reaping orphans." >&2
        echo "  Free space first:  podman rm -f \$(podman ps -aq); podman image prune -af" >&2
        echo "  (or remove unused sim images: podman rmi localhost/archbench-gem5:v7 ...)" >&2
        return 1
    fi
    echo "GUARD OK: $(guard_df) (>= ${min_gb}G required)"
    return 0
}
