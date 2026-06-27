# mini-architect-bench V6 — Codex CLI Agent Container
#
# Off-the-shelf agent (mode: bake_only). The Codex CLI is pinned at
# v0.137.0 and baked into the image at build-time via npm. Only the
# per-user auth.json is mounted in at session start.
#
# Runtime injections (per session):
#   /workspace/              — starter code, prompt.md, AGENTS.md
#   /traces/decoded          — decoded workload traces (read-only)
#   /api/                    — simulator interface docs (read-only)
#   /home/agent/.codex/      — auth.json (docker cp in), config.toml (MCP)
#
# Build:
#   docker build -t localhost/archbench-agent-codex:v6 -f runtimes/codex/Dockerfile .

FROM node:22-bookworm-slim

LABEL description="mini-architect-bench V6 - Codex CLI agent sandbox"
LABEL version="6.0"

ENV DEBIAN_FRONTEND=noninteractive

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

# clang==14.0.0 is needed by validate.py (bit-width AST audit)
RUN pip3 install --no-cache-dir --break-system-packages clang==14.0.0

# Bake Codex CLI at the pinned version. Kept in sync with
# runtimes/codex/info.yaml (runtime_version) and challenge.yaml.
# 0.137.0: gpt-5.5 needs a recent CLI (0.106.0 rejects it); 0.137.0 is
# confirmed working with gpt-5.5 + streamable-http MCP.
RUN npm install -g @openai/codex@0.137.0 \
    && npm cache clean --force

# Smoke: fail the build if the binary or version drifts
RUN codex --version

RUN mkdir -p /workspace /traces/decoded /api

# node:22-bookworm-slim ships a "node" user at UID 1000; remove it first,
# then create our "agent" user at the same UID so chown/perms inside the
# container behave like the legacy ubuntu-based image expected.
RUN userdel -r node 2>/dev/null || true \
    && useradd -m -u 1000 -s /bin/bash agent \
    && mkdir -p /home/agent/.codex \
    && chown -R agent:agent /workspace /traces /api /home/agent

# Suppress podman banner that pollutes tool outputs
RUN mkdir -p /etc/containers && touch /etc/containers/nodocker

WORKDIR /workspace

CMD ["sleep", "infinity"]
