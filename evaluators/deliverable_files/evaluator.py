"""deliverable_files — existence + char count + per-file LLM judge.

Reads ``results_dir/workspace/`` (or ``workspace_recovered/`` as a
backward-compat fallback) and, for each filename in
``config.required_files``, reports:

  - ``exists``           — bool
  - ``char_count``       — int
  - ``passes_min_chars`` — char_count >= config.min_chars
  - ``judge_score``      — 0 | 1 | None
  - ``judge_rationale``  — string, always present

If the file is missing OR fails min_chars, the LLM judge is NOT called
(no point spending tokens on an empty file). The deterministic layer's
failure is recorded in ``judge_rationale``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from archbench.evaluators.base import BaseEvaluator, judge

log = logging.getLogger("archbench.evaluators.deliverable_files")


class DeliverableFilesEvaluator(BaseEvaluator):
    name = "deliverable_files"

    def evaluate(
        self,
        challenge,
        results_dir: Path,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        required: list[str] = list(config.get("required_files", []) or [])
        min_chars: int = int(config.get("min_chars", 200))
        llm_judge: str = str(config.get("llm_judge", "0_or_1"))
        prompts: dict[str, str] = dict(config.get("llm_judge_prompts", {}) or {})

        workspace = _workspace_dir(results_dir)

        per_file: dict[str, dict[str, Any]] = {}
        all_ok = True
        for fname in required:
            entry: dict[str, Any] = {
                "exists": False,
                "char_count": 0,
                "passes_min_chars": False,
                "judge_score": None,
                "judge_rationale": "",
            }
            if workspace is None:
                entry["judge_rationale"] = (
                    f"no workspace dir under {results_dir} (checked workspace/, "
                    f"workspace_recovered/)"
                )
                per_file[fname] = entry
                all_ok = False
                continue

            fpath = workspace / fname
            if not fpath.exists() or not fpath.is_file():
                entry["judge_rationale"] = "file missing"
                per_file[fname] = entry
                all_ok = False
                continue
            entry["exists"] = True
            try:
                content = fpath.read_text(errors="replace")
            except Exception as e:
                entry["judge_rationale"] = f"unreadable: {e}"
                per_file[fname] = entry
                all_ok = False
                continue
            entry["char_count"] = len(content)
            entry["passes_min_chars"] = len(content) >= min_chars
            if not entry["passes_min_chars"]:
                entry["judge_rationale"] = (
                    f"too short: {len(content)} < {min_chars} chars; skipping LLM judge"
                )
                per_file[fname] = entry
                all_ok = False
                continue

            # LLM-judge layer
            if llm_judge == "0_or_1":
                prompt = prompts.get(fname)
                if not prompt:
                    entry["judge_rationale"] = "no judge prompt configured for this file"
                else:
                    verdict = judge(prompt, context={"file": fname, "content": content})
                    score = verdict.get("score")
                    if isinstance(score, str) and score.strip() in ("0", "1"):
                        score = int(score.strip())  # "1"-as-string drift
                    if isinstance(score, (int, float)) and score in (0, 1):
                        entry["judge_score"] = int(score)
                    else:
                        # The judge ANSWERED but off-rubric (e.g. 0.5 on a
                        # binary scale). Never invent a verdict — but keep
                        # this distinguishable from file-missing / judge-down
                        # (2026-06-10 sweep: 6 such cases read as outages).
                        entry["judge_score"] = None
                        if verdict.get("rationale"):
                            entry["judge_schema_nonconform"] = True
                            entry["judge_score_raw"] = score
                    entry["judge_rationale"] = verdict.get("rationale") or entry["judge_rationale"]
                    # Carry over any extra fields the prompt asked for.
                    for k, v in verdict.items():
                        if k not in {"score", "rationale"}:
                            entry.setdefault(k, v)
            per_file[fname] = entry

        return {
            "ok": all_ok,
            "per_file": per_file,
            "workspace": str(workspace) if workspace else None,
        }


def _workspace_dir(results_dir: Path) -> Optional[Path]:
    for name in ("workspace", "workspace_recovered"):
        p = results_dir / name
        if p.is_dir():
            return p
    return None
