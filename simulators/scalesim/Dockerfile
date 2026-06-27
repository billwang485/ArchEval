# mini-architect-bench V1 — SCALE-Sim Simulator Image
#
# Build (from project root):
#   podman build -t localhost/archbench-scalesim:v6 \
#       -f simulators/scalesim/Dockerfile .
#
FROM ubuntu:22.04

LABEL maintainer="mini-architect-bench Team"
LABEL description="mini-architect-bench V1 - Self-contained SCALE-Sim simulator"
LABEL version="1.0"

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=America/New_York

# Install Python3 and dependencies
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Python packages needed by SCALE-Sim and the harness
RUN pip3 install --no-cache-dir pyyaml swe-rex numpy pandas tqdm configparser absl-py

WORKDIR /work

# ============================================
# Clone and install SCALE-Sim
# ============================================
RUN git clone https://github.com/scalesim-project/scale-sim-v2.git /work/runtimes/scalesim \
    && cd /work/runtimes/scalesim \
    && pip3 install --no-cache-dir -e .

# ============================================
# Copy workload topology
# ============================================
COPY workload_pools/scalesim/test_small.csv /work/workloads/scalesim/test_small.csv

# ============================================
# BAKE scripts into image — single source of truth.
# verify.sh is the consolidated invariant checker, called by
# Dockerfile (here), ScaleSimPlugin.verify_simulator (runtime), and
# `archbench verify-all` (operator). No duplicate inline Python.
# ============================================
COPY simulators/scalesim/build_and_run.sh /work/build_and_run.sh
COPY simulators/scalesim/cleanup.sh /work/cleanup.sh
COPY simulators/scalesim/verify.sh /work/verify.sh
RUN chmod +x /work/build_and_run.sh /work/cleanup.sh /work/verify.sh

# ============================================
# COPY entrypoint
# ============================================
COPY simulators/scalesim/entrypoint.sh /work/entrypoint.sh
RUN chmod +x /work/entrypoint.sh

# ============================================
# Create submission directory
# ============================================
RUN mkdir -p /work/submission

# ============================================
# Environment
# ============================================
ENV WORK_HOME=/work
ENV SCALESIM_PATH=/work/runtimes/scalesim
ENV PYTHONPATH="/work/runtimes/scalesim:${PYTHONPATH:-}"

RUN ln -sf /usr/bin/python3 /usr/bin/python

# Suppress "Emulate Docker CLI using podman" banner that pollutes tool outputs
RUN mkdir -p /etc/containers && touch /etc/containers/nodocker

# ============================================
# Verify: single call to the consolidated verifier
# ============================================
RUN /work/verify.sh

WORKDIR /workspace

CMD ["sleep", "infinity"]
