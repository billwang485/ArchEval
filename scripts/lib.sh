#!/bin/bash
# scripts/lib.sh — typed-outcome discipline for the bash orchestration layer
# (lessons §26.3: build jobs printed IMGBUILD_OK while a sub-step had failed,
# and run_one.sh once swallowed exit codes entirely — §25). Source this and:
#
#   . scripts/lib.sh
#   arch_step "build image"   podman build ...
#   arch_step "smoke"         podman run ...
#   arch_verdict "MYJOB_OK"        # prints MYJOB_OK ONLY if every step rc==0,
#                                  # else MYJOB_FAIL + the failed steps; exits 1.
#
# Every step is recorded to $ARCH_STEPS (jsonl: name, rc, secs) so the job's
# verdict is DERIVED from the manifest, not from hand-written echos.
ARCH_STEPS="${ARCH_STEPS:-${TMPDIR:-/tmp}/arch_steps_$$.jsonl}"
: > "$ARCH_STEPS"

arch_step() {
  local name="$1"; shift
  local t0=$SECONDS
  "$@"
  local rc=$?
  printf '{"step": "%s", "rc": %d, "secs": %d}\n' "$name" "$rc" "$((SECONDS-t0))" >> "$ARCH_STEPS"
  if [ "$rc" -eq 0 ]; then echo "[step OK   rc=0  $((SECONDS-t0))s] $name"
  else echo "[step FAIL rc=$rc $((SECONDS-t0))s] $name" >&2; fi
  return $rc
}

arch_verdict() {
  local okmsg="${1:-JOB_OK}"
  local fails
  # grep -c prints 0 AND exits 1 on no-match; `|| echo 0` would double-print.
  fails=$(grep -c '"rc": [^0]' "$ARCH_STEPS" 2>/dev/null); fails=${fails:-0}
  echo "---- step manifest ----"; cat "$ARCH_STEPS"
  if [ "${fails:-0}" -eq 0 ]; then echo "$okmsg"; return 0
  else echo "${okmsg%_OK}_FAIL (${fails} failed steps)" >&2; exit 1; fi
}

# arch_sh <name> <shell-string>: arch_step for PIPELINED commands — runs under
# bash with pipefail so `cmd | tail` can't eat the rc (the hello-baseline
# false-OK lesson: a pipe inside sh -c reports the LAST command's rc).
arch_sh() { local name="$1"; shift; arch_step "$name" bash -o pipefail -c "$*"; }
