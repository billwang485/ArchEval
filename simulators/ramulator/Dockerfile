# mini-architect-bench — Ramulator 2.0 Simulator Image
#
# Build (from project root):
#   podman build -t localhost/archbench-ramulator:v6 \
#       -f simulators/ramulator/Dockerfile .
#
FROM ubuntu:22.04

LABEL maintainer="mini-architect-bench Team"
LABEL description="mini-architect-bench - Self-contained Ramulator 2.0 simulator"
LABEL version="1.0"

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=America/New_York

# Install build dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    cmake \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# PyYAML + swe-rex for SWE-Agent compatibility
RUN pip3 install --no-cache-dir pyyaml swe-rex

WORKDIR /work

# ============================================
# Clone and build Ramulator 2.0
# ============================================
RUN git clone https://github.com/CMU-SAFARI/ramulator2.git /work/runtimes/ramulator

# Fix template syntax issue in param.h
RUN cd /work/runtimes/ramulator \
    && sed -i 's/_config\[_name\]\.as<T>()/_config[_name].template as<T>()/g' src/base/param.h || true

RUN cd /work/runtimes/ramulator \
    && mkdir -p build && cd build \
    && cmake .. -DCMAKE_BUILD_TYPE=Release \
    && make -j$(nproc)

# ============================================
# Create archbench subdirectory for custom components
# (IFrontEnd, IControllerPlugin, IRefreshManager, etc.)
# The RAMULATOR_REGISTER_IMPLEMENTATION macro determines component
# type from the template parameter, so directory doesn't matter.
# ============================================
RUN mkdir -p /work/runtimes/ramulator/src/work \
    && printf 'file(GLOB ARCHBENCH_SRCS "${CMAKE_CURRENT_SOURCE_DIR}/*.cpp")\nif(ARCHBENCH_SRCS)\n  target_sources(ramulator PRIVATE ${ARCHBENCH_SRCS})\nendif()\n' \
       > /work/runtimes/ramulator/src/work/CMakeLists.txt \
    && printf '\nadd_subdirectory(src/work)\n' >> /work/runtimes/ramulator/CMakeLists.txt

# ============================================
# Copy traces
# ============================================
COPY workload_pools/ramulator/example_inst.trace /work/workloads/ramulator/example_inst.trace
COPY workload_pools/ramulator/470.lbm /work/workloads/ramulator/470.lbm
COPY workload_pools/ramulator/429.mcf /work/workloads/ramulator/429.mcf
COPY workload_pools/ramulator/462.libquantum /work/workloads/ramulator/462.libquantum
COPY workload_pools/ramulator/444.namd /work/workloads/ramulator/444.namd

# ============================================
# BAKE scripts into image — single source of truth.
# verify.sh is the consolidated invariant checker, called by
# Dockerfile (here), RamulatorPlugin.verify_simulator (runtime), and
# `archbench verify-all` (operator).
# ============================================
COPY simulators/ramulator/build_and_run.sh /work/build_and_run.sh
COPY simulators/ramulator/cleanup.sh /work/cleanup.sh
COPY simulators/ramulator/verify.sh /work/verify.sh
RUN chmod +x /work/build_and_run.sh /work/cleanup.sh /work/verify.sh

# ============================================
# COPY entrypoint
# ============================================
COPY simulators/ramulator/entrypoint.sh /work/entrypoint.sh
RUN chmod +x /work/entrypoint.sh

# ============================================
# Create submission directory (V6 contract)
# ============================================
RUN mkdir -p /work/submission

# ============================================
# Environment
# ============================================
ENV WORK_HOME=/work
ENV RAMULATOR_PATH=/work/runtimes/ramulator
ENV PATH="/work/runtimes/ramulator/build:${PATH}"

RUN ln -sf /usr/bin/python3 /usr/bin/python

# Suppress "Emulate Docker CLI using podman" banner that pollutes tool outputs
RUN mkdir -p /etc/containers && touch /etc/containers/nodocker

# ============================================
# Verify: single call to the consolidated verifier
# ============================================
RUN /work/verify.sh

WORKDIR /workspace

CMD ["sleep", "infinity"]
