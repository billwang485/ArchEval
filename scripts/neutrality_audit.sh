#!/bin/bash
# neutrality_audit.sh — for each agent-facing image, prove the agent finds NO
# 'archbench'/'archeval' via ls/cd (paths) or env. Loads from the pool, runs the
# checks in-container.
set -uo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"
ENGINE="${ARCHBENCH_CONTAINER_CLI:-podman}"
declare -A IMG=(
  [agent-mini]=docker/archbench-agent-mini-v6.tar
  [champsim-l2agent]=docker/archbench-champsim-l2agent-v6.tar
  [gem5-l2agent]=docker/archbench-gem5-l2agent-v7.tar
  [mnsim-l2agent]=docker/archbench-mnsim-l2agent-v6.tar
)
for name in agent-mini champsim-l2agent gem5-l2agent mnsim-l2agent; do
  tar="${IMG[$name]}"; [ -f "$tar" ] || { echo "### $name: tar MISSING"; continue; }
  ref=$($ENGINE load -i "$tar" 2>/dev/null | sed -n 's/.*: //p' | tail -1)
  echo "### $name  ($ref)"
  $ENGINE run --rm "$ref" sh -c '
    echo -n "  path hits (archbench/archeval, excl /proc /sys): "
    find / -xdev \( -path /proc -o -path /sys \) -prune -o \( -iname "*archbench*" -o -iname "*archeval*" \) -print 2>/dev/null | head -8 | tr "\n" " "; echo
    echo -n "  env hits: "; env | grep -iE "archbench|archeval" | tr "\n" " "; echo "(end)"
    echo -n "  top-level dirs the agent ls: "; ls / | tr "\n" " "; echo
    # CONTENT check on the agent task dirs (/work, /workspace) — what the agent
    # could cat/grep while working. (NOT /opt/<driver>: that is the harness loop
    # source, which imports the framework by design and is not task material.)
    echo -n "  content hits in /work + /workspace (archbench/archeval): "
    grep -rIl -e archbench -e archeval /work /workspace 2>/dev/null | head -6 | tr "\n" " "; echo "(end)"
  ' 2>&1 | sed 's/^/  /'
  $ENGINE rmi "$ref" >/dev/null 2>&1 || true
done
echo "NEUTRALITY_AUDIT_DONE"
