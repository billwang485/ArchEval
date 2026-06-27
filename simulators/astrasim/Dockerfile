# mini-architect-bench V1 — ASTRA-sim Simulator Image
#
# Analytical network backend (congestion-unaware + congestion-aware).
#
# Build (from project root):
#   podman build -t localhost/archbench-astrasim:v6 -f simulators/astrasim/Dockerfile .
#
FROM ubuntu:22.04

LABEL maintainer="mini-architect-bench Team"
LABEL description="mini-architect-bench V1 - ASTRA-sim distributed AI system simulator"
LABEL version="1.0"

ENV DEBIAN_FRONTEND=noninteractive

# ================================================================
# System dependencies
# ================================================================
RUN apt-get -y update && apt-get -y install \
    coreutils wget vim git \
    gcc g++ clang-format \
    make cmake \
    libboost-dev libboost-program-options-dev \
    openmpi-bin openmpi-doc libopenmpi-dev \
    python3 python3-pip python3-venv \
    graphviz jq \
    && rm -rf /var/lib/apt/lists/*

# Python venv (required for modern pip)
RUN python3 -m venv /opt/venv/astrasim
ENV PATH="/opt/venv/astrasim/bin:$PATH"
RUN pip3 install --upgrade pip && \
    pip3 install numpy sympy graphviz pandas pyyaml

# ================================================================
# Abseil (protobuf dependency)
# ================================================================
ARG ABSL_VER=20240722.0
WORKDIR /opt
RUN wget -q https://github.com/abseil/abseil-cpp/releases/download/${ABSL_VER}/abseil-cpp-${ABSL_VER}.tar.gz && \
    tar -xf abseil-cpp-${ABSL_VER}.tar.gz && \
    rm abseil-cpp-${ABSL_VER}.tar.gz

WORKDIR /opt/abseil-cpp-${ABSL_VER}/build
RUN cmake .. \
    -DCMAKE_CXX_STANDARD=14 \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="/opt/abseil-cpp-${ABSL_VER}/install" && \
    cmake --build . --target install --config Release --parallel $(nproc)
ENV absl_DIR="/opt/abseil-cpp-${ABSL_VER}/install"

# ================================================================
# Protobuf (from source, matching ASTRA-sim requirements)
# ================================================================
ARG PROTOBUF_VER=29.0
WORKDIR /opt
RUN wget -q https://github.com/protocolbuffers/protobuf/releases/download/v${PROTOBUF_VER}/protobuf-${PROTOBUF_VER}.tar.gz && \
    tar -xf protobuf-${PROTOBUF_VER}.tar.gz && \
    rm protobuf-${PROTOBUF_VER}.tar.gz

WORKDIR /opt/protobuf-${PROTOBUF_VER}/build
RUN cmake .. \
    -DCMAKE_CXX_STANDARD=14 \
    -DCMAKE_BUILD_TYPE=Release \
    -Dprotobuf_BUILD_TESTS=OFF \
    -Dprotobuf_ABSL_PROVIDER=package \
    -DCMAKE_INSTALL_PREFIX="/opt/protobuf-${PROTOBUF_VER}/install" && \
    cmake --build . --target install --config Release --parallel $(nproc)
ENV PATH="/opt/protobuf-${PROTOBUF_VER}/install/bin:$PATH"
ENV protobuf_DIR="/opt/protobuf-${PROTOBUF_VER}/install"
RUN pip3 install protobuf==5.${PROTOBUF_VER}
ENV PROTOBUF_FROM_SOURCE=True

# ================================================================
# ASTRA-sim: clone, build, generate workloads
# ================================================================
WORKDIR /work/runtimes
RUN git clone --recurse-submodules https://github.com/astra-sim/astra-sim.git astrasim

WORKDIR /work/runtimes/astrasim
ENV PYTHONPATH="/work/runtimes/astrasim"

# Build analytical backend (both congestion-aware and unaware)
RUN ./build/astra_analytical/build.sh -t all

# Verify binaries exist
RUN test -x build/astra_analytical/build/bin/AstraSim_Analytical_Congestion_Unaware && \
    test -x build/astra_analytical/build/bin/AstraSim_Analytical_Congestion_Aware && \
    echo "BUILD_VERIFY_OK"

# Verify microbenchmark workloads exist (pre-generated in repo)
# If missing, generate them with correct args
RUN if [ -f examples/workload/microbenchmarks/all_reduce/8npus_1MB/all_reduce.0.et ]; then \
        echo "WORKLOAD_VERIFY_OK: ET files found in repo"; \
    else \
        echo "ET files not found, generating..."; \
        cd examples/workload/microbenchmarks/generator_scripts && \
        for npus in 4 8 16; do \
            python3 all_reduce.py --npus_count $npus --msg_size 1048576 || true; \
            python3 all_gather.py --npus_count $npus --msg_size 1048576 || true; \
            python3 reduce_scatter.py --npus_count $npus --msg_size 1048576 || true; \
            python3 all_to_all.py --npus_count $npus --msg_size 1048576 || true; \
        done; \
    fi && \
    ls examples/workload/microbenchmarks/all_reduce/*/all_reduce.0.et && \
    echo "WORKLOAD_FINAL_CHECK_OK"

# ================================================================
# mini-architect-bench directory structure
# ================================================================

# Workloads (symlink to generated traces)
RUN mkdir -p /work/workloads/astrasim && \
    ln -s /work/runtimes/astrasim/examples/workload /work/workloads/astrasim/workload

# Configs (bake in example configs)
RUN mkdir -p /work/configs/network && \
    mkdir -p /work/configs/remote_memory && \
    mkdir -p /work/configs/system && \
    cp -r /work/runtimes/astrasim/examples/network/analytical/* /work/configs/network/ && \
    cp -r /work/runtimes/astrasim/examples/remote_memory/analytical/* /work/configs/remote_memory/ && \
    cp -r /work/runtimes/astrasim/examples/system/native_collectives/* /work/configs/system/

# Submission directory (agent code lands here)
RUN mkdir -p /work/submission

# Challenges directory (for prepare_challenge.py if needed later)
RUN mkdir -p /work/challenges

# ================================================================
# BAKE scripts into image — single source of truth.
# ================================================================
COPY simulators/astrasim/build_and_run.sh /work/build_and_run.sh
COPY simulators/astrasim/cleanup.sh /work/cleanup.sh
COPY simulators/astrasim/verify.sh /work/verify.sh
RUN chmod +x /work/build_and_run.sh /work/cleanup.sh /work/verify.sh

# ============================================
# COPY entrypoint
# ============================================
COPY simulators/astrasim/entrypoint.sh /work/entrypoint.sh
RUN chmod +x /work/entrypoint.sh

# ============================================
# Environment
# ============================================
ENV WORK_HOME=/work

# Suppress podman banner
RUN mkdir -p /etc/containers && touch /etc/containers/nodocker

# ============================================
# Verify: single call to the consolidated verifier
# ============================================
RUN /work/verify.sh

WORKDIR /work
CMD ["sleep", "infinity"]

# Harness contract: /workspace exists in every sim image (container card invariant).
RUN mkdir -p /workspace
