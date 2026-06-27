#!/bin/sh
# Claude Code agent verify (bake_only) — invoked by
# ClaudeCodeRuntime.verify_in_container.
#
# The Claude binary is baked into the image at build time
# (see runtimes/claude_code/Dockerfile). This script's job is to
# assert that the baked binary exists and reports the pinned version.
# Hardcoded version matches runtimes/claude_code/info.yaml runtime_version
# (this script ships with the image at build time, so they're frozen
# together).
set -e

CLAUDE=/usr/local/bin/claude
EXPECTED_VERSION=2.1.170

test -x "$CLAUDE" || { echo "CHECK_FAILED: $CLAUDE missing or not executable"; exit 1; }
"$CLAUDE" --version 2>&1 | grep -q "$EXPECTED_VERSION" || {
    echo "CHECK_FAILED: claude version mismatch (expected $EXPECTED_VERSION)";
    "$CLAUDE" --version 2>&1
    exit 1
}
echo "VERIFY_OK"
