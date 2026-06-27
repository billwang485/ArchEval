"""offline_sim_calibration — score agent's offline Python sim vs ChampSim IPC.

The cache_replacement_fast prompt encourages the agent to write a
pure-Python LLC simulator (under ``tests/`` or as ``sim_test.py``) and
test its replacement-policy idea before the single-shot ChampSim
submit. This evaluator measures *how well calibrated* that offline
predictor turned out to be: do its predicted IPCs track the real
ChampSim numbers from ``submit_outcomes.jsonl``?

Implementation note (Phase A — surrogate-base refactor):
  The algorithm now lives in
  :class:`evaluators._base.surrogate.BaseSurrogateEvaluator`. This
  class is a thin wiring that supplies the ChampSim-specific bits
  (candidate file list, probe shapes, decoded-trace locator, and the
  per-trace IPC reader) from
  :mod:`simulators.champsim.connector.surrogate_probes`. PUBLIC API +
  OUTPUT SCHEMA are unchanged from the prior monolithic version.

Design notes (carried forward from the original):
  - The challenge prompt does NOT specify a function signature. Agents
    write whatever Python they want. We probe four common shapes (see
    info.yaml) and bail with a clear "interface mismatch" reason if
    none fit. We never silently fabricate numbers.
  - Decoded traces (the .trace.txt the agent's sim reads) live inside
    the ChampSim Docker image at /traces/decoded/, not on the host. If
    the host doesn't have them, we can't run the agent's sim and we
    say so. The evaluator's contract is: produce a calibrated number
    when possible, otherwise report what was missing.
  - Per-trace timeouts (5 min default) keep a runaway agent sim from
    hanging the post-session step. Per-trace errors are reported in
    the output JSON; partial results are still useful.

Tier mapping: Tier 2 (Process) — see docs/evaluator_framework.md.
"""

from __future__ import annotations

from typing import Any

from evaluators._base.surrogate import BaseSurrogateEvaluator
from simulators.champsim.connector.surrogate_probes import (
    CHAMPSIM_CANDIDATE_FILES,
    CHAMPSIM_PROBES,
    champsim_find_workloads,
    champsim_read_ground_truth,
)


class OfflineSimCalibrationEvaluator(BaseSurrogateEvaluator):
    """ChampSim-specific wiring of the generic surrogate-calibration base.

    All of ``evaluate()``'s control flow (workspace location, probe
    iteration, per-trace timeout, aggregation) is inherited from
    :class:`BaseSurrogateEvaluator`. This subclass only declares the
    ChampSim bits.
    """

    name = "offline_sim_calibration"
    CANDIDATE_FILES = list(CHAMPSIM_CANDIDATE_FILES)
    PROBES = list(CHAMPSIM_PROBES)

    def find_workloads(
        self,
        challenge: Any,
        workspace,
        workload_names,
    ):
        return champsim_find_workloads(challenge, workspace, workload_names)

    def read_ground_truth(self, results_dir):
        return champsim_read_ground_truth(results_dir)
