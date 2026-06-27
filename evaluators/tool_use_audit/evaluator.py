"""tool_use_audit — did the L2 agent actually drive the real simulator?

The L2 (sim-dev-environment) tier bakes the agent INTO the simulator
image: its Tier-2 "process" question is whether it used that privilege —
read the source, edited, BUILT, and RAN the real simulator inside its
container before spending its one Oracle submit — or just pattern-
matched a config blind. This evaluator computes the arithmetic version
from ``trajectory.canonical.jsonl`` (schema: one ``step`` record per
turn with ``action.kind`` ∈ bash | tool_call and ``action.name`` = the
shell command / tool name). The existing LLM-judge check
(``drove_real_sim`` in trajectory_audit) stays as the qualitative
complement; this one is deterministic and runs everywhere.

What counts as a build / a sim run is sim-specific, so the patterns are
config-declared (challenge.yaml) with generic defaults::

    config:
      build_patterns: ["\\bmake\\b", "cmake", "g\\+\\+", "config\\.sh"]
      run_patterns:   ["bin/champsim", "\\./run", "python3? .*sim"]

Outputs (class == "scored"):
  steps_total / bash_count / file_reads / file_edits / submits
  build_cmds / run_cmds                pattern-matched bash invocations.
  first_submit_step                    None if the agent never submitted.
  ran_real_sim_before_first_submit     the headline boolean.
  built_before_first_submit            ditto for builds.
  edit_build_run_loops                 # of edit→…→build→…→run sequences
                                       (a proxy for real dev iterations).

Classes: evaluator_error (no canonical trajectory found — harness side),
scored otherwise (an agent that never submitted still gets its tool-use
profile; pair with the outcome layer).

Tier mapping: Tier 2 (Process) — see docs/evaluator_framework.md.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from archbench.evaluators.base import BaseEvaluator
from evaluators._base.envelope import EvalClass, envelope

log = logging.getLogger("archbench.evaluators.tool_use_audit")

DEFAULT_BUILD_PATTERNS = (
    r"\bmake\b", r"\bcmake\b", r"\bg\+\+", r"\bgcc\b", r"\bcargo\b",
    r"config\.sh", r"\bbuild\.sh", r"\bscons\b", r"\bninja\b",
)
DEFAULT_RUN_PATTERNS = (
    r"bin/champsim", r"\./build/", r"\brun\.sh", r"simulate\.sh",
    r"\bpython3?\b[^\n]*\b(main|run|sim)\w*\.py",
)
_EDIT_TOOLS = {"file_change", "write_file", "edit_file", "apply_patch"}
_READ_TOOLS = {"read_file", "list_files", "browse_simulator", "read_simulator_file"}
_SUBMIT_TOOLS = {"submit", "submit_and_wait", "submit_async"}


class ToolUseAuditEvaluator(BaseEvaluator):
    name = "tool_use_audit"

    def evaluate(
        self,
        challenge: Any,
        results_dir: Path,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        results_dir = Path(results_dir)
        traj = sorted(results_dir.rglob("trajectory.canonical.jsonl"))
        if not traj:
            return envelope(
                EvalClass.EVALUATOR_ERROR,
                reason=f"no trajectory.canonical.jsonl under {results_dir}",
            )

        build_re = [re.compile(p) for p in (config.get("build_patterns") or DEFAULT_BUILD_PATTERNS)]
        run_re = [re.compile(p) for p in (config.get("run_patterns") or DEFAULT_RUN_PATTERNS)]

        steps = 0
        bash_count = file_reads = file_edits = submits = 0
        build_cmds = run_cmds = 0
        first_submit_step: Optional[int] = None
        builds_before_submit = runs_before_submit = 0
        # edit→build→run loop counting (ordered, non-overlapping)
        loop_state = 0  # 0: want edit, 1: want build, 2: want run
        loops = 0

        for line in traj[0].read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("kind") != "step":
                continue
            steps += 1
            a = d.get("action") or {}
            kind, name = a.get("kind"), str(a.get("name") or "")
            is_build = is_run = False
            if kind == "bash":
                bash_count += 1
                is_build = any(r.search(name) for r in build_re)
                is_run = (not is_build) and any(r.search(name) for r in run_re)
                build_cmds += is_build
                run_cmds += is_run
            elif kind == "tool_call":
                if name in _READ_TOOLS:
                    file_reads += 1
                elif name in _EDIT_TOOLS:
                    file_edits += 1
                elif name in _SUBMIT_TOOLS:
                    submits += 1
                    if first_submit_step is None:
                        first_submit_step = d.get("step", steps)
            if first_submit_step is None:
                builds_before_submit += is_build
                runs_before_submit += is_run
            # loop automaton: edit … build … run = one dev iteration
            if kind == "tool_call" and name in _EDIT_TOOLS:
                loop_state = max(loop_state, 1)
            elif is_build and loop_state >= 1:
                loop_state = 2
            elif is_run and loop_state == 2:
                loops += 1
                loop_state = 0

        return envelope(
            EvalClass.SCORED,
            steps_total=steps,
            bash_count=bash_count,
            file_reads=file_reads,
            file_edits=file_edits,
            submits=submits,
            build_cmds=build_cmds,
            run_cmds=run_cmds,
            first_submit_step=first_submit_step,
            built_before_first_submit=builds_before_submit > 0,
            ran_real_sim_before_first_submit=runs_before_submit > 0,
            edit_build_run_loops=loops,
        )
