# mini-architect-bench — MNSIM 2.0 Simulator Image
#
# MNSIM 2.0 (Tsinghua NICS-EFC): behavior-level modeling tool for
# memristor / ReRAM Processing-In-Memory (PIM) neural-network
# accelerators. Estimates NN-on-crossbar accuracy + hardware
# latency / area / power / energy from a hardware-description .ini and
# a NN topology. Repo: https://github.com/thu-nics/MNSIM-2.0
#
# Build (from project root):
#   podman build -t localhost/archbench-mnsim:v6 \
#       -f simulators/mnsim/Dockerfile .
#
# Self-contained + CPU-only + reproducible:
#   * MNSIM pinned to commit ca39ccb (2024-11-27).
#   * Torch/torchvision CPU wheels, pinned. No CUDA required — MNSIM's
#     interface falls back to CPU when torch.cuda.is_available() is False.
#   * The hardware-modeling path (latency/area/power/energy) is purely
#     analytical: it needs only SimConfig.ini + the NN structure, NOT the
#     trained weights and NOT a dataset. The accuracy path (which needs the
#     OneDrive weights .pth + a CIFAR-10 download) is disabled by default
#     so the image is fully offline & reproducible.
#   * The default path runs via the baked `mnsim_hw_run.py` wrapper, which
#     constructs MNSIM's interface with weights_file=None — so NO trained
#     weights (real or synthetic) are needed for hardware modeling. Accuracy
#     simulation (opt-in) loads a real weights file a challenge mounts.
#
FROM python:3.10-slim-bookworm

LABEL maintainer="mini-architect-bench Team"
LABEL description="mini-architect-bench - Self-contained MNSIM 2.0 ReRAM/PIM NN-accelerator simulator"
LABEL version="1.0"
LABEL upstream="https://github.com/thu-nics/MNSIM-2.0"
LABEL upstream_commit="ca39ccb672b631c752dce0fb919e1bec434d8dee"

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=America/New_York

# System deps: git for clone; build-essential not needed (pure-Python +
# prebuilt wheels). awk/grep already in the base image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ============================================
# Python deps — pinned for reproducibility.
#   * torch/torchvision: CPU-only wheels (no CUDA pull).
#   * numpy<2: MNSIM is 2023/24-era; numpy 2.0 broke many APIs.
#   * pandas / tensorboardX / configparser: imported by MNSIM modules.
#   * pyyaml / swe-rex: required by the archbench harness (matches scalesim).
# ============================================
RUN pip3 install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        torch==2.2.2 torchvision==0.17.2 \
    && pip3 install --no-cache-dir \
        numpy==1.26.4 \
        pandas==2.2.2 \
        tensorboardX==2.6.2.2 \
        pyyaml \
        swe-rex

WORKDIR /work

# ============================================
# Clone + pin MNSIM 2.0.
# ============================================
RUN git clone https://github.com/thu-nics/MNSIM-2.0.git /work/runtimes/mnsim \
    && cd /work/runtimes/mnsim \
    && git checkout ca39ccb672b631c752dce0fb919e1bec434d8dee \
    && rm -rf .git MNSIM_manual.pdf

ENV MNSIM_PATH=/work/runtimes/mnsim
ENV PYTHONPATH="/work/runtimes/mnsim:${PYTHONPATH:-}"

# ============================================
# BAKE scripts into image — single source of truth.
# verify.sh / cleanup.sh / build_and_run.sh are called by the Dockerfile
# (bake-time RUN), MNSIMPlugin (runtime), and `archbench verify-all` (operator).
# ============================================
COPY simulators/mnsim/build_and_run.sh /work/build_and_run.sh
COPY simulators/mnsim/cleanup.sh /work/cleanup.sh
COPY simulators/mnsim/verify.sh /work/verify.sh
COPY simulators/mnsim/entrypoint.sh /work/entrypoint.sh
# Hardware-modeling runner (mirrors MNSIM main.py's hw block; weights-free).
COPY simulators/mnsim/mnsim_hw_run.py /work/runtimes/mnsim/mnsim_hw_run.py
RUN chmod +x /work/build_and_run.sh /work/cleanup.sh /work/verify.sh /work/entrypoint.sh

# Bake a copy of the upstream default hardware config as a known-good
# fallback the agent / baseline can start from.
RUN cp /work/runtimes/mnsim/SimConfig.ini /work/default_SimConfig.ini

RUN mkdir -p /work/submission

ENV WORK_HOME=/work

RUN ln -sf /usr/local/bin/python3 /usr/bin/python || ln -sf "$(command -v python3)" /usr/bin/python

# Suppress "Emulate Docker CLI using podman" banner that pollutes tool outputs
RUN mkdir -p /etc/containers && touch /etc/containers/nodocker

# ============================================
# Verify: single call to the consolidated verifier (must print VERIFY_OK).
# ============================================
RUN /work/verify.sh

WORKDIR /workspace

CMD ["sleep", "infinity"]
