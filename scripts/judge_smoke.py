#!/usr/bin/env python3.11
"""judge_smoke — pre-flight gate: is the LLM-as-judge backend alive AND
schema-conformant?

Campaign sbatch scripts call this before launching cells (same spirit as
the agent-image temperature assert). One trivial binary judgment is sent
through the production judge() chain; we require a parsed {score: 0|1}.
Exit 0 = judge usable; exit 1 = degraded (campaign should abort or
proceed knowingly with judge-less Tier-2).

    python3.11 scripts/judge_smoke.py            # one attempt
    python3.11 scripts/judge_smoke.py --retries 2
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from archbench.evaluators.base import judge  # noqa: E402

PROMPT = (
    "This is a harness smoke test. The document below contains the word "
    "'apple'. Score 1 if it does, 0 if it does not. "
    'Return JSON {"score": 1 or 0, "rationale": "..."}.\n\n'
    "Document: the quick brown fox ate an apple."
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--retries", type=int, default=1)
    args = ap.parse_args()

    last = None
    for attempt in range(1, args.retries + 1):
        verdict = judge(PROMPT)
        last = verdict
        score = verdict.get("score")
        if score == 1:
            print(f"JUDGE_SMOKE_OK attempt={attempt} score={score}")
            return 0
        if score == 0:
            # Backend alive + schema fine but WRONG answer — flag loudly:
            # a judge that can't find 'apple' will misgrade real rubrics.
            print(f"JUDGE_SMOKE_WRONG_ANSWER attempt={attempt} verdict={verdict}")
            return 1
        print(f"[judge_smoke] attempt {attempt}: degraded/off-schema: {verdict}")
    print(f"JUDGE_SMOKE_FAIL last={last}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
