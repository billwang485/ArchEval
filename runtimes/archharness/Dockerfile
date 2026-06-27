# archharness runtime (in-house, mode: dev_capable).
#
# mini-architect-bench V6 — ArchHarness Container
#
# In-container Python agent driving local Gemma 4 via vLLM. Parallel to
# agent-codex / agent-gemini but no CLI wrapper — the agent loop is
# Python + httpx, calling the Gemma 4 endpoint directly.
#
# Runtime injections (same as other agents):
#   /workspace/              — starter code, prompt.md, state_card.md
#   /traces/decoded          — decoded workload traces (read-only)
#   /api/                    — simulator interface docs (read-only)
#   /opt/archharness/          — agent code (baked into image)
#
# Build:
#   docker build -t localhost/archbench-agent-archharness:v6 \
#                -f runtimes/archharness/Dockerfile .

FROM ubuntu:22.04

LABEL description="mini-architect-bench V6 - ArchHarness sandbox (local Gemma 4 driver)"
LABEL version="6.0"

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    coreutils \
    gawk \
    grep \
    sed \
    python3 \
    python3-pip \
    libclang-dev \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Bit-width AST audit (matches agent-codex / agent-gemini)
RUN pip3 install --no-cache-dir clang==14.0.0 httpx==0.27.2

RUN mkdir -p /workspace /traces/decoded /api /opt/archharness

# Bake archharness runtime into image. Source is vendored under
# runtimes/archharness/src/ (Phase B). dev_capable runtimes bind-mount
# the same host path over /opt/archharness at runtime via --dev.
COPY runtimes/archharness/src/ /opt/archharness/
RUN chmod -R a+r /opt/archharness && chmod +x /opt/archharness/main.py

RUN useradd -m -u 1000 -s /bin/bash agent \
    && chown -R agent:agent /workspace /traces /api /home/agent

RUN mkdir -p /etc/containers && touch /etc/containers/nodocker

WORKDIR /workspace

CMD ["sleep", "infinity"]
