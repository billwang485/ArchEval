# Claude Code agent image (off-the-shelf, mode: bake_only).
# Binary version pinned at build time via npm; runtime contains no host
# file injection of the claude binary (only the per-user OAuth token).
#
# npm package: @anthropic-ai/claude-code (confirmed via
# `npm view @anthropic-ai/claude-code@2.1.170 version` → 2.1.170).
# Updating Claude Code = bump the version pin below + rebuild +
# update runtimes/claude_code/info.yaml `runtime_version`.
#
# Build (from project root):
#   podman build -t localhost/archbench-agent:v6 -f runtimes/claude_code/Dockerfile .
FROM node:22-bookworm-slim

LABEL maintainer="mini-architect-bench Team"
LABEL description="Claude Code v2.1.170 agent (bake_only)"

ENV DEBIAN_FRONTEND=noninteractive

# Base tools + python3/libclang for challenge validate.py AST audits
# (challenges write validate.py into /workspace and invoke it via
# `python3 validate.py`; libclang-dev provides the shared object that
# python3-clang loads at runtime).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    bash \
    coreutils \
    gawk \
    grep \
    sed \
    python3 \
    python3-pip \
    libclang-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir --break-system-packages clang==14.0.0

# Pin Claude Code at v2.1.170 (matches runtimes/claude_code/info.yaml runtime_version).
# Updating Claude Code = bump this + rebuild + update info.yaml.
RUN npm install -g @anthropic-ai/claude-code@2.1.170 \
    && npm cache clean --force
RUN claude --version  # smoke: asserts the binary works

# Container layout expected by the runner + challenge stager
RUN mkdir -p /workspace /traces/decoded /api

# Non-root agent user. Claude CLI refuses --dangerously-skip-permissions
# when EUID=0, so the runner execs as `agent` (uid 1000).
# node:22-bookworm-slim ships a "node" user at UID 1000; remove it first,
# then create our "agent" user at the same UID so chown/perms inside the
# container behave like the legacy ubuntu-based image expected.
RUN userdel -r node 2>/dev/null || true \
    && useradd -m -u 1000 -s /bin/bash agent \
    && chown -R agent:agent /workspace /traces /api /home/agent

# Suppress podman-docker banner
RUN mkdir -p /etc/containers && touch /etc/containers/nodocker

# Working dir for agent operations
WORKDIR /workspace

CMD ["sleep", "infinity"]
