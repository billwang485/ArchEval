#!/bin/sh
# agent-codex verify.sh — Codex CLI (pinned version baked in image).
# Past bug: silent CLI binary swap broke trajectory parsing.
set -e

EXPECTED_VERSION="${EXPECTED_CODEX_VERSION:-0.137.0}"

test -x /usr/local/bin/codex || { echo "CHECK_FAILED: /usr/local/bin/codex missing"; exit 1; }
/usr/local/bin/codex --version 2>&1 | grep -q "${EXPECTED_VERSION}" || { echo "CHECK_FAILED: codex version mismatch (expected ${EXPECTED_VERSION})"; exit 1; }

echo "VERIFY_OK"
