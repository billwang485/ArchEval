#!/bin/bash
# build_l2agent_image.sh — build the L2 simulator development agent image.
#
# L2 sim_dev_env profile: the agent runs inside the simulator image, with the
# simulator source and dependencies/toolchain already installed, so it can
# build, configure, and run the real simulator itself (no browse/read MCP
# tools). This image is the existing per-sim image PLUS the mini agent loop
# (/opt/mini) and its agent-loop dependencies. session.py selects it for
# challenges with session_profile: sim_dev_env.
#
# Usage: ./scripts/build_l2agent_image.sh <sim>   (e.g. champsim)
set -euo pipefail

# Container engine: ARCHBENCH_CONTAINER_CLI override, else prefer docker then
# podman (mirrors archbench/core/engine.py::container_engine; docker-first keeps
# behavior identical on a box where docker works).
ENGINE="${ARCHBENCH_CONTAINER_CLI:-$(command -v docker >/dev/null 2>&1 && echo docker || echo podman)}"

SIM="${1:?usage: $0 <sim>   (champsim|gem5|...)}"
# Base sim image tag is PER-SIM (gem5 is v7, others v6); read it from the
# manifest (the single source of truth = images.yaml) so this never drifts.
# Fallback to v6 if the manifest can't be read. The combined image inherits the
# base's tag; the agent-mini layer is always v6.
BASE="$(python3.11 -c "from archbench.image_management import manifest as m; print(m.fully_qualified('simulators','${SIM}'))" 2>/dev/null || echo "localhost/archbench-${SIM}:v6")"
TAG="${BASE##*:}"
MINI="localhost/archbench-agent-mini:v6"
OUT="localhost/archbench-${SIM}-l2agent:${TAG}"

# Scaffold-strip glob: the L2 image bakes the sim source in ON PURPOSE so the
# agent reads/builds it directly, but the hand-authored reference/answer
# scaffold (the submission-target module slot) MUST be physically stripped so
# the agent can't read or submit it. For champsim the convention is the module
# dir <comp_dir>/<comp_name> named candidate* under /work/runtimes/<sim>.
# Future non-champsim sims can pass their own slot name via this env override;
# champsim's candidate* default is correct for the 6 ChampSim challenges.
#
# NOTE: the default is applied ONLY when ARCHBENCH_L2_SCAFFOLD_GLOB is UNSET
# (no-colon form): an UNSET var -> champsim's candidate* default (byte-
# identical to historic behavior); an EXPLICITLY-EMPTY var (callers pass
# ARCHBENCH_L2_SCAFFOLD_GLOB="" for gem5/mnsim) STAYS empty and DISABLES the
# strip layer + verify strip-assert below. Those sims never bake a host
# reference/solution into the image (see their Dockerfiles), so there is
# nothing to strip; they rely solely on starter_visibility:none.
SCAFFOLD_GLOB="${ARCHBENCH_L2_SCAFFOLD_GLOB-candidate*}"

"$ENGINE" image exists "$BASE" || { echo "[l2agent] ERROR: base image $BASE not loaded." >&2; exit 1; }
"$ENGINE" image exists "$MINI" || { echo "[l2agent] ERROR: mini image $MINI not loaded." >&2; exit 1; }

echo "[l2agent] building $OUT  (FROM $BASE + /opt/mini + agent loop deps + agent user)"

# Assemble the Dockerfile in a temp file so the CONDITIONAL scaffold-strip
# layer can be included verbatim (a heredoc into a file preserves the RUN's
# `\`-continued newlines byte-for-byte; capturing into a $()-variable would
# collapse them). The result is fed to the engine via `-f - . < $DOCKERFILE`,
# so the stdin contract is unchanged from the historic inline heredoc; for
# champsim (default glob) the bytes piped to the engine are IDENTICAL.
DOCKERFILE="$(mktemp)"
trap 'rm -f "$DOCKERFILE"' EXIT

cat > "$DOCKERFILE" <<DOCKER
FROM ${BASE}
COPY --from=${MINI} /opt/mini /opt/mini
DOCKER

# Scaffold-strip layer (CONDITIONAL on a non-empty SCAFFOLD_GLOB). It runs
# BEFORE the agent-user/chown layer so we never chown a scaffold we're about
# to delete, and so a leak hard-fails the build:
#   (a) delete the agent's submission-target module slot(s) if present; then
#   (b) HARD-FAIL the build if ANY hand-authored reference scaffold remains on
#       disk. /work is the deferred container-internal runtimes path (keep /work).
# Sims that never bake a host reference into the image (gem5/mnsim, which pass
# ARCHBENCH_L2_SCAFFOLD_GLOB="") emit NO strip layer — nothing to strip.
if [ -n "$SCAFFOLD_GLOB" ]; then
  cat >> "$DOCKERFILE" <<DOCKER
# Scaffold-strip layer (runs BEFORE the agent-user/chown layer so we never
# chown a scaffold we're about to delete, and so a leak hard-fails the build).
# (a) delete the agent's submission-target module slot(s) if present; then
# (b) HARD-FAIL the build if ANY hand-authored reference scaffold remains on
# disk. /work is the deferred container-internal runtimes path (keep as /work).
RUN set -e; \\
    rm -rf /work/runtimes/${SIM}/*/${SCAFFOLD_GLOB} ; \\
    if find /work/runtimes/${SIM} -name '${SCAFFOLD_GLOB}' | grep -q . ; then \\
      echo "[l2agent] STRIP FAILED: reference scaffold present" >&2; \\
      find /work/runtimes/${SIM} -name '${SCAFFOLD_GLOB}' >&2; exit 1; fi
DOCKER
else
  echo "[l2agent] no scaffold glob for ${SIM}; relying on starter_visibility:none (host reference never baked)"
fi

# Neutralization layer: strip the harness build wrappers from the agent-facing
# /work. They are base leftovers UNUSED by the L2 flow — the agent's container
# runs the baked agent loop (/opt/mini); the L2 eval runs in the SEPARATE,
# pristine evaluation_sim_image (which keeps its own copies). These wrappers
# carry ARCHBENCH_* marker/comment strings a curious agent could `cat` to infer
# it is being benchmarked. The L2 agent builds the real simulator with its
# NATIVE build system, not these harness wrappers.
cat >> "$DOCKERFILE" <<DOCKER
RUN rm -f /work/build_and_run.sh /work/cleanup.sh /work/entrypoint.sh /work/verify.sh 2>/dev/null || true
DOCKER

# --network host: pip needs egress at BUILD time only; the agent runs with no
# task-facing network. The agent loop's external deps are httpx (it speaks the
# OpenAI-compatible API over raw httpx; no openai package needed) and clang
# (mini's verify.sh checks `import clang.cindex`). The sim image runs as root
# with no agent user, so we add the agent-runtime contract the mini runner
# expects: an `agent` user, /home/agent, an agent-owned /workspace, AND
# agent ownership of the sim's build dir so the agent can compile the real
# simulator itself in THIS (throwaway, per-run) container. The Oracle scores
# in a SEPARATE pristine sim container, so chowning here can't taint scoring.
cat >> "$DOCKERFILE" <<DOCKER
RUN (python3 -m pip --version >/dev/null 2>&1 \
       || python3 -m ensurepip --upgrade >/dev/null 2>&1 \
       || (apt-get update && apt-get install -y --no-install-recommends python3-pip)) \
 && (python3 -m pip install --no-cache-dir httpx clang \
       || python3 -m pip install --break-system-packages --no-cache-dir httpx clang) \
 && (id agent >/dev/null 2>&1 || useradd -m -u 1000 -s /bin/bash agent 2>/dev/null || useradd -m -s /bin/bash agent) \
 && mkdir -p /workspace /home/agent \
 && chown -R agent:agent /workspace /home/agent /opt/mini \
 && (chown -R agent:agent /work/runtimes/${SIM} 2>/dev/null || true)
DOCKER

"$ENGINE" build --network host -t "$OUT" -f - . < "$DOCKERFILE"

echo "[l2agent] done: $OUT"
# Verify-smoke. The /opt/mini + httpx + clang + agent-user checks run
# UNCONDITIONALLY. The scaffold strip-assert (and its mention in the success
# line) is appended ONLY when SCAFFOLD_GLOB is non-empty — byte-identical to
# the historic single command for champsim's default candidate*.
if [ -n "$SCAFFOLD_GLOB" ]; then
  VERIFY_CMD="
  test -x /opt/mini/main.py && python3 -c 'import httpx' \
  && python3 -c 'import clang.cindex' && id agent >/dev/null \
  && test -d /home/agent && test -w /workspace \
  && if find /work/runtimes/${SIM} -name '${SCAFFOLD_GLOB}' 2>/dev/null | grep -q . ; then \
       echo '[l2agent] VERIFY FAILED: scaffold ${SCAFFOLD_GLOB} still on disk' >&2; \
       find /work/runtimes/${SIM} -name '${SCAFFOLD_GLOB}' >&2; exit 1; fi \
  && echo '[l2agent] verify OK: /opt/mini + httpx + clang + agent user present; no ${SCAFFOLD_GLOB} scaffold on disk'"
else
  VERIFY_CMD="
  test -x /opt/mini/main.py && python3 -c 'import httpx' \
  && python3 -c 'import clang.cindex' && id agent >/dev/null \
  && test -d /home/agent && test -w /workspace \
  && echo '[l2agent] verify OK: /opt/mini + httpx + clang + agent user present (no scaffold glob; nothing to strip)'"
fi
"$ENGINE" run --rm "$OUT" sh -c "$VERIFY_CMD" \
  || { echo '[l2agent] VERIFY FAILED' >&2; exit 1; }
