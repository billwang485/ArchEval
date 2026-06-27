#!/bin/bash
# agent-mini verify.sh — miniswe-style Python agent loop driving Gemma via vLLM.
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

check "python3 present"       which python3
check "httpx installed"       python3 -c "import httpx"
check "clang AST module"      python3 -c "import clang.cindex"
check "/workspace exists"     test -d /workspace
check "agent user home"       test -d /home/agent
check "mini runtime baked"    test -x /opt/mini/main.py

if [ -n "${LLM_BASE_URL:-}" ]; then
    if python3 -c "
import os, sys, httpx
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
    echo "CHECK_INFO: LLM_BASE_URL unset — skipping endpoint probe"
fi

if [ "$FAILED" -eq 0 ]; then
    echo "VERIFY_OK"
    exit 0
fi
echo "VERIFY_FAILED: $FAILED checks failed"
exit 1
