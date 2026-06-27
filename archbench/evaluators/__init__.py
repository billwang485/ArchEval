"""Post-session evaluator registry.

Evaluators are declared by a challenge's ``evaluations:`` block in
``challenge.yaml`` (loaded into :attr:`archbench.core.challenge.Challenge.evaluations`).
Each entry has an ``evaluator: <name>`` key; :func:`get_evaluator`
looks that name up in this registry.

Top-level evaluator implementations live in ``evaluators/<name>/`` —
one directory per evaluator, with an ``info.yaml`` (schema doc) and an
``evaluator.py`` (the class). The registry imports them lazily so
adding a new evaluator only requires dropping the directory in and
re-registering here.
"""

from __future__ import annotations

import logging
from typing import Type

from archbench.evaluators.base import BaseEvaluator, judge

log = logging.getLogger("archbench.evaluators")


def _registry() -> dict[str, Type[BaseEvaluator]]:
    """Lazy-imported map name → BaseEvaluator subclass.

    Imports happen on first call so that evaluators with optional deps
    (e.g., an anthropic-SDK-using judge) don't load when not needed.
    """
    # Local imports — top-level `evaluators/` package on PYTHONPATH (see
    # pyproject.toml's setuptools.packages.find).
    from evaluators.simulator_metric.evaluator import SimulatorMetricEvaluator
    from evaluators.deliverable_files.evaluator import DeliverableFilesEvaluator
    from evaluators.trajectory_audit.evaluator import TrajectoryAuditEvaluator
    from evaluators.offline_sim_calibration.evaluator import (
        OfflineSimCalibrationEvaluator,
    )
    from evaluators.objective_definition_quality.evaluator import (
        ObjectiveDefinitionQualityEvaluator,
    )
    from evaluators.cross_sim_discrepancy.evaluator import (
        CrossSimDiscrepancyEvaluator,
    )
    from evaluators.gibbon_surrogate.evaluator import (
        GibbonSurrogateEvaluator,
    )
    from evaluators.prediction_calibration.evaluator import (
        PredictionCalibrationEvaluator,
    )
    from evaluators.iteration_quality.evaluator import IterationQualityEvaluator
    from evaluators.tool_use_audit.evaluator import ToolUseAuditEvaluator

    return {
        SimulatorMetricEvaluator.name: SimulatorMetricEvaluator,
        DeliverableFilesEvaluator.name: DeliverableFilesEvaluator,
        TrajectoryAuditEvaluator.name: TrajectoryAuditEvaluator,
        OfflineSimCalibrationEvaluator.name: OfflineSimCalibrationEvaluator,
        ObjectiveDefinitionQualityEvaluator.name: ObjectiveDefinitionQualityEvaluator,
        CrossSimDiscrepancyEvaluator.name: CrossSimDiscrepancyEvaluator,
        GibbonSurrogateEvaluator.name: GibbonSurrogateEvaluator,
        PredictionCalibrationEvaluator.name: PredictionCalibrationEvaluator,
        IterationQualityEvaluator.name: IterationQualityEvaluator,
        ToolUseAuditEvaluator.name: ToolUseAuditEvaluator,
    }


def get_evaluator(name: str) -> BaseEvaluator:
    """Return an instance of the named evaluator.

    Raises ``KeyError`` (with a list of available names) if the name
    isn't registered. The caller (session.py) catches and logs that
    instead of crashing the whole post-session step.
    """
    reg = _registry()
    if name not in reg:
        raise KeyError(
            f"Unknown evaluator {name!r}. Available: {sorted(reg.keys())}"
        )
    return reg[name]()


__all__ = ["BaseEvaluator", "get_evaluator", "judge"]
