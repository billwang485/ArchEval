#!/bin/sh
# agent-gemini verify.sh — Gemini CLI (pinned version baked in image).
set -e

EXPECTED_VERSION="${EXPECTED_GEMINI_VERSION:-0.38.1}"

test -x /usr/local/bin/gemini || { echo "CHECK_FAILED: /usr/local/bin/gemini missing"; exit 1; }
/usr/local/bin/gemini --version 2>&1 | grep -q "${EXPECTED_VERSION}" || { echo "CHECK_FAILED: gemini version mismatch (expected ${EXPECTED_VERSION})"; exit 1; }

echo "VERIFY_OK"
