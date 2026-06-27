# mini-architect-bench V6 — Gemini CLI Agent Container
#
# Off-the-shelf agent (mode: bake_only). The Gemini CLI is pinned at
# v0.38.1 and baked into the image at build-time via npm. Only the
# per-user OAuth credentials are docker-cp'd in at session start.
#
# Runtime injections (per session):
#   /workspace/     — starter code, prompt.md, GEMINI.md
#   /traces/decoded — decoded workload traces (read-only)
#   /api/           — simulator interface docs (read-only)
#   /home/agent/.gemini/ — OAuth credentials + MCP settings
#
# Build:
#   docker build -t localhost/archbench-agent-gemini:v6 -f runtimes/gemini/Dockerfile .

FROM node:22-bookworm-slim

LABEL description="mini-architect-bench V6 - Gemini CLI agent sandbox"
LABEL version="6.0"

ENV DEBIAN_FRONTEND=noninteractive

# Base tools + python tooling required by validate.py bit-width AST audit
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

# Bake Gemini CLI at the pinned version. Kept in sync with
# runtimes/gemini/info.yaml (runtime_version) and challenge.yaml.
RUN npm install -g @google/gemini-cli@0.38.1 \
    && npm cache clean --force

# Smoke: fail the build if the binary or version drifts
RUN gemini --version

# Create directories
RUN mkdir -p /workspace /traces/decoded /api

# Create non-root agent user.
# node:22-bookworm-slim ships a "node" user at UID 1000; remove it first,
# then create our "agent" user at the same UID so chown/perms inside the
# container behave like the legacy ubuntu-based image expected.
RUN userdel -r node 2>/dev/null || true \
    && useradd -m -u 1000 -s /bin/bash agent \
    && mkdir -p /home/agent/.gemini \
    && chown -R agent:agent /workspace /traces /api /home/agent

# Suppress podman banner that pollutes tool outputs
RUN mkdir -p /etc/containers && touch /etc/containers/nodocker

WORKDIR /workspace

CMD ["sleep", "infinity"]
