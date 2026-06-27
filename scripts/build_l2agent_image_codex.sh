#!/bin/bash
# build_l2agent_image_codex.sh — build the L2 simulator-dev agent image for the
# OpenAI Codex runtime.
#
# This is the CODEX counterpart of scripts/build_l2agent_image.sh (which bakes
# the MINI loop /opt/mini via COPY --from). L2 sim_dev_env profile: the agent
# runs INSIDE the simulator image, with the simulator source + toolchain already
# installed, so it can build/configure/run the real simulator itself (no
# browse/read MCP tools). This image is the per-sim image PLUS the Codex CLI
# (npm-installed at the same pinned version as runtimes/codex/Dockerfile) + the
# agent user/contract the CodexRuntime.start_session expects.
#
# WHY a separate script (not a flag on build_l2agent_image.sh): the mini build
# does `COPY --from=archbench-agent-mini /opt/mini` (a Python agent loop);
# Codex is an npm global package needing a Node >=20 runtime. The champsim base
# is ubuntu:22.04 with NO node, so we install Node 22 (NodeSource for jammy)
# then `npm install -g @openai/codex@<pin>` — mirroring runtimes/codex/Dockerfile.
# resolve_images() routes simulator_centric + runtime=codex to the OUT tag below
# (_l2agent_image(sim, "codex") -> <sim>-codex-l2agent).
#
# Usage: ./scripts/build_l2agent_image_codex.sh <sim>   (e.g. champsim)
set -euo pipefail

# Container engine: ARCHBENCH_CONTAINER_CLI override, else prefer docker then
# podman (mirrors archbench/core/engine.py::container_engine).
ENGINE="${ARCHBENCH_CONTAINER_CLI:-$(command -v docker >/dev/null 2>&1 && echo docker || echo podman)}"

SIM="${1:?usage: $0 <sim>   (champsim|gem5|...)}"
# Base sim image tag is PER-SIM (gem5 is v7, others v6); read it from the
# manifest (single source of truth = images.yaml) so this never drifts.
BASE="$(python3.11 -c "from archbench.image_management import manifest as m; print(m.fully_qualified('simulators','${SIM}'))" 2>/dev/null || echo "localhost/archbench-${SIM}:v6")"
TAG="${BASE##*:}"
# Codex CLI version pin. Single source of truth is runtimes/codex/info.yaml
# (runtime_version), kept in sync with runtimes/codex/Dockerfile. Read it so the
# baked CLI matches what verify.sh / the runner expect; fall back to 0.137.0.
CODEX_VERSION="$(python3.11 -c "import yaml,pathlib; print(yaml.safe_load(pathlib.Path('runtimes/codex/info.yaml').read_text())['runtime_version'])" 2>/dev/null || echo "0.137.0")"
# Node major for NodeSource. Codex 0.137.0 is validated on node 22
# (runtimes/codex/Dockerfile: FROM node:22-bookworm-slim).
NODE_MAJOR="${ARCHBENCH_CODEX_NODE_MAJOR:-22}"
OUT="localhost/archbench-${SIM}-codex-l2agent:${TAG}"

# Scaffold-strip glob: identical convention to build_l2agent_image.sh. The L2
# image bakes the sim source ON PURPOSE so the agent reads/builds it directly,
# but the hand-authored reference/answer scaffold (submission-target module
# slot) MUST be physically stripped. UNSET -> champsim's candidate* default;
# EXPLICITLY-EMPTY ("" passed by gem5/mnsim) DISABLES the strip + its assert.
SCAFFOLD_GLOB="${ARCHBENCH_L2_SCAFFOLD_GLOB-candidate*}"

"$ENGINE" image exists "$BASE" || { echo "[codex-l2agent] ERROR: base image $BASE not loaded." >&2; exit 1; }

echo "[codex-l2agent] building $OUT  (FROM $BASE + Node ${NODE_MAJOR} + codex@${CODEX_VERSION} + agent user)"

# Assemble the Dockerfile in a temp file so the CONDITIONAL scaffold-strip layer
# is included verbatim (a heredoc into a file preserves the RUN's `\`-continued
# newlines byte-for-byte). Fed to the engine via `-f - . < $DOCKERFILE`.
DOCKERFILE="$(mktemp)"
trap 'rm -f "$DOCKERFILE"' EXIT

cat > "$DOCKERFILE" <<DOCKER
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive
DOCKER

# Scaffold-strip layer (CONDITIONAL on a non-empty SCAFFOLD_GLOB). Runs BEFORE
# the agent-user/chown layer so we never chown a scaffold we're about to delete,
# and so a leak hard-fails the build. /work is the deferred container-internal
# runtimes path (keep /work).
if [ -n "$SCAFFOLD_GLOB" ]; then
  cat >> "$DOCKERFILE" <<DOCKER
RUN set -e; \\
    rm -rf /work/runtimes/${SIM}/*/${SCAFFOLD_GLOB} ; \\
    if find /work/runtimes/${SIM} -name '${SCAFFOLD_GLOB}' | grep -q . ; then \\
      echo "[codex-l2agent] STRIP FAILED: reference scaffold present" >&2; \\
      find /work/runtimes/${SIM} -name '${SCAFFOLD_GLOB}' >&2; exit 1; fi
DOCKER
else
  echo "[codex-l2agent] no scaffold glob for ${SIM}; relying on starter_visibility:none (host reference never baked)"
fi

# Neutralization layer: strip the harness build wrappers from the agent-facing
# /work. They are base leftovers UNUSED by the L2 flow — the L2 eval runs in the
# SEPARATE pristine evaluation_sim_image. The L2 agent builds the real simulator
# with its NATIVE build system, not these harness wrappers.
cat >> "$DOCKERFILE" <<DOCKER
RUN rm -f /work/build_and_run.sh /work/cleanup.sh /work/entrypoint.sh /work/verify.sh 2>/dev/null || true
DOCKER

# --network host: apt + the NodeSource setup + npm need egress at BUILD time
# only; the agent runs with no task-facing network. We:
#   (1) install Node ${NODE_MAJOR} via NodeSource (ubuntu jammy base has no node);
#   (2) npm install -g the pinned Codex CLI (-> /usr/local/bin/codex), same pin
#       as runtimes/codex/Dockerfile; smoke `codex --version`;
#   (3) ensure libclang + the python clang binding so the agent's
#       /workspace/validate.py (import clang.cindex) runs in-container (mirrors
#       the mini l2agent build, which pip-installs clang);
#   (4) add the agent runtime contract CodexRuntime.start_session expects: an
#       `agent` user (UID 1000), /home/agent + /home/agent/.codex, an
#       agent-owned /workspace, AND agent ownership of the sim source so the
#       agent can compile the real simulator in THIS throwaway per-run
#       container. Scoring is in a SEPARATE pristine sim image, so chowning here
#       can't taint scoring.
cat >> "$DOCKERFILE" <<DOCKER
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg git libclang-dev \
 && mkdir -p /etc/apt/keyrings \
 && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
 && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends nodejs \
 && npm config set prefix /usr/local \
 && npm install -g @openai/codex@${CODEX_VERSION} \
 && npm cache clean --force \
 && (test -x /usr/local/bin/codex || ln -sf "\$(command -v codex)" /usr/local/bin/codex) \
 && /usr/local/bin/codex --version \
 && (python3 -m pip install --no-cache-dir clang \
       || python3 -m pip install --break-system-packages --no-cache-dir clang \
       || pip3 install --no-cache-dir clang || true) \
 && (id agent >/dev/null 2>&1 || useradd -m -u 1000 -s /bin/bash agent 2>/dev/null || useradd -m -s /bin/bash agent) \
 && mkdir -p /workspace /home/agent/.codex \
 && chown -R agent:agent /workspace /home/agent \
 && (chown -R agent:agent /work/runtimes/${SIM} 2>/dev/null || true) \
 && rm -rf /var/lib/apt/lists/*
DOCKER

# Suppress the podman banner that pollutes tool outputs (mirrors codex Dockerfile).
cat >> "$DOCKERFILE" <<DOCKER
RUN mkdir -p /etc/containers && touch /etc/containers/nodocker
WORKDIR /workspace
DOCKER

"$ENGINE" build --network host -t "$OUT" -f - . < "$DOCKERFILE"

echo "[codex-l2agent] done: $OUT"

# Verify-smoke. The codex CLI + version + agent-user + /workspace checks run
# UNCONDITIONALLY. The scaffold strip-assert is appended ONLY when SCAFFOLD_GLOB
# is non-empty.
COMMON_CHECK="test -x /usr/local/bin/codex \
  && /usr/local/bin/codex --version 2>&1 | grep -q '${CODEX_VERSION}' \
  && id agent >/dev/null && test -d /home/agent/.codex && test -w /workspace"
if [ -n "$SCAFFOLD_GLOB" ]; then
  VERIFY_CMD="
  ${COMMON_CHECK} \
  && if find /work/runtimes/${SIM} -name '${SCAFFOLD_GLOB}' 2>/dev/null | grep -q . ; then \
       echo '[codex-l2agent] VERIFY FAILED: scaffold ${SCAFFOLD_GLOB} still on disk' >&2; \
       find /work/runtimes/${SIM} -name '${SCAFFOLD_GLOB}' >&2; exit 1; fi \
  && echo '[codex-l2agent] verify OK: codex ${CODEX_VERSION} + agent user present; no ${SCAFFOLD_GLOB} scaffold on disk'"
else
  VERIFY_CMD="
  ${COMMON_CHECK} \
  && echo '[codex-l2agent] verify OK: codex ${CODEX_VERSION} + agent user present (no scaffold glob; nothing to strip)'"
fi
# Run the verify as the `agent` user WITH HOME/CODEX_HOME set exactly as
# CodexRuntime.start_session sets them (docker exec -e HOME=/home/agent
# -e CODEX_HOME=/home/agent/.codex). Without HOME, the codex CLI tries to
# resolve ~/.codex against an unset/`/` home and errors out. Capture output so a
# failure names which check tripped instead of a bare "VERIFY FAILED".
# NB: `set -e` aborts the whole script on a failing `VAR="$(cmd)"` BEFORE the
# diagnostic can print, so disable -e just around the capture.
set +e
VERIFY_OUT="$("$ENGINE" run --rm --user agent \
  -e HOME=/home/agent -e CODEX_HOME=/home/agent/.codex \
  "$OUT" sh -c "$VERIFY_CMD" 2>&1)"
VRC=$?
set -e
echo "$VERIFY_OUT" | sed 's/^/  [verify] /'
# Use `if` (not `[ ] && {}`): a trailing `[ false ] && {}` returns rc=1 and,
# as the script's last statement under `set -e`, would exit the build non-zero
# even on a PASSING verify.
if [ "$VRC" -ne 0 ]; then
  echo "[codex-l2agent] VERIFY FAILED (rc=$VRC)" >&2
  exit 1
fi
echo "[codex-l2agent] build + verify complete: $OUT"
