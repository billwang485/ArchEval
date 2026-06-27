"""objective_definition_quality — Tier-2 LLM judge over agent's objective.md.

Scores the agent's formalization of a fuzzy prose objective on four
axes (operational, defensible, covers_prose, tradeoff_aware), each 0/1,
plus an overall 0-4 score.

See ``evaluator.py`` for the implementation and ``info.yaml`` for the
config + output schema.
"""
from __future__ import annotations

from evaluators.objective_definition_quality.evaluator import (
    ObjectiveDefinitionQualityEvaluator,
)

__all__ = ["ObjectiveDefinitionQualityEvaluator"]
