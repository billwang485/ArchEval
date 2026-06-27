"""gibbon_surrogate -- score the agent's offline EDP surrogate vs real MNSIM.

The L3 (architect) tier of gibbon_codesign gives the agent only the objective +
a single real-simulator submit, and asks it to build its OWN fast surrogate of
MNSIM's hardware-modeling path (predicting EDP for a co-design point) so it can
explore the Gibbon Table-I co-design space offline before committing its few
real submits. This evaluator measures how well-calibrated that offline surrogate
turned out to be: do its predicted EDPs track the REAL MNSIM EDP from
submit_outcomes.jsonl?

It reuses the sim-agnostic algorithm in
:class:`evaluators._base.surrogate.BaseSurrogateEvaluator`. This subclass only
supplies the Gibbon-specific bits:

  * CANDIDATE_FILES -- where the agent's surrogate source lives.
  * PROBES          -- the interface shapes we accept (the surrogate takes a
                       design.json path and returns an EDP, as a float, a dict
                       with an "edp"/"edp_raw" key, a Simulator.run, or a
                       subprocess printing "edp=<float>").
  * find_workloads  -- the single "workload" is the agent's submitted
                       design.json (recovered from the workspace).
  * read_ground_truth -- the REAL EDP (edp_raw) of the agent's final SIM_OK
                       submit, from submit_outcomes.jsonl.

NOTE the base class's output keys say ``trace`` / ``ipc`` for ChampSim back-
compat; here the single "trace" is the design and the "ipc" value is the EDP.
We grade EDP only (the hardware metric that is REAL); the accuracy surrogate is
the challenge's own calibrated oracle, not something the agent is asked to
re-predict. As with all surrogate evaluators we never fabricate numbers: if the
agent shipped no surrogate, or no real EDP is on record, we report ok=false.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from evaluators._base.surrogate import BaseSurrogateEvaluator, ProbeDescriptor

log = logging.getLogger("archbench.evaluators.gibbon_surrogate")


GIBBON_CANDIDATE_FILES = [
    "sim_test.py",
    "tests/simulator.py",
    "tests/sim_test.py",
    "edp_surrogate.py",
    # The L3 stub is STAGED at starter/sim_test.py; an agent that edits
    # it in place (instead of copying to workspace root) must still be
    # found — 2026-06-10 audit caught this path-contract gap.
    "starter/sim_test.py",
]


def _extract_float(result: Any) -> float:
    if isinstance(result, dict):
        for k in ("edp", "edp_raw", "EDP", "metric"):
            if k in result:
                return float(result[k])
        raise TypeError("surrogate dict has no edp/edp_raw key")
    if isinstance(result, (int, float)):
        return float(result)
    raise TypeError("surrogate returned %s, need float/dict" % type(result).__name__)


def _extract_subprocess(streams):
    stdout, stderr = streams
    text = (stdout or "") + "\n" + (stderr or "")
    m = re.search(r"edp(?:_raw)?\s*[=:]\s*([0-9.eE+-]+)", text, re.IGNORECASE)
    if m:
        return float(m.group(1))
    for line in reversed(text.splitlines()):
        tok = line.strip().split()[-1] if line.strip() else ""
        try:
            return float(tok)
        except ValueError:
            continue
    return None


def _subprocess_gating(sim_file: Path) -> bool:
    try:
        src = sim_file.read_text()
    except Exception:
        return False
    return "__main__" in src and "if __name__" in src


GIBBON_PROBES = [
    ProbeDescriptor(name="predict_edp", kind="callable_takes_path",
                    attr_name="predict_edp", output_extract=_extract_float),
    ProbeDescriptor(name="predict_metric", kind="callable_takes_path",
                    attr_name="predict_metric", output_extract=_extract_float),
    ProbeDescriptor(name="simulate", kind="callable_takes_path",
                    attr_name="simulate", output_extract=_extract_float),
    ProbeDescriptor(name="Simulator.run", kind="class_with_run",
                    attr_name="Simulator", output_extract=_extract_float),
    ProbeDescriptor(name="subprocess", kind="subprocess_main",
                    output_extract=_extract_subprocess,
                    gating=_subprocess_gating),
]


class GibbonSurrogateEvaluator(BaseSurrogateEvaluator):
    """Gibbon-specific wiring of the generic surrogate-calibration base."""

    name = "gibbon_surrogate"
    CANDIDATE_FILES = list(GIBBON_CANDIDATE_FILES)
    PROBES = list(GIBBON_PROBES)

    def find_workloads(self, challenge, workspace, workload_names):
        """The single 'workload' is the agent's submitted design.json.

        ``workload_names`` are the keys from read_ground_truth (here: just
        "design"). We map that to the recovered design.json in the workspace.
        """
        out = {}
        for name in workload_names:
            cand = workspace / "design.json"
            if cand.is_file():
                out[name] = cand
        return out

    def read_ground_truth(self, results_dir: Path):
        """Return {"design": real_edp} from the final SIM_OK submit.

        Reads the real (un-sentinelled) MNSIM EDP -- edp_raw -- so the agent's
        surrogate is graded against the honest hardware number even if the
        submitted design happened to trip the accuracy floor / area cap (those
        gates are about winning, not about HW-model fidelity).
        """
        jsonl = results_dir / "submit_outcomes.jsonl"
        if not jsonl.is_file():
            return {}
        rows = []
        try:
            for line in jsonl.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        except Exception as e:
            log.warning("submit_outcomes.jsonl unreadable: %s", e)
            return {}
        sim_ok = [r for r in rows
                  if r.get("outcome") == "sim_ok" and r.get("metric")]
        chosen = sim_ok[-1] if sim_ok else (rows[-1] if rows else None)
        if not chosen or not chosen.get("metric"):
            return {}
        m = chosen["metric"]
        edp = m.get("edp_raw")
        if edp is None:
            edp = m.get("edp")
        if edp is None:
            return {}
        return {"design": float(edp)}
