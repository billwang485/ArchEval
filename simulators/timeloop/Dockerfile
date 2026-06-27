# mini-architect-bench V1 — Timeloop DNN Accelerator Simulator Image
#
# Uses the official Accelergy-Timeloop infrastructure image which
# includes pre-built timeloop-mapper, timeloop-model, accelergy,
# and all required libraries (NTL, Barvinok, etc.)
#
# Build (from project root):
#   podman build -t localhost/archbench-timeloop:v6 \
#       -f simulators/timeloop/Dockerfile .
#
FROM timeloopaccelergy/accelergy-timeloop-infrastructure:latest

LABEL maintainer="mini-architect-bench Team"
LABEL description="mini-architect-bench V1 - Self-contained Timeloop simulator"
LABEL version="1.0"

USER root

ENV DEBIAN_FRONTEND=noninteractive

# Install pyyaml and timeloopfe (Python frontend for v0.4 format)
RUN pip3 install --no-cache-dir pyyaml 2>/dev/null || true
RUN pip3 install --no-cache-dir timeloopfe 2>/dev/null || \
    pip3 install --no-cache-dir 'git+https://github.com/Accelergy-Project/timeloop-accelergy-exercises.git#subdirectory=timeloopfe' 2>/dev/null || true

WORKDIR /work

# ============================================
# Copy compound component definitions from exercises
# These are needed by Accelergy for energy estimation
# ============================================
RUN cp -r /usr/local/src/accelergy/test/tests/exercises/timeloop-accelergy-exercises/workspace/example_designs/example_designs/_components /work/components || true

# ============================================
# Copy workloads
# ============================================
COPY workload_pools/timeloop/ /work/workloads/timeloop/

# ============================================
# BAKE scripts into image — single source of truth.
# verify.sh is the consolidated invariant checker, called by
# Dockerfile (here), TimeloopPlugin.verify_simulator (runtime), and
# `archbench verify-all` (operator). No duplicate inline Python.
# ============================================
COPY simulators/timeloop/build_and_run.sh /work/build_and_run.sh
COPY simulators/timeloop/cleanup.sh /work/cleanup.sh
COPY simulators/timeloop/verify.sh /work/verify.sh
RUN chmod +x /work/build_and_run.sh /work/cleanup.sh /work/verify.sh

# ============================================
# COPY entrypoint
# ============================================
COPY simulators/timeloop/entrypoint.sh /work/entrypoint.sh
RUN chmod +x /work/entrypoint.sh

# ============================================
# Create submission directory
# ============================================
RUN mkdir -p /work/submission

# ============================================
# Environment
# ============================================
ENV WORK_HOME=/work

# Suppress "Emulate Docker CLI using podman" banner that pollutes tool outputs
RUN mkdir -p /etc/containers && touch /etc/containers/nodocker

# ============================================
# Verify: single call to the consolidated verifier
# ============================================
RUN /work/verify.sh

WORKDIR /workspace

# Override the infrastructure image's entrypoint (s6-overlay)
# We just need sleep infinity for the harness sandbox
ENTRYPOINT []
CMD ["sleep", "infinity"]
