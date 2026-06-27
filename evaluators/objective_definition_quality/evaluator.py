"""objective_definition_quality — Tier-2 evaluator (NEW).

Reads the agent's ``objective.md`` (the file the agent wrote to
formalize a fuzzy prose objective) and runs an LLM judge that scores
the formalization on four binary axes:

  1. **operational** — are the thresholds numeric and measurable?
  2. **defensible** — does the agent justify the choices?
  3. **covers_prose** — every clause of the prose objective addressed?
  4. **tradeoff_aware** — does the agent state the trade-offs accepted?

Returns per-axis verdicts + an overall 0-4 score + a free-form rationale.

The judge is a single call (Haiku-class model is fine — these are
binary verdicts on a short document) producing a structured JSON
response. The output is orthogonal to the trio-of-numbers Tier-3
evaluator on purpose: this challenge wants to *primarily* reward
defensible formalization, regardless of whether the resulting numbers
are good.

See ``info.yaml`` for config + output schema; see
``docs/cnn_codesign_design.md`` for design rationale.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from archbench.evaluators.base import BaseEvaluator, judge

log = logging.getLogger("archbench.evaluators.objective_definition_quality")


_AXIS_NAMES = ("operational", "defensible", "covers_prose", "tradeoff_aware")


# Single-call judge prompt. The judge is told to return JSON with one
# block per axis plus an overall_score + overall_rationale, so the
# downstream renderer can show a per-axis table without a second call.
#
# Why one call instead of four: cost. The objective.md document is
# typically < 2k chars; sending it four times to ask one binary question
# each time is 4x the tokens for no real upside. If a single Haiku judge
# loses too much resolution we can fork into per-axis calls later.
_JUDGE_PROMPT_TEMPLATE = """\
You are evaluating an AI agent's *formalization* of a fuzzy multi-objective
research goal. The prose objective the agent was given is below; the agent
was asked to convert it into operational, numeric criteria with a stated
weighting scheme.

<prose_objective>
{prose}
</prose_objective>

<agent_objective_md>
{objective_md}
</agent_objective_md>

Score the agent's formalization on FOUR binary axes (score 0 or 1):

1. **operational** — Are the thresholds numeric and measurable?
   1 if the agent states concrete numbers (e.g. "top 10% means val_acc ≥
   0.913"; "excellent energy means ≤ 0.5 mJ/inference"); 0 if it leans on
   restatement of the prose or vague language.

2. **defensible** — Does the agent justify their choices?
   1 if there is a stated rationale (battery budgets, frame-rate target,
   percentile of a known distribution, etc.); 0 if numbers are picked
   without explanation.

3. **covers_prose** — Did the agent address EVERY clause of the prose?
   The prose contains (a) accuracy threshold ("top 10%"), (b) energy
   target ("excellent energy efficiency, paramount"), (c) latency target
   ("acceptable latency, real-time nice but not strict"), (d) aggregation
   ("no fixed weighting — you decide"). 1 if all four clauses are
   formalized; 0 if any clause is missing or hand-waved.

4. **tradeoff_aware** — Does the agent explicitly state what they are
   giving up?
   1 if the agent calls out at least one accepted trade-off (e.g. "I am
   giving up the top 1% of accuracy for a smaller model that fits the
   SRAM budget"); 0 if the agent presents the formalization as if no
   trade-offs exist.

Return a SINGLE JSON object with this exact shape:
{{
  "operational":     {{"score": 0 or 1, "rationale": "1-2 sentences"}},
  "defensible":      {{"score": 0 or 1, "rationale": "1-2 sentences"}},
  "covers_prose":    {{"score": 0 or 1, "rationale": "1-2 sentences"}},
  "tradeoff_aware":  {{"score": 0 or 1, "rationale": "1-2 sentences"}},
  "overall_score":   <sum of the four scores, 0 to 4>,
  "overall_rationale": "1-3 sentences summarizing the formalization quality"
}}

Be strict but fair. A score of 4/4 means the formalization is publishable
as-is; 2-3 means usable but rough; 0-1 means the agent did not
substantively engage with the formalization task.
"""


class ObjectiveDefinitionQualityEvaluator(BaseEvaluator):
    name = "objective_definition_quality"

    def evaluate(
        self,
        challenge,
        results_dir: Path,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        objective_file: str = str(config.get("objective_file", "objective.md"))
        prose: str = str(config.get("prose_objective", "")).strip()
        min_chars: int = int(config.get("min_chars", 200))

        if not prose:
            return {
                "ok": False,
                "error": "config.prose_objective is empty; cannot judge",
                "file_present": False,
                "char_count": 0,
                "axes": _empty_axes("no prose_objective in config"),
                "overall_score": None,
                "overall_rationale": "no prose_objective configured",
            }

        workspace = _workspace_dir(results_dir)
        if workspace is None:
            return {
                "ok": False,
                "error": (
                    f"no workspace dir under {results_dir}; "
                    f"checked workspace/, workspace_recovered/"
                ),
                "file_present": False,
                "char_count": 0,
                "axes": _empty_axes("workspace missing"),
                "overall_score": None,
                "overall_rationale": "no workspace dir",
            }

        obj_path = workspace / objective_file
        if not obj_path.is_file():
            return {
                "ok": True,
                "file_present": False,
                "char_count": 0,
                "axes": _empty_axes("objective.md not in workspace"),
                "overall_score": 0,
                "overall_rationale": (
                    f"{objective_file} missing — agent did not produce "
                    "a formalization."
                ),
            }

        try:
            content = obj_path.read_text(errors="replace")
        except Exception as e:
            return {
                "ok": False,
                "error": f"unreadable: {e}",
                "file_present": True,
                "char_count": 0,
                "axes": _empty_axes("unreadable"),
                "overall_score": None,
                "overall_rationale": f"unreadable: {e}",
            }

        char_count = len(content)
        if char_count < min_chars:
            return {
                "ok": True,
                "file_present": True,
                "char_count": char_count,
                "axes": _empty_axes(
                    f"file too short ({char_count} < {min_chars} chars)"
                ),
                "overall_score": 0,
                "overall_rationale": (
                    f"objective.md is {char_count} chars — below the "
                    f"{min_chars}-char floor; not judged. The agent "
                    "did not meaningfully attempt a formalization."
                ),
            }

        prompt = _JUDGE_PROMPT_TEMPLATE.format(
            prose=prose,
            objective_md=content,
        )
        verdict = judge(prompt, max_tokens=2048)

        return _normalize_verdict(verdict, file_present=True, char_count=char_count)


def _workspace_dir(results_dir: Path) -> Optional[Path]:
    for name in ("workspace", "workspace_recovered"):
        p = results_dir / name
        if p.is_dir():
            return p
    return None


def _empty_axes(reason: str) -> dict[str, dict[str, Any]]:
    return {
        axis: {"score": None, "rationale": reason}
        for axis in _AXIS_NAMES
    }


def _normalize_verdict(
    verdict: dict[str, Any],
    *,
    file_present: bool,
    char_count: int,
) -> dict[str, Any]:
    """Coerce the judge's response into the documented output_schema.

    The judge may misbehave (no score, score out of range, axes missing,
    extra keys). We normalize so downstream consumers always see the
    same shape regardless.
    """
    axes: dict[str, dict[str, Any]] = {}
    for axis in _AXIS_NAMES:
        block = verdict.get(axis)
        if not isinstance(block, dict):
            axes[axis] = {
                "score": None,
                "rationale": f"judge omitted '{axis}' axis",
            }
            continue
        score = block.get("score")
        if not isinstance(score, (int, float)) or score not in (0, 1):
            score = None
        else:
            score = int(score)
        axes[axis] = {
            "score": score,
            "rationale": str(block.get("rationale", "")) or "(no rationale)",
        }

    # Overall score: prefer the judge's own number if it's a sane int;
    # fall back to summing axis scores (treating None as 0).
    overall = verdict.get("overall_score")
    if isinstance(overall, (int, float)) and 0 <= overall <= 4:
        overall = int(overall)
    else:
        overall = sum(
            (a["score"] or 0) for a in axes.values()
            if isinstance(a["score"], int)
        )

    overall_rationale = (
        str(verdict.get("overall_rationale", "")).strip()
        or verdict.get("rationale", "")
        or "(no overall rationale)"
    )

    return {
        "ok": True,
        "file_present": file_present,
        "char_count": char_count,
        "axes": axes,
        "overall_score": overall,
        "overall_rationale": overall_rationale,
    }
