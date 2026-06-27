#!/bin/bash
# mini-architect-bench V1 — ASTRA-sim cleanup.sh
#
# Reset container to pristine state between challenges. Must be idempotent.
set -e

# Clear submission directory
rm -rf /work/submission
mkdir -p /work/submission

# Clear any temp files from simulation
rm -f /tmp/*.json /tmp/*.yml /tmp/*.yaml /tmp/sim_output_*

echo "CLEANUP_OK"
