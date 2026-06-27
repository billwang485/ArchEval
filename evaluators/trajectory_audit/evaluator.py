"""trajectory_audit — LLM-judge checks over the CANONICAL trajectory.

Each declared sub-check (``config.checks.<name>``) carries a prompt. We build a
compact context — the agent's tool-use sequence + a workspace peek — and ask the
judge to return JSON, stored under ``checks.<name>``.

This evaluator reads ONLY ``trajectory.canonical.jsonl`` — the one schema written
at RECORD TIME by each runtime's own adapter (see archbench/core/trajectory.py).
It does NOT auto-detect native runtime formats: that normalization happens once,
upstream (the per-agent anti-corruption layer), so this evaluator stays trivial
and runtime-agnostic. (This deleted the old ~250-line dual/triple-schema parser
zoo — the mess lived here precisely because there was no canonical form.) If a
run has no canonical trajectory (its runtime lacks a to_canonical_trajectory
adapter), the audit says so plainly instead of guessing.

Context handed to judge prompts:
   summary = {
     "bash_calls": int, "check_storage_calls": int, "submit_calls": int,
     "other_tool_calls": {name: count, ...},
     "total_assistant_turns": int, "text_excerpts_head": [str, ...],
   }
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

from archbench.evaluators.base import BaseEvaluator, judge

log = logging.getLogger("archbench.evaluators.trajectory_audit")

# Hard cap on workspace content shipped to the judge per file (token budget).
WORKSPACE_PEEK_BYTES = 3000


class TrajectoryAuditEvaluator(BaseEvaluator):
    name = "trajectory_audit"

    def evaluate(
        self,
        challenge,
        results_dir: Path,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        canon_path = results_dir / "trajectory.canonical.jsonl"
        llm_judge = str(config.get("llm_judge", "0_or_1"))
        checks_cfg: dict[str, dict[str, Any]] = dict(config.get("checks", {}) or {})

        if not canon_path.exists():
            return {
                "ok": False,
                "error": (f"no trajectory.canonical.jsonl at {canon_path} — the "
                          f"runtime needs a to_canonical_trajectory adapter "
                          f"(archbench/core/trajectory.py)"),
                "checks": {n: {"score": None, "rationale": "no canonical trajectory"}
                           for n in checks_cfg},
            }

        tool_uses, summary = _parse_canonical(canon_path)
        workspace_peek = _workspace_peek(results_dir)

        results: dict[str, dict[str, Any]] = {}
        for name, cfg in checks_cfg.items():
            prompt = (cfg or {}).get("prompt") if isinstance(cfg, dict) else None
            if not prompt:
                results[name] = {"score": None, "rationale": "no prompt configured for this check"}
                continue
            if llm_judge == "none":
                results[name] = {"score": None, "rationale": "llm_judge disabled by config"}
                continue
            context = {
                "trajectory_summary": summary,
                "tool_uses": tool_uses,
                "workspace_files": workspace_peek,
            }
            verdict = judge(prompt, context=context)
            verdict.setdefault("rationale", "")
            results[name] = verdict

        return {"ok": True, "trajectory_summary": summary, "checks": results}


def _parse_canonical(canon_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """trajectory.canonical.jsonl -> (tool_uses, summary). Trivial by design: the
    hard per-runtime normalization already happened at record time."""
    tool_uses: list[dict[str, Any]] = []
    counter: Counter[str] = Counter()
    bash_calls = check_storage = submit_calls = 0
    text_excerpts: list[str] = []
    n_steps = 0

    for ln in canon_path.read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        if rec.get("kind") != "step":
            continue
        n_steps += 1
        a = rec.get("action") or {}
        kind = a.get("kind")
        name = a.get("name") or ""
        think = (rec.get("thinking") or {}).get("text") or ""
        if think and len(text_excerpts) < 6:
            text_excerpts.append(think[:480])

        if kind == "bash":
            bash_calls += 1
            counter["bash"] += 1
            if "check_storage" in name:
                check_storage += 1
            tool_uses.append({"tool": "bash", "input": name[:500], "turn": rec.get("step")})
        elif kind == "submit":
            submit_calls += 1
            counter["submit"] += 1
            tool_uses.append({"tool": "submit",
                              "input": _summarize_tool_input("submit", a.get("args")),
                              "turn": rec.get("step")})
        elif kind == "tool_call":
            counter[name] += 1
            tool_uses.append({"tool": name,
                              "input": _summarize_tool_input(name, a.get("args")),
                              "turn": rec.get("step")})

    other = {k: v for k, v in counter.items() if k not in ("bash", "submit")}
    summary = {
        "bash_calls": bash_calls,
        "check_storage_calls": check_storage,
        "submit_calls": submit_calls,
        "other_tool_calls": other,
        "total_assistant_turns": n_steps,
        "text_excerpts_head": text_excerpts,
    }
    return tool_uses, summary


def _summarize_tool_input(tool: str, inp: Any) -> str:
    """Compact a tool's input dict for the judge context."""
    if not isinstance(inp, dict):
        return str(inp)[:500]
    for key in ("command", "file_path", "path", "url", "pattern", "query", "prompt"):
        if key in inp:
            v = inp[key]
            if isinstance(v, str):
                return v[:500]
    return json.dumps(inp, default=str)[:500]


def _workspace_peek(results_dir: Path) -> dict[str, str]:
    """Return {filename: first WORKSPACE_PEEK_BYTES of content}."""
    out: dict[str, str] = {}
    for ws_name in ("workspace", "workspace_recovered"):
        ws = results_dir / ws_name
        if not ws.is_dir():
            continue
        for f in sorted(ws.rglob("*")):
            if not f.is_file():
                continue
            if len(out) >= 30:
                break
            try:
                rel = str(f.relative_to(ws))
            except ValueError:
                rel = f.name
            try:
                txt = f.read_text(errors="replace")[:WORKSPACE_PEEK_BYTES]
            except Exception:
                continue
            out[rel] = txt
        break
    return out
