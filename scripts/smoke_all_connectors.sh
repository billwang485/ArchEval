#!/bin/bash
# =============================================================================
# scripts/smoke_all_connectors.sh
# =============================================================================
# Hello-world smoke test for every per-sim MCP connector under
# `simulators/<sim>/connector/`. For each sim, verify:
#   1. The connector package imports without error.
#   2. tool_schema.TOOLS advertises the canonical 6 tools.
#   3. server_subprocess.main is callable (--help exits cleanly).
#
# This is a CONTRACT TEST — it does NOT bring up a sim container.
# Full bring-up + tools/list-over-HTTP is in tests/test_connectors_smoke.py.
# Run that one on a host with podman + the sim images loaded.
#
# Usage:
#   bash scripts/smoke_all_connectors.sh
# Exit code:
#   0 = all 7 connectors green; 1 = any failure (per-sim line shows which).
# =============================================================================
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-python3.11}"
EXPECTED_TOOLS="submit submit_and_wait check_submission session_end browse_simulator read_simulator_file"

cd "$REPO_ROOT"
echo "=== hello-world smoke test for all sim connectors ==="
echo "expected tools: ${EXPECTED_TOOLS}"
echo

fail=0
for sim_dir in simulators/*/; do
    sim="$(basename "$sim_dir")"
    if [ ! -d "${sim_dir}connector" ]; then
        echo "  ${sim}: SKIP (no connector/ subdir)"
        continue
    fi
    # 1. import test
    if ! "$PY" -c "from simulators.${sim}.connector.tool_schema import TOOLS; assert len(TOOLS) == 6, f'expected 6 tools, got {len(TOOLS)}'" 2>/tmp/smoke_err; then
        echo "  ${sim}: FAIL tool_schema import — $(cat /tmp/smoke_err)"
        fail=1
        continue
    fi
    # 2. tool names match
    names_match="$("$PY" -c "
from simulators.${sim}.connector.tool_schema import TOOLS
got = {t.name for t in TOOLS}
want = set('${EXPECTED_TOOLS}'.split())
print('ok' if got == want else f'mismatch: missing={want-got} extra={got-want}')
")"
    if [ "$names_match" != "ok" ]; then
        echo "  ${sim}: FAIL tool name set — $names_match"
        fail=1
        continue
    fi
    # 3. server_subprocess --help exits cleanly
    if ! "$PY" -m "simulators.${sim}.connector.server_subprocess" --help >/dev/null 2>/tmp/smoke_err; then
        echo "  ${sim}: FAIL server_subprocess --help — $(head -3 /tmp/smoke_err)"
        fail=1
        continue
    fi
    echo "  ${sim}: OK"
done

echo
if [ "$fail" -eq 0 ]; then
    echo "all sim connectors green ✓"
else
    echo "some sim connectors failed (exit 1)"
fi
exit $fail
