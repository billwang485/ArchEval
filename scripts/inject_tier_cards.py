"""Inject 3-tier eval cards + optional head-to-head section into a run's
report.html.

Reads:
  results_dir/eval_simulator_metric.json        — Tier 3
  results_dir/eval_deliverable_files.json       — Tier 1 (existence) + Tier 2 (LLM judge)
  results_dir/eval_trajectory_audit.json        — Tier 2
  results_dir/eval_offline_sim_calibration.json — Tier 2 (NEW)
  results_dir/session.json
  results_dir/submit_outcomes.jsonl

Writes:
  results_dir/report.html — modified in place; the 3-tier section is
  inserted right after ``<div class="container">``. Idempotent: re-runs
  replace a prior insertion (delimited by HTML comments).

Optional flags:
  --vs <run_dir>      Add a "vs <name> head-to-head" section comparing the
                      three tier scores side by side.

Usage:
  python3 scripts/inject_tier_cards.py results/cache_replacement/<run>
  python3 scripts/inject_tier_cards.py results/cache_replacement/<run> \\
      --vs results/cache_replacement/<other_run>

The output styling uses the same CSS variables already defined in the
existing report.html so the new cards inherit the dark theme.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Optional

START = "<!-- BEGIN-PHASE-F-TIER-CARDS -->"
END = "<!-- END-PHASE-F-TIER-CARDS -->"

VS_START = "<!-- BEGIN-PHASE-F-HEAD-TO-HEAD -->"
VS_END = "<!-- END-PHASE-F-HEAD-TO-HEAD -->"


def _read(p: Path) -> Optional[dict[str, Any]]:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _tier_state(results_dir: Path) -> dict[str, Any]:
    """Compute the three-tier summary for one run."""
    sim = _read(results_dir / "eval_simulator_metric.json") or {}
    deliv = _read(results_dir / "eval_deliverable_files.json") or {}
    traj = _read(results_dir / "eval_trajectory_audit.json") or {}
    calib = _read(results_dir / "eval_offline_sim_calibration.json") or {}
    session = _read(results_dir / "session.json") or {}

    # Submit outcomes — counts.
    submits_path = results_dir / "submit_outcomes.jsonl"
    submit_outcomes: list[str] = []
    if submits_path.exists():
        for line in submits_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            submit_outcomes.append(row.get("outcome", "?"))
    any_sim_ok = any(o.lower() == "sim_ok" for o in submit_outcomes)

    # --- Tier 1 ---
    per_file = (deliv.get("per_file") or {}) if isinstance(deliv, dict) else {}
    files_present = sum(1 for f in per_file.values() if f.get("exists"))
    files_total = len(per_file)
    rc = session.get("rc")
    t1_pass = (
        files_total > 0 and files_present == files_total
        and bool(submit_outcomes) and rc == 0
    )
    t1_reasons: list[str] = []
    if files_total == 0:
        t1_reasons.append("deliverable_files reported no required files")
    elif files_present < files_total:
        missing = [n for n, f in per_file.items() if not f.get("exists")]
        t1_reasons.append(f"missing deliverables: {', '.join(missing)}")
    if not submit_outcomes:
        t1_reasons.append("no submit outcome recorded")
    if rc not in (0, None):
        t1_reasons.append(f"session rc={rc}")

    # --- Tier 2 ---
    # deliverable_files LLM-judge layer
    judge_scores = [
        f.get("judge_score") for f in per_file.values()
        if f.get("judge_score") is not None
    ]
    judge_total = sum(1 for f in per_file.values() if f.get("passes_min_chars"))
    # trajectory_audit checks
    checks = (traj.get("checks") or {}) if isinstance(traj, dict) else {}
    t_scores = [
        v.get("score") for v in checks.values()
        if isinstance(v, dict) and v.get("score") is not None
    ]
    # offline_sim_calibration
    cal_ok = bool(calib.get("ok"))
    mae = calib.get("mean_absolute_error") if cal_ok else None
    dir_acc = calib.get("direction_accuracy") if cal_ok else None
    cal_reason = (
        None if cal_ok else (calib.get("reason") or "no calibration data")
    )

    t2_summary = {
        "deliv_judge_avg": (sum(judge_scores) / len(judge_scores)) if judge_scores else None,
        "deliv_judge_n": len(judge_scores),
        "deliv_judge_total": judge_total,
        "traj_judge_avg": (sum(t_scores) / len(t_scores)) if t_scores else None,
        "traj_judge_n": len(t_scores),
        "traj_judge_total": len(checks),
        "calib_ok": cal_ok,
        "calib_mae": mae,
        "calib_dir_acc": dir_acc,
        "calib_reason": cal_reason,
        "judge_rationales": [
            (name, f.get("judge_rationale", "")) for name, f in per_file.items()
            if f.get("judge_rationale")
        ],
    }

    # --- Tier 3 ---
    speedup = sim.get("geomean_speedup")
    t3_state = {
        "speedup": speedup,
        "source": sim.get("source"),
        "per_trace_ipc": sim.get("per_trace_ipc"),
    }

    return {
        "tier1": {
            "pass": t1_pass,
            "files_present": files_present,
            "files_total": files_total,
            "submit_outcomes": submit_outcomes,
            "any_sim_ok": any_sim_ok,
            "rc": rc,
            "reasons": t1_reasons,
        },
        "tier2": t2_summary,
        "tier3": t3_state,
        "_session": session,
    }


def _fmt_speedup(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v:.4f}×"


def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v*100:.0f}%"


def _color_class(v: Optional[float], threshold: float = 1.0) -> str:
    if v is None:
        return ""
    return "score-positive" if v >= threshold else "score-negative"


def _render_tier_cards(state: dict[str, Any], run_name: str) -> str:
    t1, t2, t3 = state["tier1"], state["tier2"], state["tier3"]

    t1_status_text = "PASS" if t1["pass"] else "PARTIAL"
    t1_status_cls = "score-positive" if t1["pass"] else "score-negative"
    t1_details = []
    t1_details.append(f"{t1['files_present']}/{t1['files_total']} deliverables present")
    t1_details.append(f"submit: {len(t1['submit_outcomes'])} ({', '.join(t1['submit_outcomes']) or 'none'})")
    t1_details.append(f"rc={t1['rc']}")
    if t1["reasons"]:
        t1_details.append("issues: " + "; ".join(t1["reasons"]))
    t1_detail_html = " &middot; ".join(t1_details)

    t2_lines: list[str] = []
    if t2["deliv_judge_total"]:
        avg = t2["deliv_judge_avg"]
        t2_lines.append(
            f"deliverable judge {t2['deliv_judge_n']}/{t2['deliv_judge_total']}"
            + (f" — avg {avg:.2f}" if avg is not None else " — judge null (no API key?)")
        )
    if t2["traj_judge_total"]:
        avg = t2["traj_judge_avg"]
        t2_lines.append(
            f"trajectory judge {t2['traj_judge_n']}/{t2['traj_judge_total']}"
            + (f" — avg {avg:.2f}" if avg is not None else " — judge null (no API key?)")
        )
    if t2["calib_ok"]:
        t2_lines.append(
            f"offline-sim calibration: MAE {t2['calib_mae']:.4f}, "
            f"direction accuracy {_fmt_pct(t2['calib_dir_acc'])}"
        )
    elif t2["calib_reason"]:
        t2_lines.append(f"offline-sim calibration: not computed — {t2['calib_reason']}")
    if not t2_lines:
        t2_lines.append("no Tier-2 evaluator output")
    t2_html = "<br>".join(_escape(L) for L in t2_lines)

    t3_speedup = t3["speedup"]
    t3_class = _color_class(t3_speedup)
    t3_pct = ""
    if t3_speedup is not None:
        delta = (t3_speedup - 1.0) * 100
        t3_pct = f"{delta:+.2f}% vs LRU baseline"

    return f"""{START}
<section id="phase-f-tiers">
  <h2>3-tier eval (headline)</h2>
  <p style="color: var(--muted); font-size: 13px; margin: 0 0 14px;">
    Three independent scores: Basic / Process / Outcome. See
    <a href="../../docs/evaluator_framework.md" style="color: var(--accent);">docs/evaluator_framework.md</a>
    for the rationale. Detailed per-evaluator output below.
  </p>
  <div class="score-grid">
    <div class="score-card big">
      <div class="score-label">Tier 1 &middot; Basic (procedural)</div>
      <div class="score-value {t1_status_cls}">{t1_status_text}</div>
      <div class="score-sub">{_escape(t1_detail_html)}</div>
    </div>
    <div class="score-card big">
      <div class="score-label">Tier 2 &middot; Process (quality of thinking)</div>
      <div class="score-value" style="font-size: 22px; line-height: 1.3;">{t2_html}</div>
      <div class="score-sub">LLM-judge + offline-sim calibration MAE</div>
    </div>
    <div class="score-card big">
      <div class="score-label">Tier 3 &middot; Outcome (real ChampSim)</div>
      <div class="score-value {t3_class}">{_fmt_speedup(t3_speedup)}</div>
      <div class="score-sub">{_escape(t3_pct) if t3_pct else 'speedup vs baseline LRU'}</div>
    </div>
  </div>
</section>
{END}
"""


def _escape(s: str) -> str:
    """Minimal HTML escape that preserves &middot;/&mdash; etc."""
    if not isinstance(s, str):
        s = str(s)
    out = s.replace("&amp;", "&").replace("&", "&amp;")
    out = out.replace("<", "&lt;").replace(">", "&gt;")
    # Restore allowed entities so our injected &middot; et al. survive.
    out = out.replace("&amp;middot;", "&middot;").replace("&amp;mdash;", "&mdash;")
    return out


def _render_head_to_head(this_state, this_name, other_state, other_name) -> str:
    """Two-column comparison of the 3 tier scores."""
    rows: list[tuple[str, str, str]] = []
    t1a = this_state["tier1"]; t1b = other_state["tier1"]
    rows.append((
        "Tier 1 Basic",
        f"{'PASS' if t1a['pass'] else 'PARTIAL'} ({t1a['files_present']}/{t1a['files_total']} files, rc={t1a['rc']})",
        f"{'PASS' if t1b['pass'] else 'PARTIAL'} ({t1b['files_present']}/{t1b['files_total']} files, rc={t1b['rc']})",
    ))

    def t2_short(t2):
        bits = []
        if t2["deliv_judge_avg"] is not None:
            bits.append(f"deliv judge avg {t2['deliv_judge_avg']:.2f}")
        elif t2["deliv_judge_total"]:
            bits.append(f"deliv judge {t2['deliv_judge_total']} (null)")
        if t2["traj_judge_avg"] is not None:
            bits.append(f"traj judge avg {t2['traj_judge_avg']:.2f}")
        elif t2["traj_judge_total"]:
            bits.append(f"traj judge {t2['traj_judge_total']} (null)")
        if t2["calib_ok"]:
            bits.append(f"calib MAE {t2['calib_mae']:.3f}, dir {_fmt_pct(t2['calib_dir_acc'])}")
        else:
            bits.append("calib n/a")
        return "; ".join(bits) if bits else "—"

    rows.append(("Tier 2 Process", t2_short(this_state["tier2"]), t2_short(other_state["tier2"])))

    t3a = this_state["tier3"]; t3b = other_state["tier3"]
    rows.append((
        "Tier 3 Outcome",
        _fmt_speedup(t3a["speedup"]),
        _fmt_speedup(t3b["speedup"]),
    ))

    rows_html = "\n".join(
        f"<tr><td><b>{_escape(label)}</b></td>"
        f"<td>{_escape(a)}</td>"
        f"<td>{_escape(b)}</td></tr>"
        for label, a, b in rows
    )
    return f"""{VS_START}
<section id="phase-f-head-to-head">
  <h2>vs {_escape(other_name)} (head-to-head)</h2>
  <p style="color: var(--muted); font-size: 13px; margin: 0 0 12px;">
    Same challenge, same evaluators. The 3-tier scores side-by-side.
  </p>
  <table class="score-table">
    <thead><tr><th>Tier</th><th>{_escape(this_name)}</th><th>{_escape(other_name)}</th></tr></thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
</section>
{VS_END}
"""


def _inject_or_replace(html: str, block: str, start: str, end: str) -> str:
    """If START..END already present, replace; else insert after container open."""
    pat = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if pat.search(html):
        return pat.sub(block.strip(), html)
    # Insert right after the first `<div class="container">`.
    anchor = '<div class="container">'
    idx = html.find(anchor)
    if idx < 0:
        # Last resort: prepend before </body>.
        return html.replace("</body>", block + "\n</body>")
    insert_at = idx + len(anchor)
    return html[:insert_at] + "\n" + block + "\n" + html[insert_at:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir", type=Path)
    ap.add_argument("--vs", type=Path, default=None,
                    help="Optional: other run dir to head-to-head against.")
    ap.add_argument("--vs-name", type=str, default=None,
                    help="Display label for the --vs run (defaults to its dir name).")
    args = ap.parse_args()

    rd = Path(args.results_dir)
    report = rd / "report.html"
    if not report.exists():
        raise SystemExit(f"no report.html at {report}")

    html = report.read_text()
    state = _tier_state(rd)
    tier_block = _render_tier_cards(state, rd.name)
    html = _inject_or_replace(html, tier_block, START, END)

    if args.vs is not None:
        other = Path(args.vs)
        other_state = _tier_state(other)
        other_name = args.vs_name or other.name
        vs_block = _render_head_to_head(state, rd.name, other_state, other_name)
        html = _inject_or_replace(html, vs_block, VS_START, VS_END)

    report.write_text(html)
    print(f"patched {report}")


if __name__ == "__main__":
    main()
