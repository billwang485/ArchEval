#!/bin/bash
# agent-archharness verify.sh — local Python agent loop driving Gemma via vLLM.
#
# Structural fix for lessons §7: archharness used to skip the vLLM endpoint
# probe, so a dead model server only failed mid-round. Now: if LLM_BASE_URL
# is set, we HTTP GET /v1/models and fail-fast if unreachable.
set -uo pipefail

FAILED=0
check() {
    local name="$1"; shift
    if "$@" >/dev/null 2>&1; then
        echo "CHECK_OK: $name"
    else
        echo "CHECK_FAILED: $name"
        FAILED=$((FAILED + 1))
    fi
}

check "python3 present"   which python3
check "httpx installed"   python3 -c "import httpx"
check "clang AST module"  python3 -c "import clang.cindex"
check "/workspace exists" test -d /workspace
check "agent user home"   test -d /home/agent
check "archharness runtime baked" test -x /opt/archharness/main.py

# vLLM endpoint probe — only when LLM_BASE_URL is set (verify-all without it
# is a structural sanity pass; full preflight runs with the env var set).
if [ -n "${LLM_BASE_URL:-}" ]; then
    if python3 -c "
import os, sys, httpx
# LLM_BASE_URL convention contains /v1 (OpenAI-compatible). Append
# /models, not /v1/models — otherwise we hit /v1/v1/models and get 404.
base = os.environ['LLM_BASE_URL'].rstrip('/')
url = base + ('/models' if base.endswith('/v1') else '/v1/models')
try:
    r = httpx.get(url, timeout=5)
    sys.exit(0 if r.status_code == 200 else 1)
except Exception as e:
    print(f'probe error: {e}', file=sys.stderr)
    sys.exit(1)
" 2>&1; then
        echo "CHECK_OK: vLLM endpoint reachable ($LLM_BASE_URL)"
    else
        echo "CHECK_FAILED: vLLM endpoint NOT reachable ($LLM_BASE_URL)"
        FAILED=$((FAILED + 1))
    fi
else
    echo "CHECK_INFO: LLM_BASE_URL unset — skipping endpoint probe (set it for full preflight)"
fi

if [ "$FAILED" -eq 0 ]; then
    echo "VERIFY_OK"
    exit 0
fi
echo "VERIFY_FAILED: $FAILED checks failed"
exit 1
