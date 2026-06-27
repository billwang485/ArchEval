"""BaseSurrogateEvaluator — generic offline-surrogate calibration.

Several challenges encourage the agent to build an offline / in-Python
surrogate model of the target simulator (e.g. a pure-Python LLC sim
that approximates ChampSim IPC, or a lightweight analytic model of a
DRAM controller) and iterate on that before paying for the real
single-shot run. This base class measures *how well-calibrated* that
surrogate ended up being against ground truth recovered from the on-
session real-simulator submit.

The algorithm is sim-agnostic:

  1. Locate the agent's workspace dir (results_dir/workspace/ or
     workspace_recovered/).
  2. Find a candidate surrogate source file using
     ``self.CANDIDATE_FILES`` (sim-specific list).
  3. Pull ground-truth per-workload metric values from the results dir
     via ``self.read_ground_truth(results_dir)`` (sim-specific).
  4. Pull baseline per-workload metric values from the challenge's
     ``baseline.json`` (generic; baseline path comes from challenge).
  5. Locate workload inputs the surrogate needs to consume via
     ``self.find_workloads(challenge_dir, workspace_dir)`` (sim-specific).
  6. Import the surrogate module and probe interfaces declared in
     ``self.PROBES`` (sim-specific list of ProbeDescriptor).
  7. Run the surrogate per-workload under a wall-time cap.
  8. Aggregate: mean absolute error, direction accuracy vs baseline,
     best- and worst-predicted workloads.

The OUTPUT SCHEMA is fixed (see ``evaluate`` below) so callers can
keep parsing it identically across sims:
  ok, reason, sim_file, interface, mean_absolute_error,
  direction_accuracy, best_predicted_trace, worst_predicted_trace,
  per_trace_error, real_per_trace_ipc.

(The keys still say ``trace`` / ``ipc`` for backward compat with the
ChampSim wiring — they are treated as opaque workload-name / metric-
value strings for non-ChampSim subclasses.)
"""

from __future__ import annotations

import importlib.util
import logging
import math
import signal
import sys
from abc import abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from archbench.evaluators.base import BaseEvaluator
from evaluators._base.envelope import EvalClass, envelope

log = logging.getLogger("archbench.evaluators.surrogate")


DEFAULT_PER_TRACE_TIMEOUT_S = 300


# ---------------------------------------------------------------------------
# Probe descriptor — declarative shape of one supported agent interface.
# ---------------------------------------------------------------------------


@dataclass
class ProbeDescriptor:
    """One agent-side interface shape the evaluator knows how to call.

    Subclasses ship a list of these in ``PROBES``; the first one that
    matches the loaded agent module wins.

    Fields:
      name:           Short label written into the output under
                      ``interface`` (e.g. ``predict_ipc``, ``simulate``,
                      ``Simulator.run``, ``subprocess``).
      kind:           One of:
                        * ``callable_takes_path`` — module-level callable
                          taking a path argument.
                        * ``class_with_run``    — class with a default
                          constructor and a ``.run(path)`` method.
                        * ``subprocess_main``    — the script is invoked
                          as ``python <sim_file> <workload_path>`` and
                          stdout/stderr is scraped.
                      Subclasses may extend kinds via ``custom_loader``.
      attr_name:      The attribute name on the module to look for
                      (e.g. ``predict_ipc``, ``simulate``, ``Simulator``).
                      Ignored for ``subprocess_main``.
      output_extract: A callable taking the raw return value (or the
                      ``(stdout, stderr)`` tuple for subprocess) and
                      returning a ``float`` metric value. May raise to
                      signal "no usable output for this workload".
      gating:         Optional callable taking ``sim_file: Path`` and
                      returning True iff this probe should be attempted
                      at all (e.g. subprocess probe only fires if the
                      source has a ``__main__`` guard).
      custom_loader:  Optional callable taking ``(sim_file, module)`` and
                      returning ``(name, run)`` or None. If set, ``kind``
                      and ``attr_name`` are ignored — the loader has full
                      control. (Escape hatch for sim-specific shapes.)
    """

    name: str
    kind: str = "callable_takes_path"
    attr_name: str = ""
    output_extract: Optional[Callable[[Any], float]] = None
    gating: Optional[Callable[[Path], bool]] = None
    custom_loader: Optional[
        Callable[[Path, Any], Optional[tuple[str, Callable[[Path], float]]]]
    ] = None


# ---------------------------------------------------------------------------
# BaseSurrogateEvaluator
# ---------------------------------------------------------------------------


class BaseSurrogateEvaluator(BaseEvaluator):
    """Abstract base for offline-surrogate calibration evaluators.

    Subclass contract:
      * Set class attribute ``name`` (str) — registry key.
      * Set ``CANDIDATE_FILES`` — list of workspace-relative paths to
        search for the agent's surrogate source (first hit wins).
      * Set ``PROBES`` — list of :class:`ProbeDescriptor` describing
        supported interface shapes (probed in order).
      * Implement ``find_workloads`` — locate per-workload input files
        the surrogate needs (sim-specific).
      * Implement ``read_ground_truth`` — pull per-workload real metric
        values from ``results_dir`` (sim-specific).
    """

    name: str = ""
    CANDIDATE_FILES: list[str] = []
    PROBES: list[ProbeDescriptor] = []

    # ------------------------------------------------------------------ hooks

    @abstractmethod
    def find_workloads(
        self,
        challenge: Any,
        workspace: Path,
        workload_names: list[str],
    ) -> dict[str, Path]:
        """Return a mapping ``workload_name -> input_path``.

        Implementations look in sim-specific host locations (e.g. an
        env-var-pointed pool, an in-repo symlink, the challenge dir).
        Missing entries are allowed — the evaluator will record them as
        per-workload errors instead of aborting.
        """
        raise NotImplementedError

    @abstractmethod
    def read_ground_truth(self, results_dir: Path) -> dict[str, float]:
        """Return per-workload real metric values from the run.

        For ChampSim this reads ``submit_outcomes.jsonl`` and falls back
        to ``reeval_result.json`` + ``raw_log_tail`` scraping.
        """
        raise NotImplementedError

    def read_baseline(self, challenge: Any) -> dict[str, float]:
        """Default: read per-workload baseline from ``challenge.baseline.json``.

        Subclasses can override for sims whose baseline metric lives
        elsewhere. The default reads
        ``<challenge_dir>/<baseline_file>``'s ``per_trace`` list,
        expecting ``[{trace, ipc}, …]`` shape (ChampSim convention).
        """
        if challenge is None or not getattr(challenge, "challenge_dir", None):
            return {}
        import json as _json
        ev = getattr(challenge, "eval", None)
        baseline_rel = getattr(ev, "baseline_file", None) or "baseline.json"
        path = (challenge.challenge_dir / baseline_rel)
        if not path.exists():
            return {}
        try:
            data = _json.loads(path.read_text())
        except Exception:
            return {}
        out: dict[str, float] = {}
        for r in data.get("per_trace", []) or []:
            if isinstance(r, dict) and r.get("trace") and "ipc" in r:
                out[r["trace"]] = float(r["ipc"])
        return out

    # -------------------------------------------------------------- algorithm

    def evaluate(
        self,
        challenge: Any,
        results_dir: Path,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        results_dir = Path(results_dir)
        candidates = tuple(config.get("candidate_files") or self.CANDIDATE_FILES)
        timeout_s = int(config.get("per_trace_timeout_s") or DEFAULT_PER_TRACE_TIMEOUT_S)
        trace_subset = config.get("trace_subset") or None

        workspace = _workspace_dir(results_dir)
        if workspace is None:
            return envelope(
                EvalClass.EVALUATOR_ERROR,
                reason=(
                    f"no workspace dir under {results_dir} "
                    "(checked workspace/, workspace_recovered/)"
                ),
            )

        sim_file = _find_sim_file(workspace, candidates)
        if sim_file is not None and _is_untouched_starter_copy(sim_file, workspace, challenge):
            # The staged stub counts as the AGENT's surrogate only if the
            # agent actually edited it. Byte-identical to the challenge's
            # starter copy ⇒ no agent work happened (2026-06-10 audit:
            # adding starter/sim_test.py to the search list must not turn
            # "never wrote a surrogate" into "graded the placeholder").
            return envelope(
                EvalClass.AGENT_MISSING_ARTIFACT,
                reason=(
                    f"only the untouched starter stub present "
                    f"({sim_file.relative_to(workspace)} is byte-identical to the staged copy)"
                ),
                sim_file=str(sim_file.relative_to(workspace)),
            )
        if sim_file is None:
            return envelope(
                EvalClass.AGENT_MISSING_ARTIFACT,
                reason=f"no offline simulator in workspace (looked for {list(candidates)})",
                sim_file=None,
            )

        # Real per-workload values from on-session submit.
        real_metric = self.read_ground_truth(results_dir)
        if not real_metric:
            return envelope(
                EvalClass.NO_GROUND_TRUTH,
                reason=(
                    "no real per-trace metric in submit_outcomes.jsonl "
                    "(no scored submit reached the simulator) — the surrogate "
                    "cannot be calibrated against this run"
                ),
                sim_file=str(sim_file.relative_to(workspace)),
            )

        # Baseline per-workload values — used for direction accuracy.
        baseline_metric = self.read_baseline(challenge)

        # Locate inputs the surrogate consumes.
        workload_paths = self.find_workloads(
            challenge, workspace, list(real_metric.keys()),
        )
        if not workload_paths:
            return envelope(
                EvalClass.EVALUATOR_ERROR,
                reason=(
                    "decoded traces unavailable on host (looked under "
                    "workload_pools/champsim/decoded/); offline simulator "
                    "cannot be re-run for calibration"
                ),
                sim_file=str(sim_file.relative_to(workspace)),
                real_per_trace_ipc=real_metric,
            )

        # Probe the surrogate.
        runner = self._load_surrogate(sim_file)
        if runner is None:
            interface_list = ", ".join(p.name for p in self.PROBES) or "<none>"
            return envelope(
                EvalClass.ARTIFACT_BROKEN,
                reason=(
                    f"interface mismatch: {sim_file.name} doesn't expose any "
                    f"of ({interface_list}). "
                    f"See evaluators/{self.name}/info.yaml."
                ),
                sim_file=str(sim_file.relative_to(workspace)),
                interface="none",
            )

        interface_name, run = runner

        per_trace_error: dict[str, dict[str, Any]] = {}
        for trace_name, real in real_metric.items():
            if trace_subset and trace_name not in trace_subset:
                continue
            trace_path = workload_paths.get(trace_name)
            entry: dict[str, Any] = {
                "predicted": None,
                "real": float(real),
                "abs_error": None,
                "sign_correct": None,
                "error": None,
            }
            if trace_path is None:
                entry["error"] = f"decoded trace not found for {trace_name}"
                per_trace_error[trace_name] = entry
                continue
            try:
                with _alarm(timeout_s):
                    pred = run(trace_path)
            except TimeoutError:
                entry["error"] = f"timeout after {timeout_s}s"
                per_trace_error[trace_name] = entry
                continue
            except Exception as e:
                entry["error"] = f"{type(e).__name__}: {e}"
                per_trace_error[trace_name] = entry
                continue
            if pred is None or not isinstance(pred, (int, float)) or not math.isfinite(pred):
                entry["error"] = f"agent sim returned non-numeric: {pred!r}"
                per_trace_error[trace_name] = entry
                continue
            pred_f = float(pred)
            entry["predicted"] = pred_f
            entry["abs_error"] = abs(pred_f - float(real))
            base = baseline_metric.get(trace_name)
            if base is not None and base > 0:
                pred_better = pred_f > base
                real_better = float(real) > base
                entry["sign_correct"] = (pred_better == real_better)
            per_trace_error[trace_name] = entry

        with_pred = {
            t: e for t, e in per_trace_error.items()
            if e["predicted"] is not None and e["abs_error"] is not None
        }
        if not with_pred:
            return envelope(
                EvalClass.ARTIFACT_BROKEN,
                reason="no traces produced a usable prediction (see per_trace_error for per-trace failures)",
                sim_file=str(sim_file.relative_to(workspace)),
                interface=interface_name,
                per_trace_error=per_trace_error,
            )
        mae = sum(e["abs_error"] for e in with_pred.values()) / len(with_pred)
        signs = [
            e["sign_correct"] for e in with_pred.values()
            if e["sign_correct"] is not None
        ]
        direction_accuracy = (
            sum(1 for s in signs if s) / len(signs) if signs else None
        )
        best = min(with_pred.items(), key=lambda kv: kv[1]["abs_error"])[0]
        worst = max(with_pred.items(), key=lambda kv: kv[1]["abs_error"])[0]

        return envelope(
            EvalClass.SCORED,
            sim_file=str(sim_file.relative_to(workspace)),
            interface=interface_name,
            mean_absolute_error=mae,
            direction_accuracy=direction_accuracy,
            best_predicted_trace=best,
            worst_predicted_trace=worst,
            per_trace_error=per_trace_error,
        )

    # ------------------------------------------------------------- internals

    def _load_surrogate(
        self, sim_file: Path,
    ) -> Optional[tuple[str, Callable[[Path], float]]]:
        """Import the agent's source and probe ``self.PROBES`` in order.

        Returns ``(interface_name, run_callable)`` or None on no match /
        unloadable. Subclasses normally don't override this — they
        customize behavior by declaring different ``PROBES``.
        """
        sim_dir = str(sim_file.parent.resolve())
        added = []
        for p in (sim_dir, str(sim_file.parent.parent.resolve())):
            if p not in sys.path:
                sys.path.insert(0, p)
                added.append(p)
        try:
            spec = importlib.util.spec_from_file_location(
                f"_agent_sim_{abs(hash(str(sim_file)))}",
                str(sim_file),
            )
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)  # type: ignore[attr-defined]
            except SystemExit:
                # Some scripts call sys.exit() on import (rare). Swallow.
                pass
            except Exception as e:
                log.warning(
                    "%s: agent sim import failed: %s", self.name, e,
                )
                return None

            for probe in self.PROBES:
                if probe.gating is not None and not probe.gating(sim_file):
                    continue
                if probe.custom_loader is not None:
                    bound = probe.custom_loader(sim_file, module)
                    if bound is not None:
                        return bound
                    continue
                runner = _bind_probe(probe, sim_file, module)
                if runner is not None:
                    return runner
            return None
        finally:
            for p in added:
                try:
                    sys.path.remove(p)
                except ValueError:
                    pass


# ---------------------------------------------------------------------------
# Probe binding — turn a ProbeDescriptor + loaded module into a runner.
# ---------------------------------------------------------------------------


def _bind_probe(
    probe: ProbeDescriptor,
    sim_file: Path,
    module: Any,
) -> Optional[tuple[str, Callable[[Path], float]]]:
    """Bind one declarative probe shape against a loaded module.

    Returns ``(probe.name, runner)`` if the module exposes the named
    interface, else None.
    """
    if probe.kind == "callable_takes_path":
        fn = getattr(module, probe.attr_name, None)
        if not callable(fn):
            return None
        extract = probe.output_extract

        def run(p: Path, _fn=fn, _extract=extract) -> float:
            r = _fn(str(p))
            if _extract is not None:
                return float(_extract(r))
            return float(r)

        return (probe.name, run)

    if probe.kind == "class_with_run":
        cls = getattr(module, probe.attr_name, None)
        if not isinstance(cls, type):
            return None
        try:
            inst = cls()
        except Exception:
            return None
        if not (hasattr(inst, "run") and callable(inst.run)):
            return None
        extract = probe.output_extract

        def run(p: Path, _inst=inst, _extract=extract) -> float:
            r = _inst.run(str(p))
            if _extract is not None:
                return float(_extract(r))
            return float(r)

        return (probe.name, run)

    if probe.kind == "subprocess_main":
        # Subprocess probes are always available (no module attr needed)
        # but gating still applies (typically: requires __main__ guard).
        import subprocess
        extract = probe.output_extract

        def run(p: Path, _sim=sim_file, _extract=extract) -> Optional[float]:
            try:
                proc = subprocess.run(
                    [sys.executable, str(_sim), str(p)],
                    capture_output=True, text=True,
                    timeout=300, cwd=str(_sim.parent),
                )
            except subprocess.TimeoutExpired:
                raise TimeoutError("subprocess timeout")
            if _extract is not None:
                return float(_extract((proc.stdout or "", proc.stderr or "")))
            text = (proc.stdout or "") + "\n" + (proc.stderr or "")
            return float(text.strip().splitlines()[-1])

        return (probe.name, run)

    log.warning("surrogate: unknown probe kind %r (skipping)", probe.kind)
    return None


# ---------------------------------------------------------------------------
# Workspace + timeout helpers (sim-agnostic).
# ---------------------------------------------------------------------------


def _workspace_dir(results_dir: Path) -> Optional[Path]:
    for name in ("workspace", "workspace_recovered"):
        p = results_dir / name
        if p.is_dir():
            return p
    return None


def _is_untouched_starter_copy(sim_file: Path, workspace: Path, challenge: Any) -> bool:
    """True iff the found candidate is byte-identical to the challenge's
    staged starter copy of the same relative name — i.e. the placeholder
    stub, not agent work. Best-effort: any doubt returns False (grade it)."""
    starter = getattr(challenge, "starter_dir", None)
    if not starter:
        return False
    try:
        rel = sim_file.relative_to(workspace)
    except ValueError:
        return False
    # workspace/starter/<x> maps to <starter_dir>/<x>; workspace/<x> maps too.
    rel_s = str(rel)
    rel_in_starter = rel_s[len("starter/"):] if rel_s.startswith("starter/") else rel_s
    original = Path(starter) / rel_in_starter
    try:
        return original.is_file() and original.read_bytes() == sim_file.read_bytes()
    except OSError:
        return False


def _find_sim_file(workspace: Path, candidates: tuple[str, ...]) -> Optional[Path]:
    for rel in candidates:
        if "*" in rel:  # contract may name a pattern (e.g. tests/test_*.py)
            hits = sorted(q for q in workspace.glob(rel) if q.is_file())
            if hits:
                return hits[0]
            continue
        p = workspace / rel
        if p.is_file():
            return p
    return None


@contextmanager
def _alarm(seconds: int):
    """SIGALRM-based timeout context for the current thread.

    Falls through silently on platforms without SIGALRM (Windows) — the
    subprocess probe carries its own timeout via subprocess.run.
    """
    if not hasattr(signal, "SIGALRM") or seconds <= 0:
        yield
        return

    def _handler(signum, frame):
        raise TimeoutError(f"timeout after {seconds}s")

    prev = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev)
