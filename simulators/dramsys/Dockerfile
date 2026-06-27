# mini-architect-bench V6 — DRAMSys Simulator Image
#
# Self-contained: scripts baked in. Challenges + agent code injected
# per-session via the connector (not baked).
#
# Build (from project root):
#   podman build -t localhost/archbench-dramsys:v6 \
#       -f simulators/dramsys/Dockerfile .
#
FROM ubuntu:22.04

LABEL maintainer="mini-architect-bench Team"
LABEL description="mini-architect-bench - Self-contained DRAMSys simulator"
LABEL version="1.0"

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=America/New_York

# Install build dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    wget \
    curl \
    python3 \
    python3-pip \
    python3-dev \
    pkg-config \
    libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*

# Install newer CMake (3.28+) required by DRAMSys
RUN wget -qO- https://github.com/Kitware/CMake/releases/download/v3.28.0/cmake-3.28.0-linux-x86_64.tar.gz \
    | tar xz -C /opt
ENV PATH="/opt/cmake-3.28.0-linux-x86_64/bin:${PATH}"

# PyYAML + swe-rex for SWE-Agent compatibility
RUN pip3 install --no-cache-dir pyyaml swe-rex

WORKDIR /work

# ============================================
# Build DRAMSys
# ============================================
RUN git clone https://github.com/tukl-msd/DRAMSys.git /work/runtimes/dramsys \
    && cd /work/runtimes/dramsys \
    && git submodule update --init --recursive

RUN cd /work/runtimes/dramsys \
    && mkdir -p build && cd build \
    && cmake -DCMAKE_BUILD_TYPE=Release .. \
    && make -j$(nproc)

# ============================================
# Traces: DRAMSys repo ships built-in traces in configs/traces/.
# Challenges reference these via relative paths (e.g., "traces/example.stl").
# ============================================
RUN echo "Available traces:" && ls -la /work/runtimes/dramsys/configs/traces/

# ============================================
# BAKE scripts into image — single source of truth.
# verify.sh is the consolidated invariant checker, called by
# Dockerfile (here), DRAMSysPlugin.verify_simulator (runtime), and
# `archbench verify-all` (operator).
# ============================================
COPY simulators/dramsys/build_and_run.sh /work/build_and_run.sh
COPY simulators/dramsys/cleanup.sh /work/cleanup.sh
COPY simulators/dramsys/verify.sh /work/verify.sh
RUN chmod +x /work/build_and_run.sh /work/cleanup.sh /work/verify.sh

# ============================================
# COPY entrypoint
# ============================================
COPY simulators/dramsys/entrypoint.sh /work/entrypoint.sh
RUN chmod +x /work/entrypoint.sh

# ============================================
# Create submission directory (V6 contract)
# ============================================
RUN mkdir -p /work/submission

# ============================================
# Environment
# ============================================
ENV WORK_HOME=/work
ENV DRAMSYS_PATH=/work/runtimes/dramsys
ENV PATH="/work/runtimes/dramsys/build/bin:${PATH}"

RUN ln -sf /usr/bin/python3 /usr/bin/python

# Suppress "Emulate Docker CLI using podman" banner that pollutes tool outputs
RUN mkdir -p /etc/containers && touch /etc/containers/nodocker

# ============================================
# Verify: single call to the consolidated verifier
# ============================================
RUN /work/verify.sh

WORKDIR /workspace

CMD ["sleep", "infinity"]
