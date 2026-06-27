"""[concept: VERIFY] archbench doctor — write-time pair-consistency checks.

The wave-1 post-mortem (lessons §25/§26) found that every burned hour happened
in a gap where a DECLARATION had no gate: prompts described environments that
didn't exist, cards asserted paths the image never had, starters drifted from
the baselines stamped on them, an unfinished challenge (baseline.json.todo)
sat in a campaign list. The runtime gates (§1.7 provenance, card-on-load)
caught what they covered — doctor closes the rest by verifying every claim
PAIR at write/review time, repo-side, no containers needed:

  E1  baseline.json exists and is real (not *.todo)        challenge ↔ runnable
  E2  provenance block complete (5 fields)                 baseline ↔ §1.7
  E3  starter hash == baseline.starter_sha256 (full only)  starter ↔ baseline
  E4  traces resolvable + hash == trace_sha256             workload ↔ baseline
  E5  rubric_mapping ⊆ running evaluators (no phantom)     rubric ↔ evaluators
  E6  workspace_setup host_paths exist on disk             manifest ↔ repo
  E7  tools invoked in prompt ⊆ tier's tool surface        prompt ↔ tools
  E8  baseline primary metric is real (not null/inf)       baseline ↔ E1 freshness
  E9  contract-evaluator artifacts disclosed in prompt     prompt ↔ evaluators
  W1  concrete container paths in prompt are stageable     prompt ↔ environment
  W2  container card exists for the sim image              image ↔ card
  W3  prompt over length budget (>600 words)               style
  W4  llm_judge evaluators wired (judge env needed at run) runtime note
  W5  contract evaluator wired w/o explicit artifact list  contract single-source
  W7  provenance source field set (paper|community)         challenge ↔ source

Challenges whose yaml carries a "🔴 BLOCKED" marker are reported as SKIPPED —
they are excluded from campaigns by policy and must not fail the gate.
Runtime-only facts (live image digests, endpoint health) stay with the session
gates; doctor is the repo-internal half of the contract.
"""
from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from archbench.core.challenge import load_challenge
from archbench.core.provenance import (
    starter_dir_sha256,
    trace_files_sha256,
    resolve_trace_path,
)
from archbench.core.container_card import card_path_for, sim_source_root

# tools that exist in the connector universe; E7 flags an INVOCATION (`name(`)
# of a tool outside the tier's surface. Bare mentions ("there is NO
# browse_simulator tool") are deliberately not flagged.
_KNOWN_TOOLS = (
    "browse_simulator", "read_simulator_file", "submit_and_wait",
    "submit", "check_submission", "session_end",
)
_DEFAULT_TOOLS = set(_KNOWN_TOOLS)


@dataclass
class Finding:
    level: str   # ERROR | WARN
    code: str
    msg: str


@dataclass
class Report:
    challenge: str
    status: str = "OK"           # OK | FAIL | SKIPPED
    findings: list[Finding] = field(default_factory=list)

    def err(self, code: str, msg: str) -> None:
        self.findings.append(Finding("ERROR", code, msg))
        self.status = "FAIL"

    def warn(self, code: str, msg: str) -> None:
        self.findings.append(Finding("WARN", code, msg))


def _prompt_fields(raw: dict) -> dict[str, str]:
    return {k: (raw.get(k + "_prompt") or "") for k in
            ("task", "constraints", "scoring", "others")}


def check_challenge(challenge_dir: Path) -> Report:
    challenge_dir = Path(challenge_dir)
    rep = Report(str(challenge_dir))
    yaml_text = (challenge_dir / "challenge.yaml").read_text()
    if "🔴 BLOCKED" in yaml_text or "BLOCKED —" in yaml_text:
        rep.status = "SKIPPED"
        return rep

    ch = load_challenge(challenge_dir)
    raw = getattr(ch, "raw_data", None) or {}
    eval_dir = Path(getattr(ch, "evaluation_dir", challenge_dir / "evaluation"))

    # --- E1/E2: baseline reality + provenance completeness -------------------
    bl_path = eval_dir / "baseline.json"
    baseline: Optional[dict] = None
    if not bl_path.exists():
        todo = list(eval_dir.glob("baseline.json.todo"))
        rep.err("E1", f"baseline.json missing{' (only .todo placeholder)' if todo else ''}: {bl_path}")
    else:
        baseline = json.loads(bl_path.read_text())
        prov = baseline.get("provenance") or {}
        missing = [k for k in ("image_digest", "config_sha256", "starter_sha256",
                               "trace_sha256", "harness_commit") if not prov.get(k)]
        if missing:
            rep.err("E2", f"provenance incomplete, missing {missing}")

    # --- E8: baseline PRIMARY METRIC is real (fresh-but-garbage guard) -------
    # (wave-2 incident: a rebuilt sim image silently broke Accelergy; the
    # baseline restamp "succeeded" with geomean_edp=None and the freshness
    # check waved it through. A baseline whose metric is null/non-finite is
    # not a baseline.)
    if baseline:
        _METRIC_KEYS = ("mpki", "ipc", "edp", "geomean_edp", "agent_scalar",
                        "bandwidth_gbps", "total_cycles", "overhead_pct", "value")
        vals = [baseline.get(k) for k in _METRIC_KEYS if k in baseline]
        if vals and not any(isinstance(v, (int, float)) and v == v and abs(v) != float("inf") and v is not True
                            for v in vals):
            rep.err("E8", f"baseline metric is null/non-finite: "
                          f"{ {k: baseline.get(k) for k in _METRIC_KEYS if k in baseline} }")

    # --- E3: reference ↔ baseline pair (protocol v2) --------------------------
    # The baseline is stamped from the family REFERENCE implementation
    # (challenge.reference_dir; legacy single-tier: the starter itself).
    if baseline:
        sdir = Path(getattr(ch, "reference_dir", None)
                    or getattr(ch, "starter_dir", challenge_dir / "starter"))
        if sdir.is_dir():
            got = starter_dir_sha256(sdir)
            want = (baseline.get("provenance") or {}).get("starter_sha256")
            if want and got != want:
                rep.err("E3", f"starter drift: starter_dir_sha256 {got[:12]}… != baseline {str(want)[:12]}…")

    # --- E4: traces resolvable + hash pair (champsim-style per_trace) --------
    if baseline:
        trace_names = [t["trace"] + ".champsimtrace.xz"
                       for t in baseline.get("per_trace", []) if isinstance(t, dict) and t.get("trace")]
        if trace_names:
            sim_dir = Path(getattr(ch, "simulator_dir", challenge_dir / "simulator"))
            repo = _repo_root(challenge_dir)
            dirs = [sim_dir / "subtraces", challenge_dir / "simulator" / "subtraces",
                    challenge_dir / "subtraces", repo / "workload_pools" / "champsim"]
            try:
                paths = [resolve_trace_path(t, dirs) for t in trace_names]
                got = trace_files_sha256(paths)
                want = (baseline.get("provenance") or {}).get("trace_sha256")
                if want and got != want:
                    rep.err("E4", f"trace hash drift: {got[:12]}… != baseline {str(want)[:12]}…")
            except FileNotFoundError as e:
                rep.err("E4", f"trace unresolvable: {e}")

    # --- E5: phantom rubric ---------------------------------------------------
    running = {e.get("evaluator") for e in (getattr(ch, "evaluations", None) or [])
               if isinstance(e, dict)}
    phantom = [k for k in (getattr(ch, "rubric_mapping", {}) or {}) if k not in running]
    if phantom:
        rep.err("E5", f"rubric_mapping for non-running evaluators: {phantom}")

    # --- E6: workspace manifest host paths ------------------------------------
    repo = _repo_root(challenge_dir)
    for f in ((raw.get("workspace_setup") or {}).get("files") or []):
        hp = f.get("host_path")
        if hp and not (repo / hp).exists():
            rep.err("E6", f"workspace_setup host_path missing on disk: {hp}")

    # --- E7: prompt tool invocations ⊆ tier surface ----------------------------
    tier_tools = raw.get("tier_tools")
    surface = set(tier_tools) if tier_tools else _DEFAULT_TOOLS
    whole = "\n".join(_prompt_fields(raw).values())
    for tool in _KNOWN_TOOLS:
        if re.search(rf"\b{tool}\s*\(", whole) and tool not in surface:
            rep.err("E7", f"prompt invokes `{tool}(` but tier_tools excludes it")

    # --- E9 / W5: artifact-contract evaluators ↔ prompt disclosure -------------
    # (2026-06-10 judge-prompt audit: 8 families graded a surrogate the agent
    # was never told to write — an unfair measurement. Rule: every artifact an
    # evaluator searches for must be named in the agent-facing prompt. E9 fires
    # when the yaml DECLARES the artifact list (single source) but the prompt
    # omits a file; W5 fires when a contract evaluator is wired with NO
    # explicit artifact declaration — it falls back to defaults hardcoded in
    # the evaluator, which the prompt may or may not mention.)
    _CONTRACT_EVALUATORS = {
        "prediction_calibration": "prediction_files",
        "offline_sim_calibration": "candidate_files",
        "gibbon_surrogate": "candidate_files",
    }
    for entry in (getattr(ch, "evaluations", None) or []):
        if not isinstance(entry, dict):
            continue
        ev_name = entry.get("evaluator")
        key = _CONTRACT_EVALUATORS.get(ev_name)
        if key is None:
            continue
        cfg = entry.get("config") or {}
        declared = cfg.get(key)
        if ev_name == "prediction_calibration":
            if not cfg.get("metric_key") or cfg.get("direction") not in ("lower", "higher"):
                rep.err("E9", f"{ev_name} wired without metric_key/direction "
                              "(would yield class=not_configured at runtime)")
            declared = declared or ["prediction.json"]
        if not declared:
            rep.warn("W5", f"{ev_name} wired without explicit {key} in config — "
                           "artifact contract lives in evaluator defaults; declare it "
                           "in the yaml and name the files in the prompt")
            continue
        # The declared list is a set of ACCEPTED locations for one artifact;
        # fairness requires the prompt to name at least one of them.
        bases = {str(f).rsplit("/", 1)[-1] for f in declared}
        if not any(b in whole for b in bases):
            rep.err("E9", f"{ev_name} grades an artifact at {sorted(bases)} but the "
                          "prompt mentions none of them — undisclosed scored "
                          "artifact (unfair measurement)")

    # --- W1: concrete container paths in prompt are stageable -----------------
    staged = {f.get("container_path", "") for f in
              ((raw.get("workspace_setup") or {}).get("files") or [])}
    src_root = sim_source_root(ch.simulator) if getattr(ch, "simulator", None) else ""
    ok_prefixes = tuple(p for p in (
        "/workspace", "/traces", "/api", "/work/workloads", src_root, "/tmp"))
    for m in set(re.findall(r"(?<![\w/])(/(?:workspace|work|api|traces)/[\w./-]+)", whole)):
        if any(ch_ in m for ch_ in "*<>{}"):
            continue
        if m in staged or m.startswith(ok_prefixes):
            continue
        rep.warn("W1", f"prompt path not obviously stageable: {m}")

    # --- W2: card exists for the sim image ------------------------------------
    sim = getattr(ch, "simulator", None)
    if sim:
        try:
            from archbench.image_management import manifest as mf
            tag = mf.fully_qualified("simulators", sim)
            if not card_path_for(tag).exists():
                rep.warn("W2", f"no container card for {tag}")
        except Exception as e:  # manifest gaps are a warning, not a crash
            rep.warn("W2", f"image manifest lookup failed: {e}")

    # --- W3: prompt length ------------------------------------------------------
    words = sum(len(v.split()) for v in _prompt_fields(raw).values())
    if words > 600:
        rep.warn("W3", f"prompt {words} words (>600 budget)")

    # --- W6: deprecated starter_visibility field (protocol v3) ----------------
    # FIELD declarations only — comments that merely mention the word
    # (e.g. "removed per protocol v3") must not warn.
    if re.search(r"^\s*starter_visibility\s*:", yaml_text, re.MULTILINE):
        rep.warn("W6", "starter_visibility is deprecated (protocol v3 stages the "
                       "family starter to /workspace/starter/ AND /workspace/ root "
                       "for every tier) — remove the field")

    # --- W7: provenance source field (2026-06-18) ----------------------------
    # Family-level property: required on the headline (family root / standalone),
    # not on each assisted tier (same underlying problem, same source).
    _is_tier = challenge_dir.parent.name == "assisted"
    src = (raw.get("source") or "").strip()
    if _is_tier:
        pass  # tiers inherit the family's source; no per-tier requirement
    elif src not in ("paper", "community"):
        rep.warn("W7", f"source field missing/invalid ({src!r}); set source: paper|community"
                       + (" and a reference" if src == "paper" else ""))
    elif src == "paper" and not (raw.get("reference") or "").strip():
        rep.warn("W7", "source: paper but reference is empty — add the paper citation/link")

    # --- W4: judge-wired evaluators ----------------------------------------------
    if "llm_judge" in yaml_text:
        rep.warn("W4", "llm_judge evaluators present — runtime needs a judge backend "
                       "(ARCHBENCH_JUDGE_MODEL / ANTHROPIC_API_KEY) or scores degrade to None")

    return rep


def _repo_root(challenge_dir: Path) -> Path:
    p = Path(challenge_dir).resolve()
    for anc in p.parents:
        if anc.name == "challenges":
            return anc.parent
    return p.parents[1]


def check_all(repo: Path) -> list[Report]:
    reports = []
    for y in sorted(glob.glob(str(repo / "challenges" / "**" / "challenge.yaml"),
                              recursive=True)):
        # work_in_progress/ holds unfinished challenges (no image / broken
        # baseline / scaffold); hello_world/ holds plumbing smoke tests — both
        # are parked out of the graded suite, so don't gate them.
        yp = y.replace(os.sep, "/")
        if "/work_in_progress/" in yp or "/hello_world/" in yp:
            continue
        reports.append(check_challenge(Path(os.path.dirname(y))))
    return reports


def render(reports: list[Report], errors_only: bool = False) -> str:
    out = []
    n_fail = sum(r.status == "FAIL" for r in reports)
    for r in reports:
        shown = [f for f in r.findings if not errors_only or f.level == "ERROR"]
        if r.status == "OK" and not shown:
            continue
        out.append(f"{r.status:<8} {r.challenge}")
        for f in shown:
            out.append(f"    [{f.level} {f.code}] {f.msg}")
    out.append(f"---- doctor: {len(reports)} checked, {n_fail} FAIL, "
               f"{sum(r.status=='SKIPPED' for r in reports)} skipped (BLOCKED)")
    return "\n".join(out)
