"""Typed result envelope for post-session evaluators.

Every evaluator report carries a machine-readable ``class`` field so
downstream consumers (failure triage, result figures, release gates)
can separate *what kind of statement* the report is making without
parsing prose ``reason`` strings. The 2026-06-10 campaign audit showed
why: ``ok: false`` conflated "the agent never wrote the artifact"
(capability evidence — a VALID data point) with "the evaluator itself
broke" (infra — must be excluded), and separating them took a manual
read of every report.

The six classes form a closed set:

  =====================  =====================================================
  class                  meaning / how to read it
  =====================  =====================================================
  scored                 Evaluator ran end-to-end; numeric fields are valid.
  agent_missing_artifact The agent never produced the artifact under
                         evaluation (no prediction file, no surrogate, ...).
                         CAPABILITY evidence — report it, don't hide it.
  artifact_broken        The artifact exists but is unusable (unparsable
                         JSON, wrong schema, surrogate crashes/times out).
                         Also capability evidence, distinct from absence.
  no_ground_truth        The RUN offers nothing to grade against: no scored
                         submit reached the real simulator (all failed or
                         sentinel-penalized). Read alongside the outcome
                         layer — usually capability evidence there, but
                         this evaluator abstains rather than guessing.
  evaluator_error        The evaluator itself failed (host misconfiguration,
                         unreadable inputs that should exist, bug). INFRA —
                         exclude the cell from capability claims and fix
                         the harness.
  not_configured         The challenge doesn't wire the inputs this
                         evaluator needs (no baseline, no metric key).
                         AUTHORING signal — fix the challenge.yaml.
  =====================  =====================================================

Usage::

    from evaluators._base.envelope import EvalClass, envelope

    return envelope(EvalClass.AGENT_MISSING_ARTIFACT,
                    reason="no prediction.json in workspace",
                    looked_for=candidates)

``envelope`` keeps the legacy ``ok`` boolean in sync (``ok`` is True
only for ``scored``) so existing readers of ``ok``/``reason`` keep
working unchanged.
"""

from __future__ import annotations

from typing import Any


class EvalClass:
    """Closed set of evaluator-report classes (see module docstring)."""

    SCORED = "scored"
    AGENT_MISSING_ARTIFACT = "agent_missing_artifact"
    ARTIFACT_BROKEN = "artifact_broken"
    NO_GROUND_TRUTH = "no_ground_truth"
    EVALUATOR_ERROR = "evaluator_error"
    NOT_CONFIGURED = "not_configured"

    ALL = (
        SCORED,
        AGENT_MISSING_ARTIFACT,
        ARTIFACT_BROKEN,
        NO_GROUND_TRUTH,
        EVALUATOR_ERROR,
        NOT_CONFIGURED,
    )


def envelope(clazz: str, **fields: Any) -> dict[str, Any]:
    """Build an evaluator report dict with a typed ``class`` field.

    ``ok`` is derived (True iff ``clazz == EvalClass.SCORED``) for
    backward compatibility with pre-envelope readers; passing an
    explicit ``ok`` in ``fields`` is rejected to keep the two in sync.
    """
    if clazz not in EvalClass.ALL:
        raise ValueError(f"unknown eval class {clazz!r}; expected one of {EvalClass.ALL}")
    if "ok" in fields:
        raise ValueError("'ok' is derived from class — don't pass it explicitly")
    return {"ok": clazz == EvalClass.SCORED, "class": clazz, **fields}
