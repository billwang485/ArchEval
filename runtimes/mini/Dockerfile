# mini runtime (in-house, mode: dev_capable).
#
# mini-architect-bench V6 — agent-mini container
#
# Mirror of agent-archharness/Dockerfile, but bakes mini_runtime/ instead.
# In-container miniswe-style agent loop driving local Gemma 4 via vLLM,
# talking to a host-side MCP server for simulator-side tools.
#
# Runtime injections (same as archharness):
#   /workspace/      — starter code, prompt.md (host runner mounts/copies in)
#   /traces/decoded  — decoded workload traces (read-only)
#   /api/            — simulator interface docs (read-only)
#   /opt/mini/       — agent code (baked into image)
#
# Build:
#   docker build -t localhost/archbench-agent-mini:v6 \
#                -f runtimes/mini/Dockerfile .

FROM ubuntu:22.04

LABEL description="mini-architect-bench V6 - mini-runtime agent (miniswe + simulator MCP)"
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

# Bit-width AST audit (matches agent-archharness)
RUN pip3 install --no-cache-dir clang==14.0.0 httpx==0.27.2

RUN mkdir -p /workspace /traces/decoded /api /opt/mini

# Bake mini_runtime into the image. Source is vendored under
# runtimes/mini/src/ (Phase B). dev_capable runtimes bind-mount the same
# host path over /opt/mini at runtime via --dev.
COPY runtimes/mini/src/ /opt/mini/
RUN chmod -R a+r /opt/mini && chmod +x /opt/mini/main.py

RUN useradd -m -u 1000 -s /bin/bash agent \
    && chown -R agent:agent /workspace /traces /api /home/agent

RUN mkdir -p /etc/containers && touch /etc/containers/nodocker

WORKDIR /workspace

CMD ["sleep", "infinity"]
