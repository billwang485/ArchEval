#!/bin/bash
# mini-architect-bench V1 — gem5 Cleanup
#
# Resets simulator container to pristine state between challenges.
# Must be idempotent. After this returns, verify.sh must pass.
set -e

# Kill any stray gem5 processes
pkill -f "gem5" 2>/dev/null || true
sleep 0.5
pkill -9 -f "gem5" 2>/dev/null || true

# Clear submission directory (including dotfiles)
rm -rf /work/submission
mkdir -p /work/submission

# Clear gem5 workdir and temp files (including dotfiles)
rm -rf /tmp/gem5_run
rm -rf /tmp/gem5*
rm -rf /tmp/.gem5*

echo "CLEANUP_OK"
