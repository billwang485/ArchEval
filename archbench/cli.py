"""archbench CLI — verify-all / run / baseline subcommands."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("archbench.cli")

REPO_ROOT = Path(__file__).resolve().parents[1]
# Tar search dirs come from `default_tar_search_dirs()` so the same logic
# (env-var ARCHBENCH_LEGACY_TAR_DIR override) is used in CLI, session, and tests.
from archbench.core.container import default_tar_search_dirs
from archbench.core.path_resolution import resolved_dirs as _resolved_dirs
DEFAULT_TAR_SEARCH = default_tar_search_dirs()

# `archbench images ...` lives in the archbench.image_management package (the discoverable
# image-management subsystem). main() calls register_images_subcommand() to
# wire the subparser; the verb functions are re-exported into this namespace so
# `archbench.cli.cmd_images_*` / `cli._resolve_image_targets` / `cli._save_tar_path`
# keep resolving for existing callers + tests. Imported AFTER DEFAULT_TAR_SEARCH
# is defined: the verbs read archbench.cli.DEFAULT_TAR_SEARCH lazily at call time, so
# there is no import cycle.
from archbench.image_management.cli import (  # noqa: E402,F401
    register_images_subcommand,
    cmd_images,
    cmd_images_status,
    cmd_images_build,
    cmd_images_load,
    cmd_images_save,
    cmd_images_pull,
    cmd_images_rm,
    cmd_images_gc,
    cmd_images_digest,
    _resolve_image_targets,
    _save_tar_path,
    _image_tar_candidates,
    _image_pool_path,
    _local_short_digest,
    _list_unmanaged_images,
)


# ---------------------------------------------------------------------------
# verify-all
# ---------------------------------------------------------------------------


def cmd_verify_all(args) -> int:
    """Probe every registered simulator + runtime end-to-end.

    For each: ensure_image is loadable, start a fresh container, run the
    in-container verify.sh, tear down. Reports green/red per component.

    Skips simulator/runtime checks if `--only` selects something else.
    Useful in CI as a preflight before SLURM submission.
    """
    from archbench.core.container import (
        ContainerConfig,
        ContainerManager,
        ensure_image,
        ImageNotFoundError,
    )
    from archbench.runtimes import _REGISTRY as RUNTIME_REGISTRY
    from archbench.simulators import _REGISTRY as SIM_REGISTRY

    only = set(args.only.split(",")) if args.only else None
    results: list[tuple[str, str, list[str]]] = []  # (kind, name, errors)

    # --- Simulators ---
    for sim_name, sim_cls in SIM_REGISTRY.items():
        if only and sim_name not in only:
            continue
        plugin = sim_cls()
        errs = _verify_simulator(plugin)
        results.append(("sim", sim_name, errs))

    # --- Runtimes ---
    for rt_name, rt_cls in RUNTIME_REGISTRY.items():
        if only and rt_name not in only:
            continue
        runtime = rt_cls()
        errs = _verify_runtime(runtime)
        results.append(("runtime", rt_name, errs))

    # --- Report ---
    print()
    print("=" * 60)
    print("archbench verify-all")
    print("=" * 60)
    all_ok = True
    for kind, name, errs in results:
        if errs:
            all_ok = False
            print(f"  [RED]    {kind:8s} {name}")
            for e in errs:
                print(f"            - {e}")
        else:
            print(f"  [GREEN]  {kind:8s} {name}")
    print("=" * 60)
    print(f"  result: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


def _verify_simulator(plugin) -> list[str]:
    from archbench.core.container import (
        ContainerConfig, ContainerManager, ensure_image,
        ImageNotFoundError,
    )
    name = plugin.name
    log.info("verify sim=%s image=%s", name, plugin.docker_image)
    try:
        ensure_image(plugin.docker_image, DEFAULT_TAR_SEARCH)
    except ImageNotFoundError as e:
        return [f"image not loadable: {e}"]
    except Exception as e:
        return [f"ensure_image raised: {e}"]

    cfg = ContainerConfig.with_run_id(plugin.docker_image, f"verify_sim_{name}")
    sim = ContainerManager(cfg)
    try:
        sim.start()
    except Exception as e:
        return [f"container start failed: {e}"]
    try:
        return plugin.verify_simulator(sim)
    except Exception as e:
        return [f"verify_simulator raised: {e}"]
    finally:
        sim.stop()


def _verify_runtime(runtime) -> list[str]:
    from archbench.core.container import (
        ContainerConfig, ContainerManager, ensure_image,
        ImageNotFoundError,
    )
    name = runtime.name
    log.info("verify runtime=%s image=%s", name, runtime.docker_image)

    # Host-side preflight first; if this fails, in-container is moot.
    host_errs = runtime.verify_runtime(REPO_ROOT)
    if host_errs:
        # Surface as warnings, still try in-container — some users won't have
        # all secrets set up locally but the image is still verifiable.
        log.warning("host-side preflight for %s: %s", name, host_errs)

    try:
        ensure_image(runtime.docker_image, DEFAULT_TAR_SEARCH)
    except ImageNotFoundError as e:
        return host_errs + [f"image not loadable: {e}"]
    except Exception as e:
        return host_errs + [f"ensure_image raised: {e}"]

    cfg = ContainerConfig.with_run_id(runtime.docker_image, f"verify_rt_{name}")
    agent = ContainerManager(cfg)
    try:
        agent.start()
    except Exception as e:
        return host_errs + [f"container start failed: {e}"]
    try:
        return host_errs + runtime.verify_in_container(agent)
    except Exception as e:
        return host_errs + [f"verify_in_container raised: {e}"]
    finally:
        agent.stop()


# ---------------------------------------------------------------------------
# run — single end-to-end (used by P5 smoke test)
# ---------------------------------------------------------------------------


def cmd_run(args) -> int:
    """Run one challenge × agent end-to-end.

    Front door:            archbench run <run.yaml> [agent]
    Legacy (back-compat):  archbench run <challenge_dir> <runtime> [flags]

    Everything else (challenge, tier, model, anonymize, …) lives in the run-spec
    YAML — one file, one run. See archbench/core/run_spec.py for the schema.
    """
    from archbench.core.challenge import load_challenge
    from archbench.runtimes import runtime_from_challenge
    from archbench.runtimes.session import run_session

    spec = _build_run_spec(args)
    if not (spec.challenge_dir / "challenge.yaml").exists():
        log.error("No challenge.yaml in %s", spec.challenge_dir)
        return 1
    challenge = load_challenge(spec.challenge_dir)
    runtime = runtime_from_challenge(spec.agent, challenge)
    log.info(
        "run: challenge=%s tier=%s agent=%s model=%s anonymize=%s",
        challenge.id, spec.tier or "(path)", runtime.name,
        spec.model or "(default)", spec.anonymize,
    )
    return run_session(
        challenge=challenge,
        runtime=runtime,
        anonymize=spec.anonymize,
        run_name=spec.run_name or f"run_{uuid.uuid4().hex[:8]}",
        results_root=spec.results_dir or (REPO_ROOT / "results"),
        dev_mode=spec.dev,
        model=spec.model,
        thinking=spec.thinking,
    )


def _build_run_spec(args):
    """Resolve CLI args to a single RunSpec.

    A ``.yaml`` / ``.yml`` first positional is the FRONT-DOOR run spec (and the
    optional second positional is the agent override). Anything else is the
    LEGACY ``<challenge_dir> <runtime>`` form, mapped onto the same RunSpec so
    existing scripts keep working unchanged.
    """
    from archbench.core.run_spec import RunSpec

    target = Path(args.challenge_dir)
    if target.suffix in (".yaml", ".yml") and target.is_file():
        return RunSpec.from_yaml(target, agent_override=args.runtime)
    if not args.runtime:
        raise SystemExit(
            "usage: archbench run <run.yaml> [agent]            (front door)\n"
            "   or: archbench run <challenge_dir> <runtime>     (legacy)"
        )
    return RunSpec(
        challenge_dir=target.resolve(),
        agent=args.runtime,
        model=args.model,
        anonymize=args.anonymize,
        run_name=args.run_name,
        results_dir=Path(args.results_dir) if args.results_dir else None,
        dev=getattr(args, "dev", False),
        thinking=getattr(args, "thinking", False),
        tier=None,
    )


def cmd_card(args) -> int:
    """Container card: stamp / verify / show the per-image content contract.

      archbench card stamp  <image> --role simulator --sim champsim   # gen the card
      archbench card verify <image>                                   # check live vs card
      archbench card show   <image>                                   # print the card
    """
    import subprocess
    import yaml as _yaml

    from archbench.core import container_card as cc
    from archbench.core.container import get_image_digest
    from archbench.image_management.engine import container_engine

    image = args.image
    engine = container_engine()
    path = cc.card_path_for(image)

    if args.action == "show":
        card = cc.load_card(path)
        if not card:
            print(f"no card at {path}")
            return 1
        if getattr(args, "raw", False):
            print(_yaml.safe_dump(card, sort_keys=False))
        else:
            print(cc.render_pretty(card))
        return 0

    if args.action == "verify":
        card = cc.load_card(path)
        if not card:
            print(f"no card at {path} — stamp it first: "
                  f"archbench card stamp {image} --role <role>")
            return 1
        rc = 0
        ident = cc.verify_identity(card, image)
        if ident is not None:
            match, live, stamped = ident
            if match:
                print(f"IDENTITY: this IS the exact image you stamped (digest {live[:12]}…)")
            else:
                print(f"IDENTITY: DIFFERENT image than stamped — "
                      f"live {live[:12]}… vs stamped {stamped[:12]}…")
                rc = 1
        violations = cc.verify_against_image(card, image, engine)
        if violations:
            print(f"CONTENT MISMATCH {image} (vs {path.name}):")
            for v in violations:
                print(f"  - {v}")
            return 1
        print(f"CONTENT OK {image} matches {path.name}")
        return rc

    # stamp
    d = cc.role_defaults(args.role, args.sim)
    commit = ""
    try:
        commit = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True).stdout.strip()
    except Exception:
        pass
    card = cc.stamp_from_image(
        image, args.role, engine,
        paths_present=d["paths_present"], paths_absent=d["paths_absent"],
        hash_files=d["hash_files"], env_absent_tokens=d["env_absent_tokens"],
        snapshot_dirs=d.get("snapshot_dirs", []),
        commit=commit, digest=get_image_digest(image) or "")
    cc.write_card(card, path)
    print(f"stamped {path}")
    print(_yaml.safe_dump(card, sort_keys=False))
    return 0


# ---------------------------------------------------------------------------
# baseline — sim-agnostic: run the SAME evaluate.sh the agent hits, on the
# starter, parse via plugin.parse_output, write + stamp baseline.json.
# ---------------------------------------------------------------------------


def _resolve_baseline_trace_files(
    challenge_dir: Path, baseline: dict,
    simulator_dir: Path | None = None,
) -> list[Path]:
    """Resolve per_trace files for trace_sha256, matching the drift guard.

    `_check_baseline_provenance` (session.py L1000-1038) derives trace
    names as ``t["trace"] + ".champsimtrace.xz"`` for each entry in
    ``baseline["per_trace"]`` and resolves them under
    ``simulator/subtraces`` then the repo's ``workload_pools/champsim``
    pool. We mirror that EXACTLY so the stamper's trace_sha256 byte-matches
    what the session re-derives. Returns [] when there is no per_trace (the
    stamper then zeroes trace_sha256, which the guard skips).

    Tier-mode: when ``simulator_dir`` is supplied (from
    ``load_challenge``'s resolved field), prefer ``simulator_dir/subtraces``
    over the legacy ``challenge_dir/simulator/subtraces`` candidate so
    family-root common dirs work. The workload_pools fallback always
    resolves against ``REPO_ROOT`` (not ``challenge_dir.parents[1]``,
    which is the wrong path under the tier layout).
    """
    per_trace = baseline.get("per_trace") or []
    if not per_trace:
        return []
    subtraces_candidates: list[Path] = []
    if simulator_dir is not None:
        subtraces_candidates.append(simulator_dir / "subtraces")
    subtraces_candidates.extend([
        challenge_dir / "simulator" / "subtraces",
        challenge_dir / "eval" / "subtraces",
        challenge_dir / "subtraces",
    ])
    subtraces_dir = next(
        (d for d in subtraces_candidates if d.is_dir()),
        subtraces_candidates[0],
    )
    workload_pool_dir = REPO_ROOT / "workload_pools" / "champsim"
    resolved: list[Path] = []
    for t in per_trace:
        tn = t["trace"] + ".champsimtrace.xz"
        sub = subtraces_dir / tn
        pool = workload_pool_dir / tn
        if sub.is_file():
            resolved.append(sub)
        elif pool.is_file():
            resolved.append(pool)
        else:
            raise FileNotFoundError(
                f"trace {tn!r} not in subtraces/ or workload_pools/champsim/ "
                f"(needed to stamp trace_sha256 for {challenge_dir.name})"
            )
    return resolved


def _recover_per_trace_from_raw(raw: str) -> list:
    """Recover a per-trace breakdown from raw evaluate.sh output.

    champsim's aggregate.py emits a single ChampSim-shape block whose
    ``_per_trace`` field carries the per-workload rows (each with a
    ``trace`` key). The plugin's parse_output collapses the block to
    scalars and drops ``_per_trace``, so we re-scan the raw stdout here to
    repopulate baseline["per_trace"] for the drift guard's trace rehash.

    Looks for ``_per_trace`` in (a) any ARCHBENCH_JSON_START/END block, then
    (b) the first balanced ``[{`` / ``{`` JSON token. Returns [] if none
    found (non-champsim sims simply have no per-trace breakdown).
    """
    import json as _json
    import re as _re

    def _extract_pt(obj) -> list:
        if isinstance(obj, list):
            obj = obj[0] if obj else {}
        if isinstance(obj, dict):
            pt = obj.get("_per_trace") or obj.get("per_trace")
            if isinstance(pt, list) and pt:
                return pt
        return []

    # (a) ARCHBENCH_JSON_START/END blocks.
    pos = 0
    while True:
        si = raw.find("ARCHBENCH_JSON_START", pos)
        if si < 0:
            break
        ei = raw.find("ARCHBENCH_JSON_END", si + 1)
        if ei < 0:
            break
        try:
            pt = _extract_pt(_json.loads(raw[si + len("ARCHBENCH_JSON_START"):ei].strip()))
            if pt:
                return pt
        except _json.JSONDecodeError:
            pass
        pos = ei + len("ARCHBENCH_JSON_END")

    # (b) First balanced bare JSON token (ChampSim aggregate block).
    for m in _re.finditer(r'[\[{]', raw):
        start = m.start()
        depth = 0
        in_str = False
        esc = False
        for i, c in enumerate(raw[start:], start):
            if esc:
                esc = False
                continue
            if c == "\\" and in_str:
                esc = True
                continue
            if c == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    try:
                        pt = _extract_pt(_json.loads(raw[start:i + 1]))
                        if pt:
                            return pt
                    except _json.JSONDecodeError:
                        pass
                    break
    return []


def _cmd_baseline_multi_sim(challenge, challenge_dir: Path) -> int:
    """Baseline for a multi-sim challenge (docs/multi_sim_design.md).

    The baseline = the cross-sim metric discrepancy at the STARTER configs.
    There's no single evaluate.sh; each sim has its own
    ``evaluation/evaluate_<sim>.sh`` (the same script that sim's submit
    dispatches to). For each bound sim we:
      1. ensure_image(plugin.docker_image) — fail fast.
      2. run evaluate_<sim>.sh on challenge/starter/ (the same code path the
         agent hits), parse via plugin.parse_output (§1 comparability).
      3. pull the cross-sim metric field (from the cross_sim_discrepancy
         evaluator config; default bandwidth_gbps).
    Then compute discrepancy_pct vs the reference sim and write baseline.json.

    Provenance is stamped against the PRIMARY sim's image (challenge.simulator)
    because the session-start drift guard (_check_baseline_provenance) checks
    the primary sim digest; the extra sims' images are validated at run start
    by ensure_image but are not part of the 4-tuple today (a follow-up could
    extend the tuple per-sim — recorded as a known gap).
    """
    import json
    import subprocess

    from archbench.core.container import ensure_image
    from archbench.core.provenance import stamp_baseline
    from archbench.runtimes.session import _child_env
    from archbench.simulators import get_plugin

    sims = challenge.simulators
    log.info("baseline (multi-sim): challenge=%s simulators=%s",
             challenge.id, sims)

    # Pull the cross_sim_discrepancy evaluator config for the metric field +
    # reference sim (so the baseline discrepancy is computed the SAME way the
    # post-session evaluator computes the agent's).
    metric_field = "bandwidth_gbps"
    reference_sim = challenge.simulator
    for ev in (challenge.evaluations or []):
        if ev.get("evaluator") == "cross_sim_discrepancy":
            cfg = ev.get("config") or {}
            metric_field = cfg.get("metric_field", metric_field)
            reference_sim = cfg.get("reference_sim", reference_sim)
            break

    # Tier-aware path resolution: prefer load_challenge's resolved fields
    # (simulator_dir / evaluation_dir / starter_dir); fall back to legacy
    # in-challenge-dir layout. See _resolved_dirs.
    sim_root_dir, eval_dir, starter_dir = _resolved_dirs(challenge, challenge_dir)
    # Baseline runs on the REFERENCE (baseline/one_shot), never the staged
    # starter (which is now the L2/L3 skeleton). Mirrors the single-sim
    # cmd_baseline path; defensive — no multi-sim challenge exists yet, but this
    # keeps the two baseline paths consistent once one is added.
    if getattr(challenge, "reference_dir", None):
        starter_dir = challenge.reference_dir
    if not starter_dir.is_dir():
        log.error("No starter dir resolved for %s (got %s)",
                  challenge_dir, starter_dir)
        return 1

    per_sim_metric: dict[str, float] = {}
    per_sim_full: dict[str, dict] = {}
    for sim in sims:
        plugin = get_plugin(sim)
        try:
            ensure_image(plugin.docker_image, DEFAULT_TAR_SEARCH)
        except Exception as e:
            log.error("ensure_image(%s) failed: %s", plugin.docker_image, e)
            return 1
        # Per-sim eval entry: evaluate_<sim>.sh, fallback to evaluate.sh.
        evaluate_sh = next((p for p in (
            eval_dir / f"evaluate_{sim}.sh",
            eval_dir / "evaluate.sh",
        ) if p.exists()), None)
        if evaluate_sh is None:
            log.error("No evaluate_%s.sh (or evaluate.sh) under %s/",
                      sim, eval_dir)
            return 1
        log.info("running %s on starter %s", evaluate_sh.name, starter_dir)
        try:
            result = subprocess.run(
                ["bash", str(evaluate_sh), str(starter_dir)],
                capture_output=True, text=True, timeout=1800, env=_child_env(),
            )
        except subprocess.TimeoutExpired:
            log.error("%s exceeded 1800s on the starter — infra/sim issue.",
                      evaluate_sh.name)
            return 1
        raw = result.stdout + result.stderr
        if result.returncode != 0:
            log.error("%s rc=%d on the starter (infra failure). Tail:\n%s",
                      evaluate_sh.name, result.returncode, raw[-2000:])
            return 1
        metric = plugin.parse_output(raw)
        if metric is None or metric.get(metric_field) is None:
            log.error(
                "%s ran but produced no %r metric for sim %s. Refusing to "
                "fabricate a baseline (§1.9). Tail:\n%s",
                evaluate_sh.name, metric_field, sim, raw[-2000:],
            )
            return 1
        per_sim_metric[sim] = float(metric[metric_field])
        per_sim_full[sim] = metric
        log.info("baseline: %s %s = %s", sim, metric_field, per_sim_metric[sim])

    # Compute the starter discrepancy vs the reference sim (max pairwise).
    ref_val = per_sim_metric.get(reference_sim)
    if not ref_val:
        log.error("reference sim %r has no %s; cannot compute discrepancy",
                  reference_sim, metric_field)
        return 1
    pairwise = {
        s: round(abs(v - ref_val) / ref_val * 100.0, 4)
        for s, v in per_sim_metric.items() if s != reference_sim
    }
    discrepancy_pct = max(pairwise.values()) if pairwise else 0.0

    baseline_path = eval_dir / "baseline.json"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline = {
        "discrepancy_pct": discrepancy_pct,
        "metric_field": metric_field,
        "reference_sim": reference_sim,
        "per_sim_metric": per_sim_metric,
        "pairwise_discrepancy_pct": pairwise,
        "per_sim_full": per_sim_full,
        "per_trace": [],
        "policy": "starter",
        "note": (
            f"Multi-sim baseline for {challenge.id}: cross-sim "
            f"{metric_field} discrepancy at the starter configs, measured by "
            f"running each sim's evaluate_<sim>.sh on challenge/starter/ (the "
            f"same scripts the agent's per-sim submit dispatches to)."
        ),
    }
    baseline_path.write_text(json.dumps(baseline, indent=2) + "\n")
    log.info("wrote multi-sim baseline.json -> %s (discrepancy_pct=%.4f)",
             baseline_path, discrepancy_pct)

    # Stamp provenance against the EVALUATION image (the pristine scorer the
    # session-start drift guard checks — docs §3). For multi-sim this defaults
    # to the PRIMARY sim image (eval == sim by default → unchanged), unless the
    # challenge pins evaluation_sim_image.
    primary_plugin = get_plugin(challenge.simulator)
    from archbench.image_management.plan import _resolve_pseudo_path
    eval_image = _resolve_pseudo_path(
        getattr(challenge, "evaluation_sim_image", None),
        primary_plugin.docker_image,
    )
    config_path = sim_root_dir / "config.json"
    if not config_path.exists():
        config_path = None
    try:
        prov = stamp_baseline(
            baseline_path,
            image_tag=eval_image,
            config_path=config_path,
            starter_dir=starter_dir,
            trace_files=[],
            repo_root=REPO_ROOT,
        )
    except Exception as e:
        log.error("provenance stamping failed: %s", e)
        return 1
    log.info("stamped provenance (primary sim %s): image=%s…",
             challenge.simulator, prov.image_digest[:16])
    log.info("multi-sim baseline OK: discrepancy_pct = %.4f%% "
             "(%s)", discrepancy_pct,
             ", ".join(f"{s}={v}" for s, v in per_sim_metric.items()))
    return 0


def cmd_baseline(args) -> int:
    """Regenerate evaluation/baseline.json by running the challenge's own
    evaluate.sh on the starter — the SAME script the agent's submit hits.

    Sim-agnostic flow (design §3.3):
      1. load_challenge + get_plugin.
      2. ensure_image(plugin.docker_image) — fail fast, no silent pull.
      3. Locate evaluation/evaluate.sh (fallback root evaluate.sh).
      4. Run it on challenge/starter/ with _child_env(); capture stdout+stderr.
      5. metric = plugin.parse_output(raw); abort LOUD if None (§1.9 — never
         fabricate a baseline).
      6. Write evaluation/baseline.json (metric + _per_trace if present + note).
      7. Stamp the Provenance 4-tuple via the unified stamper.

    This runs evaluate.sh directly (which itself spawns the sim container,
    e.g. via `podman run --rm`), so no long-lived sim container is started
    here. Returns 0 on success, non-zero on any failure.
    """
    import json
    import subprocess

    from archbench.core.challenge import load_challenge
    from archbench.core.container import ensure_image
    from archbench.core.provenance import stamp_baseline
    from archbench.runtimes.session import _child_env
    from archbench.simulators import get_plugin

    challenge_dir = Path(args.challenge_dir).resolve()
    if not (challenge_dir / "challenge.yaml").exists():
        log.error("No challenge.yaml in %s", challenge_dir)
        return 1
    challenge = load_challenge(challenge_dir)

    # §1.17 comparability: the baseline is a FAMILY-level constant — the
    # canonical "beat this" reference, identical across tiers — measured on the
    # full-scaffold reference design. Per-tier `starter/` only controls what is
    # STAGED to the agent (full / none / api_stub); an `api_stub` starter is a
    # throwaway schema stub and `none` has no starter at all. Measuring the
    # baseline on those yields a degenerate denominator AND corrupts the SHARED
    # common/evaluation/baseline.json (it would be overwritten with whichever
    # tier was last baselined). Refuse: baseline only from the full-scaffold
    # tier (its starter IS the reference). See lessons §22.
    # Protocol v2: the baseline (floor = "claude_code_oneshot") is measured
    # on the family REFERENCE implementation — challenge.reference_dir
    # (legacy location: <family>/assisted/L1/starter). The staged starter is
    # the unified skeleton and would be a degenerate denominator; refuse if
    # the family ships no reference at all.
    _ref = getattr(challenge, "reference_dir", None)
    if _ref is None and getattr(challenge, "tier_name", None) is not None:
        log.error(
            "Refusing to generate the baseline for %s: no reference "
            "implementation found (expected <family>/assisted/L1/starter). "
            "The floor baseline is measured on the reference, not the "
            "unified starter skeleton.", challenge.id,
        )
        return 1

    # Multi-sim challenges (docs/multi_sim_design.md): the baseline is the
    # cross-sim discrepancy at the STARTER configs. There's no single
    # evaluate.sh; each sim has its own evaluate_<sim>.sh. Hand off to the
    # multi-sim baseline builder, which runs each on challenge/starter/,
    # parses each via its plugin, and records every sim's metric + the
    # starter discrepancy. Single-sim falls through to the generic flow.
    if len(challenge.simulators) > 1:
        return _cmd_baseline_multi_sim(challenge, challenge_dir)

    plugin = get_plugin(challenge.simulator)
    log.info("baseline: challenge=%s simulator=%s", challenge.id, challenge.simulator)

    # Tier-aware path resolution: prefer load_challenge's resolved fields
    # (simulator_dir / evaluation_dir / starter_dir) so the new
    # <family>/common/{simulator,evaluation}/ + <family>/tiers/<T>/starter/
    # layout works transparently. Falls back to the legacy 3-subdir
    # construction for unmigrated single-tier challenges.
    sim_root_dir, eval_dir, starter_dir = _resolved_dirs(challenge, challenge_dir)
    # Protocol v2: the floor baseline runs on the family REFERENCE
    # implementation, not the unified staged skeleton. Legacy single-tier
    # challenges fall through (their starter IS the reference).
    if getattr(challenge, "reference_dir", None):
        starter_dir = challenge.reference_dir

    # 2. Image preflight — fail fast (no silent docker pull).
    try:
        ensure_image(plugin.docker_image, DEFAULT_TAR_SEARCH)
    except Exception as e:
        log.error("ensure_image(%s) failed: %s", plugin.docker_image, e)
        return 1

    # 3. Locate evaluate.sh: canonical evaluation_dir/, fallback root.
    evaluate_sh = next(
        (p for p in (
            eval_dir / "evaluate.sh",
            challenge_dir / "evaluate.sh",
        ) if p.exists()),
        None,
    )
    if evaluate_sh is None:
        log.error(
            "No evaluate.sh in %s (checked %s and root). The generic "
            "`archbench baseline` requires a sim-running evaluate.sh — the same "
            "script the agent's submit dispatches to.",
            challenge_dir, eval_dir,
        )
        return 1

    # 4. Run evaluate.sh on the starter dir (the SAME contract the agent hits).
    if not starter_dir.is_dir():
        log.error("No starter dir resolved for %s (got %s)",
                  challenge_dir, starter_dir)
        return 1
    log.info("running %s on starter %s", evaluate_sh, starter_dir)
    try:
        result = subprocess.run(
            ["bash", str(evaluate_sh), str(starter_dir)],
            capture_output=True, text=True, timeout=1800,
            env=_child_env(),
        )
    except subprocess.TimeoutExpired:
        log.error("evaluate.sh exceeded 1800s on the starter — infra/sim issue.")
        return 1
    raw = result.stdout + result.stderr
    if result.returncode != 0:
        # Non-zero rc from evaluate.sh signals INFRA failure (per §1.9 the
        # script encodes agent-code failure in JSON+marker at rc 0). The
        # starter is known-good, so a non-zero rc means the sim couldn't run.
        log.error("evaluate.sh returned rc=%d on the starter (infra failure). "
                  "Last 2000 chars of output:\n%s", result.returncode, raw[-2000:])
        return 1

    # 5. Parse via the SAME parser the connector uses → identical metric shape.
    metric = plugin.parse_output(raw)
    if metric is None:
        # §1.9 spirit: NEVER fabricate a baseline. If the starter ran but
        # produced no parseable metric, abort loudly so the operator fixes
        # evaluate.sh rather than shipping a zero/copied baseline.
        log.error(
            "plugin.parse_output returned None for the starter run of %s. "
            "Refusing to write a fabricated baseline (lessons §1/§1.9). "
            "Last 2000 chars of evaluate.sh output:\n%s",
            challenge.id, raw[-2000:],
        )
        return 1

    # 6. Assemble + write baseline.json. Surface _per_trace (champsim
    # aggregate.py shape) as per_trace so the drift guard can rehash traces.
    # baseline.json lives under the resolved evaluation_dir — for tier mode
    # this lands at <family>/common/evaluation/baseline.json, shared across
    # all tiers (per the pre-committed interface §8).
    baseline_path = eval_dir / "baseline.json"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline: dict = dict(metric)
    per_trace = metric.get("_per_trace") or metric.get("per_trace") or []
    if not per_trace:
        # champsim aggregator reconcile (design §5b.1): champsim's
        # evaluate.sh emits ONE bare ChampSim-shape block via aggregate.py;
        # parse_output collapses it to scalars and does NOT propagate the
        # per-trace breakdown (it lives as `_per_trace` *inside* the block).
        # Recover it from the raw output so per_trace is populated and the
        # session drift guard can rehash the workload traces.
        per_trace = _recover_per_trace_from_raw(raw)
    if per_trace:
        baseline["per_trace"] = per_trace
        baseline.pop("_per_trace", None)
    else:
        baseline.setdefault("per_trace", [])
    # Display the evaluate.sh location relative to the repo root so the note
    # is portable across legacy 3-subdir AND tier layouts (in tier mode,
    # evaluate.sh sits under <family>/common/evaluation/ which is NOT a
    # subpath of the per-tier challenge_dir).
    try:
        eval_display = evaluate_sh.relative_to(REPO_ROOT)
    except ValueError:
        eval_display = evaluate_sh
    baseline.setdefault(
        "note",
        f"Baseline for {challenge.id}: measured by running "
        f"{eval_display} on the resolved starter dir "
        f"(the same evaluate.sh the agent's submit dispatches to).",
    )
    if "policy" not in baseline:
        baseline["policy"] = "starter"
    baseline_path.write_text(json.dumps(baseline, indent=2) + "\n")
    log.info("wrote baseline.json -> %s", baseline_path)

    # 7. Stamp provenance (D.4 unified stamper). config under simulator_dir/
    # (tier-aware via _resolved_dirs).
    config_path = sim_root_dir / "config.json"
    if not config_path.exists():
        config_path = None
    try:
        trace_files = _resolve_baseline_trace_files(
            challenge_dir, baseline, simulator_dir=sim_root_dir,
        )
    except FileNotFoundError as e:
        log.error("cannot resolve trace files for stamping: %s", e)
        return 1
    # Stamp against the EVALUATION image (the pristine scorer), not blindly
    # plugin.docker_image — so baseline.json's provenance pins the same image
    # the session-start drift guard checks (docs/docker_management.md §3).
    # cmd_baseline has no runtime, so resolve the eval image directly:
    # challenge.evaluation_sim_image (a pseudo-path) if set, else default to
    # plugin.docker_image (eval == sim by default → unchanged for every
    # existing challenge).
    from archbench.image_management.plan import _resolve_pseudo_path
    eval_image = _resolve_pseudo_path(
        getattr(challenge, "evaluation_sim_image", None), plugin.docker_image,
    )
    try:
        prov = stamp_baseline(
            baseline_path,
            image_tag=eval_image,
            config_path=config_path,
            starter_dir=starter_dir,
            trace_files=trace_files,
            repo_root=REPO_ROOT,
        )
    except Exception as e:
        log.error("provenance stamping failed: %s", e)
        return 1
    log.info(
        "stamped provenance: image=%s… config=%s… starter=%s… trace=%s…",
        prov.image_digest[:16], prov.config_sha256[:16],
        prov.starter_sha256[:16], prov.trace_sha256[:16],
    )
    metric_val = baseline.get("metric", baseline.get(challenge.eval.metric, "?"))
    log.info("baseline OK: %s = %s", challenge.eval.metric, metric_val)
    return 0


# ---------------------------------------------------------------------------
# mcp-serve — start MCP standalone for external agent frameworks
# ---------------------------------------------------------------------------


def cmd_mcp_serve(args) -> int:
    """Start an MCP server bound to a sim container, then block until Ctrl+C.

    Lets external agent frameworks (mini-swe-baseline, terminal-bench,
    custom CLIs) point at this server's URL without using `archbench run`.

    Flow:
      1. Load challenge, ensure sim image, start sim container.
      2. plugin.verify_simulator → must pass.
      3. plugin.configure_simulator (stages traces, runs config.sh).
      4. Spawn MCP subprocess on the requested port (or auto-pick).
      5. Print the URL + 4 tool names; block until SIGINT/SIGTERM.
      6. atexit removes the sim container.

    The agent container is NOT started here — agents are external.
    """
    from archbench.core.anonymizer import Anonymizer
    from archbench.core.challenge import load_challenge
    from archbench.core.container import (
        ContainerConfig, ContainerManager, ensure_image,
    )
    from simulators.champsim.connector.server import SubmitContext
    from archbench.runtimes.session import (
        _find_free_port, _start_mcp_server, _wait_for_port,
    )
    from archbench.simulators import get_plugin

    challenge_dir = Path(args.challenge_dir).resolve()
    if not (challenge_dir / "challenge.yaml").exists():
        log.error("No challenge.yaml in %s", challenge_dir)
        return 1
    challenge = load_challenge(challenge_dir)
    plugin = get_plugin(challenge.simulator)

    ensure_image(plugin.docker_image, DEFAULT_TAR_SEARCH)
    sim_cfg = ContainerConfig.with_run_id(plugin.docker_image, "archbench_mcpserve_sim")
    sim = ContainerManager(sim_cfg)
    sim.start()

    log.info("verifying sim container ...")
    errors = plugin.verify_simulator(sim)
    if errors:
        log.error("sim verify failed: %s", errors)
        sim.stop()
        return 2
    log.info("configuring sim container ...")
    plugin.configure_simulator(sim, challenge)

    # Construct context with a dummy agent (we never copy from it; agents
    # external to this process push files into /work/submission/ themselves).
    class _NullAgent:
        name = ""
        def copy_out(self, *a, **kw): raise NotImplementedError(
            "mcp-serve runs without an agent container — submit() expects "
            "the EXTERNAL agent to place files in /work/submission/ "
            "in the sim container before calling submit (or in a host tmp "
            "dir for evaluate.sh mode)."
        )
    ctx = SubmitContext(
        challenge=challenge,
        challenge_dir=challenge_dir,
        plugin=plugin,
        agent=_NullAgent(),  # type: ignore[arg-type]
        sim=sim,
        anonymizer=Anonymizer.disabled() if not args.anonymize
                   else _load_anon(challenge.simulator),
    )

    port = args.port or _find_free_port()
    out_dir = Path(args.log_dir) if args.log_dir else challenge_dir / "_mcp_serve"
    out_dir.mkdir(parents=True, exist_ok=True)
    mcp_proc = _start_mcp_server(ctx, port, out_dir / "mcp.log")
    url = f"http://127.0.0.1:{port}/mcp"
    log.info("MCP subprocess started (pid=%d); waiting for bind...", mcp_proc.pid)
    if not _wait_for_port(port, mcp_proc, timeout=120):
        log.error("MCP didn't bind on %d within 120s", port)
        sim.stop()
        return 3

    print(
        f"\n{'=' * 60}\n"
        f"  ARCHEVAL MCP server READY\n"
        f"{'=' * 60}\n"
        f"  URL:        {url}\n"
        f"  Transport:  streamable-http\n"
        f"  Sim:        {sim.name}\n"
        f"  Tools:\n"
        f"    - submit()                  compile + simulate; returns typed OutcomeReport\n"
        f"    - browse_simulator(path)    list files in sim container (blocklist enforced)\n"
        f"    - read_simulator_file(path) read a file (blocklist enforced)\n"
        f"    - get_challenge_info()      prompt + starter files + submit count\n"
        f"{'=' * 60}\n"
        f"  Point your agent's MCP client at {url}.\n"
        f"  Ctrl+C to shut down (sim container auto-cleanup).\n"
        f"{'=' * 60}\n",
        flush=True,
    )

    import signal
    def _shutdown(_sig=None, _frm=None):
        log.info("shutting down...")
        try:
            mcp_proc.terminate()
            mcp_proc.wait(timeout=10)
        except Exception:
            try: mcp_proc.kill()
            except Exception: pass
        sim.stop()
        sys.exit(0)
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        mcp_proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown()
    return 0


def _load_anon(simulator: str):
    """Best-effort anonymizer loader for the named simulator."""
    if simulator == "champsim":
        from simulators.champsim.anonymization.build_anonymizer import (
            load_champsim_anonymizer,
        )
        return load_champsim_anonymizer()
    from archbench.core.anonymizer import Anonymizer
    return Anonymizer.disabled()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------



def cmd_doctor(args) -> int:
    """[concept: VERIFY] repo-side pair-consistency checks (archbench/core/doctor.py)."""
    from pathlib import Path
    from archbench.core import doctor
    if args.all:
        reports = doctor.check_all(Path(".").resolve())
    else:
        if not args.challenge_dir:
            print("doctor: give a challenge dir or --all"); return 2
        reports = [doctor.check_challenge(Path(args.challenge_dir))]
    print(doctor.render(reports, errors_only=args.errors_only))
    return 1 if any(r.status == "FAIL" for r in reports) else 0


def cmd_smoke(args) -> int:
    """[concept: VERIFY] NoopAgent end-to-end smoke: stage the challenge, run
    the REAL session machinery with a deterministic no-LLM agent that submits
    the staged starter artifacts once, and report outcome (+ baseline parity
    is then visible in eval_simulator_metric.json). The runnability gate that
    static checks (doctor) cannot provide."""
    import os
    from pathlib import Path
    from archbench.core.challenge import load_challenge
    ch = load_challenge(Path(args.challenge_dir))
    outs = getattr(ch, "output_files", None) or []
    if not outs:
        print("smoke: challenge declares no output.files — nothing to noop-submit")
        return 2
    # Protocol v2: the unified starter is staged to /workspace/starter/ (the
    # workspace root is empty until an agent authors there). The NoopAgent
    # writes nothing, so it submits the staged starter files in place — the
    # floor behavior. submit() accepts any /workspace/-prefixed path whose
    # basename is a declared submission file, so /workspace/starter/<f> is valid.
    os.environ["ARCHBENCH_NOOP_SUBMIT"] = ",".join(f"/workspace/starter/{f}" for f in outs)
    args.runtime = args.runtime or "mini"
    args.run_name = args.run_name or f"smoke_{ch.id}"
    args.anonymize = False
    args.dev = False
    return cmd_run(args)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="archbench")
    sub = parser.add_subparsers(dest="cmd")

    p_va = sub.add_parser("verify-all", help="Probe every simulator + runtime")
    p_va.add_argument("--only", help="Comma-separated list of names to check")
    p_va.set_defaults(func=cmd_verify_all)

    p_run = sub.add_parser(
        "run",
        help="Run one challenge × agent. Front door: archbench run <run.yaml> [agent]",
    )
    p_run.add_argument(
        "challenge_dir", metavar="run.yaml | challenge_dir",
        help="a run-spec YAML (front door — everything's in it) OR, legacy, a "
             "challenge directory",
    )
    p_run.add_argument(
        "runtime", nargs="?", default=None, metavar="agent | runtime",
        help="agent override (with a run.yaml) OR the runtime name (legacy "
             "<challenge_dir> <runtime> form)",
    )
    p_run.add_argument("--run-name")
    p_run.add_argument(
        "--anonymize", action=argparse.BooleanOptionalAction,
        default=True,
        help="Anonymize SPEC trace names + simulator workload identifiers so "
             "agents can't pattern-match training-data priors. ON by default for "
             "valid head-to-head benchmark numbers. Pass --no-anonymize to "
             "disable (e.g. for debug runs where you want recognizable trace "
             "names in the trajectory).",
    )
    p_run.add_argument("--results-dir")
    p_run.add_argument(
        "--dev", action="store_true",
        help="Enable dev mode: bind-mount the runtime's src/ over the baked code. "
             "Only valid for runtimes whose info.yaml has mode: dev_capable.",
    )
    p_run.add_argument(
        "--model", default=None,
        help="Model identifier. For bundled runtimes (claude_code/codex/gemini), "
             "must be in the runtime's info.yaml allowed_models. For byo_model "
             "runtimes (mini/archharness), must be a key in archbench/serving/routes.yaml. "
             "If omitted, uses the runtime's default_model.",
    )
    p_run.add_argument(
        "--thinking", action="store_true",
        help="Enable reasoning/thinking mode. For byo_model runtimes, redirects to "
             "the thinking-enabled route variant if one exists (e.g., gemma4 -> "
             "gemma4-thinking). For bundled runtimes, sets vendor-specific reasoning "
             "flags if supported.",
    )
    p_run.set_defaults(func=cmd_run)

    p_card = sub.add_parser(
        "card", help="Container card: stamp/verify/show what MUST be inside an image")
    p_card.add_argument("action", choices=["stamp", "verify", "show"])
    p_card.add_argument("image", help="image tag, e.g. localhost/archbench-champsim:v6")
    p_card.add_argument("--role", default="simulator",
                        choices=["simulator", "agent", "agent_sim"])
    p_card.add_argument("--sim", default=None,
                        help="sim name (for simulator / agent_sim roles)")
    p_card.add_argument("--raw", action="store_true",
                        help="show: print the raw card yaml instead of the readable view")
    p_card.set_defaults(func=cmd_card)

    p_bl = sub.add_parser("baseline", help="Regenerate baseline.json for a challenge")
    p_bl.add_argument("challenge_dir")

    p_doc = sub.add_parser("doctor", help="repo-side pair-consistency checks (prompts/cards/baselines/rubric)")
    p_doc.add_argument("challenge_dir", nargs="?", default=None)
    p_doc.add_argument("--all", action="store_true")
    p_doc.add_argument("--errors-only", action="store_true")
    p_doc.set_defaults(func=cmd_doctor)

    p_smoke = sub.add_parser("smoke", help="NoopAgent end-to-end smoke (no LLM): submit the staged starter once via the real session machinery")
    p_smoke.add_argument("challenge_dir")
    p_smoke.add_argument("runtime", nargs="?", default="mini")
    p_smoke.add_argument("--run-name")
    p_smoke.add_argument("--results-dir")
    p_smoke.add_argument("--model", default="gemma4")  # proxy boots; noop never calls it
    p_smoke.set_defaults(func=cmd_smoke)
    p_bl.set_defaults(func=cmd_baseline)

    # images — inventory + lifecycle over the images.yaml manifest (K0 + K5).
    # The verbs + their subparser live in archbench/image_management/cli.py (the discoverable
    # image-management package); this is the thin hook that wires them in so
    # `archbench images ...` works identically.
    register_images_subcommand(sub)

    p_mcp = sub.add_parser(
        "mcp-serve",
        help="Start standalone MCP server bound to a sim container "
             "(for integrating with external agent frameworks)",
    )
    p_mcp.add_argument("challenge_dir")
    p_mcp.add_argument("--port", type=int, default=0,
                       help="TCP port (0 = pick free port)")
    p_mcp.add_argument(
        "--anonymize", action=argparse.BooleanOptionalAction,
        default=True,
        help="Anonymize trace/workload identifiers in MCP tool outputs. "
             "ON by default; pass --no-anonymize to disable for debug.",
    )
    p_mcp.add_argument("--log-dir", help="Where to write mcp.log (default: <challenge_dir>/_mcp_serve/)")
    p_mcp.set_defaults(func=cmd_mcp_serve)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
