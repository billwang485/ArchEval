#!/bin/bash
# stamp_cards.sh — stamp a container card for each pool image on the CURRENT
# (known-good, neutralized) bits. Run once; thereafter ensure_image verifies
# every load against these cards. Loads each image, stamps, rmi to free space.
# Disk-safe (podman storage on big local /tmp). Run with: srun --tmp=40G ...
set -uo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"
export XDG_DATA_HOME="${TMPDIR:-/tmp}/pod_card_$$"; mkdir -p "$XDG_DATA_HOME"
ENGINE=podman

stamp() {  # <image> <role> [sim]
  local img="$1" role="$2" sim="${3:-}"
  local slug="${img##*/}"; slug="${slug/:/-}"
  local tar="docker/${slug}.tar"
  echo "===== [$(date +%T)] $img  ($role${sim:+ $sim}) ====="
  if [ ! -f "$tar" ]; then echo "  SKIP: no tar $tar"; return; fi
  $ENGINE load -i "$tar" >/dev/null 2>&1 || { echo "  LOAD FAIL"; return; }
  python3.11 -m archbench.cli card stamp "$img" --role "$role" ${sim:+--sim "$sim"} \
    2>&1 | grep -vE "^image:|^stamped_at|^stamped_from" | sed 's/^/  /'
  $ENGINE rmi -f "$img" >/dev/null 2>&1 || true
}

stamp localhost/archbench-champsim:v6        simulator champsim
stamp localhost/archbench-gem5:v7            simulator gem5
stamp localhost/archbench-mnsim:v6           simulator mnsim
stamp localhost/archbench-agent-mini:v6      agent
stamp localhost/archbench-champsim-l2agent:v6 agent_sim champsim
stamp localhost/archbench-gem5-l2agent:v7     agent_sim gem5
stamp localhost/archbench-mnsim-l2agent:v6    agent_sim mnsim
echo "=== cards written ==="; ls -1 docker/*.card.yaml 2>/dev/null | sed 's/^/  /'
echo "STAMP_CARDS_DONE"
