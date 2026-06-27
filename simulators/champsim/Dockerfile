# mini-architect-bench V6 — ChampSim Simulator Image
#
# EVERYTHING needed for evaluation is baked in. No runtime injection
# of scripts — only challenge-specific config.json and agent code
# are injected per-session.
#
# Build (from project root):
#   podman build -t localhost/archbench-champsim:v6 \
#       -f simulators/champsim/Dockerfile .
#
FROM ubuntu:22.04

LABEL maintainer="mini-architect-bench Team"
LABEL description="mini-architect-bench V6 - Self-contained ChampSim simulator"
LABEL version="6.0"

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=America/New_York

# Install build dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    git \
    wget \
    curl \
    zip \
    unzip \
    tar \
    xz-utils \
    ninja-build \
    python3 \
    python3-pip \
    python3-dev \
    pkg-config \
    zlib1g-dev \
    liblzma-dev \
    findutils \
    && rm -rf /var/lib/apt/lists/*

# PyYAML for prepare_challenge.py; swe-rex for SWE-Agent compatibility
RUN pip3 install --no-cache-dir pyyaml swe-rex

WORKDIR /work

# ============================================
# Build ChampSim
# ============================================
ENV VCPKG_FORCE_SYSTEM_BINARIES=1

RUN git clone https://github.com/ChampSim/ChampSim.git /work/runtimes/champsim \
    && cd /work/runtimes/champsim \
    && git submodule update --init \
    && vcpkg/bootstrap-vcpkg.sh \
    && vcpkg/vcpkg install

# Create default ChampSim config and do initial build
RUN cd /work/runtimes/champsim \
    && echo '{"executable_name":"champsim","block_size":64,"page_size":4096,"heartbeat_frequency":10000000,"num_cores":1,"ooo_cpu":[{"frequency":4000,"ifetch_buffer_size":64,"decode_buffer_size":32,"dispatch_buffer_size":32,"scheduler_size":128,"rob_size":352,"lq_size":128,"sq_size":72,"fetch_width":6,"decode_width":6,"dispatch_width":6,"execute_width":4,"retire_width":5,"mispredict_penalty":1,"decode_latency":1,"dispatch_latency":1,"schedule_latency":0,"execute_latency":0,"branch_predictor":"hashed_perceptron","btb":"basic_btb","DIB":{"sets":32,"ways":8}}],"caches":[{"name":"cpu0_L1I","frequency":4000,"sets":64,"ways":8,"rq_size":64,"wq_size":64,"pq_size":32,"mshr_size":8,"latency":4,"fill_latency":1,"max_tag_check":2,"max_fill":2,"prefetch_as_load":false,"virtual_prefetch":true,"wq_check_full_addr":true,"prefetch_activate":"LOAD,PREFETCH","lower_level":"cpu0_L2C","prefetcher":"no"},{"name":"cpu0_L1D","frequency":4000,"sets":64,"ways":12,"rq_size":64,"wq_size":64,"pq_size":8,"mshr_size":16,"latency":5,"fill_latency":1,"max_tag_check":2,"max_fill":2,"prefetch_as_load":false,"wq_check_full_addr":true,"prefetch_activate":"LOAD,PREFETCH","lower_level":"cpu0_L2C","prefetcher":"next_line"},{"name":"cpu0_L2C","frequency":4000,"sets":1024,"ways":8,"rq_size":32,"wq_size":32,"pq_size":16,"mshr_size":32,"latency":10,"fill_latency":1,"max_tag_check":1,"max_fill":1,"prefetch_as_load":false,"wq_check_full_addr":false,"prefetch_activate":"LOAD,PREFETCH","lower_level":"LLC","prefetcher":"no","replacement":"lru"},{"name":"LLC","frequency":4000,"sets":2048,"ways":16,"rq_size":32,"wq_size":32,"pq_size":32,"mshr_size":64,"latency":20,"fill_latency":1,"max_tag_check":1,"max_fill":1,"prefetch_as_load":false,"wq_check_full_addr":false,"prefetch_activate":"LOAD,PREFETCH","lower_level":"DRAM","prefetcher":"no","replacement":"lru"}],"physical_memory":{"frequency":3200,"channels":1,"ranks":1,"banks":8,"rows":65536,"columns":128,"channel_width":8,"wq_size":64,"rq_size":64,"tRP":12.5,"tRCD":12.5,"tCAS":12.5,"turn_around_time":7.5}}' > champsim_config.json \
    && ./config.sh champsim_config.json \
    && make -j$(nproc)

# ============================================
# COPY trace files into image
# ============================================
RUN mkdir -p /work/workload_pools/champsim
COPY workload_pools/champsim/ /work/workload_pools/champsim/

# ============================================
# Decode ALL binary traces into human-readable text
# ============================================
COPY simulators/champsim/decode_traces.py /tmp/decode_traces.py
RUN python3 /tmp/decode_traces.py \
        /work/workload_pools/champsim \
        /work/workload_pools/champsim/decoded \
        --max-records 200000 \
    && echo "=== Decoded traces ===" \
    && ls -lh /work/workload_pools/champsim/decoded/ \
    && rm /tmp/decode_traces.py

# Verify every .champsimtrace.xz has a corresponding decoded .trace.txt
RUN echo "=== Verifying decoded traces ===" \
    && MISSING=0 \
    && for trace in /work/workload_pools/champsim/*.champsimtrace.xz; do \
        base=$(basename "$trace" .champsimtrace.xz); \
        decoded="/work/workload_pools/champsim/decoded/${base}.trace.txt"; \
        if [ ! -f "$decoded" ]; then \
            echo "MISSING decoded trace: $decoded"; \
            MISSING=$((MISSING+1)); \
        fi; \
    done \
    && if [ $MISSING -gt 0 ]; then echo "FATAL: $MISSING traces not decoded"; exit 1; fi \
    && echo "All traces decoded OK"

# NOTE (open-ended rewrite): legacy image baked challenges/champsim/ into
# /work/challenges/. The new connector contract doesn't read these —
# challenges live on the host and are injected via MCP submit. We keep
# the line removed to avoid a stale copy of challenge code inside the
# image. If you really need it for a legacy use case, restore the COPY.

# ============================================
# BAKE scripts into image — single source of truth.
# verify.sh is the consolidated invariant checker, called by
# Dockerfile (here), ChampSimPlugin.verify_simulator (runtime), and
# `archbench verify-all` (operator). No duplicate inline Python.
# ============================================
COPY simulators/champsim/build_and_run.sh /work/build_and_run.sh
COPY simulators/champsim/cleanup.sh /work/cleanup.sh
COPY simulators/champsim/verify.sh /work/verify.sh
RUN chmod +x /work/build_and_run.sh /work/cleanup.sh /work/verify.sh

# ============================================
# COPY entrypoint
# ============================================
COPY simulators/champsim/entrypoint.sh /work/entrypoint.sh
RUN chmod +x /work/entrypoint.sh

# ============================================
# Create submission directory
# ============================================
RUN mkdir -p /work/submission

# ============================================
# Environment
# ============================================
ENV WORK_HOME=/work
ENV CHAMPSIM_PATH=/work/runtimes/champsim
ENV PATH="/work/runtimes/champsim/bin:${PATH}"

RUN ln -sf /usr/bin/python3 /usr/bin/python

# Suppress "Emulate Docker CLI using podman" banner that pollutes tool outputs
RUN mkdir -p /etc/containers && touch /etc/containers/nodocker

# ============================================
# Verify: single call to the consolidated verifier
# ============================================
RUN /work/verify.sh

WORKDIR /workspace

CMD ["sleep", "infinity"]
