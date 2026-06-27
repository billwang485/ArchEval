# mini-architect-bench V1 — gem5 Simulator Image
#
# Build (from project root):
#   podman build -t localhost/archbench-gem5:v6 -f simulators/gem5/Dockerfile .
#
# Pin gem5 v24.1.0.1 (compiled Mar 14 2026) for reproducible metrics.
FROM ghcr.io/gem5/devcontainer@sha256:025ca544f062ad149a46592cc9d2784596dadd60cde1834f9447630d31a3c29c

LABEL maintainer="mini-architect-bench Team"
LABEL description="mini-architect-bench V1 - gem5 full-system simulator"
LABEL version="1.0"
LABEL gem5.version="24.1.0.1"

ENV DEBIAN_FRONTEND=noninteractive

# ============================================
# Pre-compile workload binaries
# ============================================
RUN mkdir -p /work/workloads/gem5

# Simple hello world with a compute loop (static linked, no library deps)
RUN printf '#include <stdio.h>\nint main() {\n    int sum = 0;\n    for (int i = 0; i < 1000; i++) sum += i;\n    printf("Hello gem5! Sum = %%d\\n", sum);\n    return 0;\n}\n' > /tmp/hello.c && \
    gcc -static -O2 -o /work/workloads/gem5/hello_static /tmp/hello.c && \
    rm /tmp/hello.c

# ============================================
# BAKE scripts into image — single source of truth.
# verify.sh is the consolidated invariant checker, called by
# Dockerfile (here), GEM5Plugin.verify_simulator (runtime), and
# `archbench verify-all` (operator). No duplicate inline Python.
# ============================================
# Bake the gem5 SOURCE TREE (matching the binary's v24.1.0.1) at /work/gem5 —
# the image originally shipped binary-only, which broke the L2 sim-dev premise
# and the L3 browse guidance (wave-1 imgcheck; lessons §26 context). Shallow
# clone keeps the layer ~1GB.
RUN git clone --depth 1 --branch v24.1.0.1 https://github.com/gem5/gem5.git /work/gem5 && \
    rm -rf /work/gem5/.git && \
    test -f /work/gem5/configs/example/se.py

COPY simulators/gem5/build_and_run.sh /work/build_and_run.sh
COPY simulators/gem5/cleanup.sh /work/cleanup.sh
COPY simulators/gem5/verify.sh /work/verify.sh
RUN chmod +x /work/build_and_run.sh /work/cleanup.sh /work/verify.sh

# ============================================
# COPY entrypoint
# ============================================
COPY simulators/gem5/entrypoint.sh /work/entrypoint.sh
RUN chmod +x /work/entrypoint.sh

# ============================================
# Create submission directory
# ============================================
RUN mkdir -p /work/submission

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

WORKDIR /workspace

CMD ["sleep", "infinity"]
