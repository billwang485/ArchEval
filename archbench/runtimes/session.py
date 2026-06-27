"""[concept: ORCHESTRATION — the conductor; weaves in VERIFY/MONITOR/EVALUATE. See ARCHITECTURE.md]

run_session — orchestrate one challenge × runtime end-to-end.

Flow (per the design paradigm in README):

  1. ensure_image for sim + agent (no silent docker pull)
  2. Start sim container, verify, configure
  3. Start agent container, verify
  4. Start MCP server (subprocess, port-bound)
  5. runtime.start_session(...) — BLOCKING, drives the agent
  6. Persist trajectory + final metrics into results/<challenge>/<run_name>/
  7. Provenance check against baseline.json (any drift → red)
  8. atexit unconditional cleanup (registered by ContainerManager.start)

Returns the rc the CLI passes through to the shell.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from archbench.core.anonymizer import Anonymizer
from archbench.core.container import (
    ContainerConfig,
    ContainerManager,
    ensure_image,
)
from simulators.champsim.connector.server import SubmitContext
from archbench.core.path_resolution import resolved_dirs
from archbench.core.provenance import (
    repo_root_from_challenge_dir,
    Provenance,
    docker_image_digest,
    git_head_commit,
    sha256_of_bytes,
    sha256_of_file,
)
from archbench.simulators import get_plugin

if TYPE_CHECKING:
    from archbench.core.challenge import Challenge
    from archbench.core.runtime_base import AgentRuntime

log = logging.getLogger("archbench.session")

REPO_ROOT = Path(__file__).resolve().parents[2]
from archbench.core.container import default_tar_search_dirs
DEFAULT_TAR_SEARCH = default_tar_search_dirs()


# ``_l2agent_image`` lives in archbench.image_management.plan (the pure resolver, K4) and is
# re-exported here so existing importers keep working unchanged. In particular
# ``tests/test_tier_behavior.py`` does ``from archbench.runtimes.session import
# _l2agent_image`` and asserts its exact string output (docs §11) — this
# re-export preserves both the symbol and the output byte-for-byte.
from archbench.image_management.plan import _l2agent_image, resolve_images  # noqa: E402,F401


def run_session(
    challenge: Challenge,
    runtime: AgentRuntime,
    anonymize: bool,
    run_name: str,
    results_root: Path,
    dev_mode: bool = False,
    model: Optional[str] = None,
    thinking: bool = False,
) -> int:
    """End-to-end: returns 0 on success, nonzero on infra/agent failure.

    `dev_mode` (set from CLI's `--dev`) is propagated onto the runtime
    instance. Before any container starts we enforce that the runtime's
    info.yaml declares `mode: dev_capable`; bake_only runtimes reject
    --dev with a RuntimeError so users don't accidentally hit the
    bind-mount path on off-the-shelf agents.

    `model` (CLI --model) selects the model. For bundled runtimes
    (claude_code/codex/gemini), it must be in info.yaml's allowed_models.
    For byo_model runtimes (mini/archharness), it must be a key in
    archbench/serving/routes.yaml. If None, the runtime's default_model from
    info.yaml is used (already populated into RuntimeSpec.model by the
    challenge loader).

    `thinking` (CLI --thinking) enables reasoning. For byo_model + a
    matching `<model>-thinking` route, the model is auto-redirected to
    the thinking variant; otherwise it's a warning (the underlying route
    may already enable thinking via routes.yaml `extra_body`).
    """
    plugin = get_plugin(challenge.simulator)
    # Multi-simulator support (docs/multi_sim_design.md): challenge.simulators
    # is [primary, *extra]. For the common single-sim case this is just
    # [challenge.simulator] and every branch below is a no-op extra. The
    # EXTRA sims (everything after the primary) each get their own plugin +
    # per-run container + a prefixed tool namespace in the MCP server.
    extra_sim_names = [s for s in challenge.simulators if s != challenge.simulator]
    extra_plugins = {name: get_plugin(name) for name in extra_sim_names}

    # 4a/4b. Mode-compat enforcement BEFORE image work so the failure is
    # cheap and clear. Default to "bake_only" if info.yaml didn't declare
    # `mode:` — the safer default for an unknown runtime.
    runtime.dev_mode = dev_mode
    rt_info = challenge.runtime_for(runtime.name)
    mode = rt_info.data.get("mode", "bake_only")
    if dev_mode and mode != "dev_capable":
        raise RuntimeError(
            f"Runtime {runtime.name!r} has mode={mode!r}; "
            f"--dev requires mode: dev_capable. "
            f"Use --dev only with in-house runtimes (archharness, mini)."
        )

    # Resolve + validate model against the schema sibling's RuntimeSpec
    # fields (runtime_type, allowed_models). Fail BEFORE any container
    # work so the user sees a clear early error.
    runtime_type = getattr(rt_info, "runtime_type", "byo_model")
    resolved_model = model or rt_info.model  # info.yaml's default_model
                                              # lands in spec.model via loader
    routes_obj = None  # populated lazily for byo_model
    if runtime_type == "bundled":
        allowed = rt_info.allowed_models or []
        if resolved_model is None:
            raise RuntimeError(
                f"Runtime {runtime.name!r} (bundled) has no resolved model "
                f"(neither --model nor info.yaml default_model). "
                f"Allowed: {allowed}"
            )
        if allowed and resolved_model not in allowed:
            raise RuntimeError(
                f"Runtime {runtime.name!r} (bundled) doesn't allow model "
                f"{resolved_model!r}. Allowed: {allowed}"
            )
    elif runtime_type == "byo_model":
        # Validate against routes.yaml — sibling agent owns this module.
        from archbench.serving.routes import load_routes, default_routes_path
        routes_obj = load_routes(default_routes_path())
        # Apply --thinking redirect (a): if `<model>-thinking` exists, use it.
        if thinking and resolved_model is not None:
            thinking_variant = f"{resolved_model}-thinking"
            if thinking_variant in routes_obj:
                log.info(
                    "--thinking: redirecting model %r -> %r",
                    resolved_model, thinking_variant,
                )
                resolved_model = thinking_variant
            else:
                # The route itself may already enable thinking via
                # extra_body; warn and continue.
                entry = routes_obj.get(resolved_model)
                if entry and entry.supports_thinking:
                    log.info(
                        "--thinking: route %r supports_thinking (no variant "
                        "redirect needed)", resolved_model,
                    )
                else:
                    log.warning(
                        "--thinking: no %r route; running without thinking",
                        thinking_variant,
                    )
        if resolved_model is None:
            raise RuntimeError(
                f"Runtime {runtime.name!r} (byo_model) has no resolved model "
                f"(neither --model nor info.yaml default_model). "
                f"Available routes: {routes_obj.names()}"
            )
        if resolved_model not in routes_obj:
            raise RuntimeError(
                f"Runtime {runtime.name!r} (byo_model) requested model "
                f"{resolved_model!r} but it's not in archbench/serving/routes.yaml. "
                f"Available: {routes_obj.names()}"
            )
    else:
        raise RuntimeError(
            f"Runtime {runtime.name!r} has unknown runtime_type={runtime_type!r}; "
            f"expected 'bundled' or 'byo_model'."
        )

    # Push the resolved model onto the runtime instance so its
    # start_session() picks it up (mini/archharness/claude_code all read
    # self.model when building the exec command).
    if hasattr(runtime, "model") and resolved_model is not None:
        runtime.model = resolved_model

    # 1. Image plan (PURE — no docker calls; docs/docker_management.md §3).
    # resolve_images derives the 3 logical images from (challenge, plugin,
    # runtime). DEFAULT-IDENTICAL: with no new YAML keys this yields exactly
    # today's strings (agent_centric -> runtime.docker_image; simulator_centric
    # -> _l2agent_image(sim); eval == plugin.docker_image).
    plan = resolve_images(challenge, plugin, runtime)
    agent_image = plan.agent_image
    # challenge_centric is recognized-but-not-runnable (§1.3): typed, loud failure
    # BEFORE any container starts — never a silent fallback to agent_centric.
    if plan.agent_image_mode == "challenge_centric":
        raise NotImplementedError(
            "challenge_centric agent_image_mode is an interface placeholder; "
            "not implemented"
        )

    # 1. Image preflight (fail fast — no silent pull). The SIM container runs
    # on plan.simulator_image (== plugin.docker_image). The EVAL image
    # (plan.evaluation_sim_image, == plan.simulator_image by default) is what
    # the provenance/baseline gate pins, per §3 — the pristine scorer.
    sim_digest = ensure_image(plan.simulator_image, DEFAULT_TAR_SEARCH)
    eval_digest = ensure_image(plan.evaluation_sim_image, DEFAULT_TAR_SEARCH)
    agent_digest = ensure_image(agent_image, DEFAULT_TAR_SEARCH)
    log.info(
        "images: sim=%s eval=%s agent=%s (agent_image_mode=%s, source=%s%s)",
        sim_digest[:24], eval_digest[:24], agent_digest[:24],
        plan.agent_image_mode, plan.source,
        ", simulator dev image" if agent_image != runtime.docker_image else "",
    )
    # Preflight extra-sim images too (multi-sim). No-op for single-sim.
    for name, ep in extra_plugins.items():
        ed = ensure_image(ep.docker_image, DEFAULT_TAR_SEARCH)
        log.info("images: extra sim %s=%s", name, ed[:24])

    # 1a. Surface judge-auth env at session start so the user knows BEFORE
    # the run completes whether the post-session evaluators (in-process,
    # see _run_post_session_evaluators) will have an LLM backend. The
    # Phase C Gemma 4 run surfaced "judge: no LLM backend configured"
    # only AFTER hours of session; this log makes the gap visible up
    # front. ``set -a; source .env; set +a`` and ``export FOO=bar`` both
    # land in ``os.environ`` for this process; ``nohup`` does not strip
    # env.
    _log_judge_env_status()

    # 1b. Provenance check — refuse to run against drifted baseline (lessons §1).
    # The digest checked is the EVAL image's (plan.evaluation_sim_image), the
    # pristine scorer that stamps baseline.json — §3. By default this equals
    # sim_digest (eval == sim), so the §1.7 gate is unchanged; the signature of
    # _check_baseline_provenance is untouched (only the digest handed in moves).
    baseline_drift = _check_baseline_provenance(challenge, eval_digest)
    if baseline_drift:
        log.error("baseline.json provenance mismatch:\n  %s",
                  "\n  ".join(baseline_drift))
        log.error(
            "Refusing to run against a drifted baseline. "
            "Regenerate with `archbench baseline %s`.",
            challenge.challenge_dir,
        )
        return 2

    # 2. Output dir for this run
    out_dir = (
        results_root / challenge.id / run_name
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info("results dir: %s", out_dir)

    # Wrap orchestration in try/finally so cleanup runs even if
    # configure_simulator / verify / MCP-spawn raises. atexit is the
    # safety net; this is the explicit-order cleanup (sub-agent finding).
    sim: Optional[ContainerManager] = None
    # Multi-sim: extra sim containers, parallel to extra_sim_names. Declared
    # before the try so the finally block can tear them down even if a later
    # extra-sim start/verify raises (§1.8 — every container gets cleaned up).
    extra_sim_mgrs: list[ContainerManager] = []
    agent: Optional[ContainerManager] = None
    mcp_proc: Optional[subprocess.Popen] = None
    proxy_proc: Optional[subprocess.Popen] = None
    proxy_port: Optional[int] = None
    rc = 0
    # Failure-mode flags consumed in the finally block to derive a
    # distinct, post-hoc-meaningful rc (Bug 5 fix). The legacy behavior
    # was rc=0 unless a NotImplementedError was caught; any silent
    # round_timeout or non-zero claude exit registered as success.
    round_timeout_fired = False
    claude_nonzero_exit = False
    caught_exception = False

    try:
        # 3. Sim container (each `return rc` below already sets rc;
        # uncaught exceptions are caught in `except Exception` and set
        # rc=5 so session.json reflects the actual failure).
        sim_cfg = ContainerConfig.with_run_id(plan.simulator_image, "archbench_sim")
        sim = ContainerManager(sim_cfg)
        sim.start()
        errors = plugin.verify_simulator(sim)
        if errors:
            log.error("simulator verify failed:\n  %s", "\n  ".join(errors))
            return 3
        plugin.configure_simulator(sim, challenge)

        # 3a. Extra sim containers (multi-sim). Each gets its OWN per-run
        # uuid-named container (§1.8) + verify + configure via its plugin,
        # exactly like the primary above. No-op for single-sim challenges.
        # extra_sims feeds _start_mcp_server as (sim_name, plugin_name, mgr)
        # so the connector registers <sim>_-prefixed tools for each.
        extra_sims: list[tuple[str, str, ContainerManager]] = []
        for name in extra_sim_names:
            ep = extra_plugins[name]
            ecfg = ContainerConfig.with_run_id(ep.docker_image, f"archbench_sim_{name}")
            emgr = ContainerManager(ecfg)
            emgr.start()
            extra_sim_mgrs.append(emgr)
            eerrors = ep.verify_simulator(emgr)
            if eerrors:
                log.error("extra-sim %s verify failed:\n  %s",
                          name, "\n  ".join(eerrors))
                return 3
            ep.configure_simulator(emgr, challenge)
            extra_sims.append((name, name, emgr))

        # 3b. Start the host-side model proxy for byo_model runtimes.
        # Bundled runtimes hit the vendor API directly and DON'T need it.
        # Order: proxy first, then point runtime.llm_base_url at it so
        # verify_in_container's endpoint probe hits the proxy. The proxy
        # forwards to whichever backend routes.yaml chose.
        if runtime_type == "byo_model":
            proxy_port = _find_free_port()
            proxy_log_path = out_dir / "proxy.log"
            proxy_proc = _start_proxy_server(proxy_port, proxy_log_path)
            log.info(
                "model proxy starting on port %d (pid=%d), waiting for bind...",
                proxy_port, proxy_proc.pid,
            )
            if not _wait_for_port(proxy_port, proxy_proc, timeout=120):
                try:
                    err = proxy_log_path.read_text()[-3000:]
                except Exception:
                    err = "(no log)"
                raise RuntimeError(
                    f"model proxy didn't bind port {proxy_port} within 120s "
                    f"(pid={proxy_proc.pid}). log tail:\n{err}"
                )
            # --network host: container hits the proxy at host's localhost.
            proxy_url = f"http://127.0.0.1:{proxy_port}/v1"
            log.info("model proxy ready at %s (model=%s)", proxy_url, resolved_model)
            # Point the runtime at the proxy. mini/archharness's auth()
            # reads self.llm_base_url first, then falls back to env. We
            # also set the model so RuntimeAuth.env_vars carries it into
            # the container.
            if hasattr(runtime, "llm_base_url"):
                runtime.llm_base_url = proxy_url

        # 4. Agent container. agent_centric uses the runtime image;
        # simulator_centric swaps in a simulator-derived dev image
        # (plan.agent_image, via the _l2agent convention) with dependencies
        # installed. challenge_centric short-circuited above (NotImplementedError).
        agent_cfg = ContainerConfig.with_run_id(agent_image, "archbench_agent")
        agent = ContainerManager(agent_cfg)
        agent.start()
        errors = runtime.verify_in_container(agent)
        if errors:
            log.error("agent verify failed:\n  %s", "\n  ".join(errors))
            return 4

        # 4b. Stage challenge files via the runtime (P7 — runtime-owned).
        # `starter_visibility` is read off the Challenge dataclass (added by
        # A1 in this phase); challenges that don't set it default to 'full'
        # via the dataclass default, preserving legacy behavior.
        runtime.stage_workspace(
            agent,
            challenge,
            starter_visibility=getattr(challenge, "starter_visibility", "full"),
        )
        # 4c. Have the sim plugin push its workload artifacts (decoded
        # traces, etc.) into the agent — plugin knows what to ship and
        # where it lives in the sim image.
        plugin.export_workload_files(sim, agent, challenge)
        # Same for each extra sim (multi-sim). No-op for single-sim and for
        # sims whose plugin has a no-op export_workload_files (dramsys/ramulator).
        for (name, _pname, emgr) in extra_sims:
            extra_plugins[name].export_workload_files(emgr, agent, challenge)

        # 5. MCP server subprocess
        anon = Anonymizer.disabled()
        if anonymize and challenge.simulator == "champsim":
            from simulators.champsim.anonymization.build_anonymizer import (
                load_champsim_anonymizer,
            )
            anon = load_champsim_anonymizer()

        ctx = SubmitContext(
            challenge=challenge,
            challenge_dir=challenge.challenge_dir,
            plugin=plugin,
            agent=agent,
            sim=sim,
            anonymizer=anon,
            results_dir=out_dir,
        )
        port = _find_free_port()
        mcp_proc = _start_mcp_server(
            ctx, port, out_dir / "mcp.log", out_dir, extra_sims=extra_sims,
        )
        mcp_url = f"http://127.0.0.1:{port}/mcp"
        log.info("MCP server starting on %s (pid=%d), waiting for bind...",
                 mcp_url, mcp_proc.pid)
        # Wait for the MCP subprocess to actually bind. FastMCP + uvicorn
        # need ~1-3s cold; without this the agent's first MCPClient.connect
        # races and fails with Connection refused (caught in mini-smoke).
        if not _wait_for_port(port, mcp_proc, timeout=120):
            try:
                err = (out_dir / "mcp.log").read_text()[-3000:]
            except Exception:
                err = "(no log)"
            raise RuntimeError(
                f"MCP server didn't bind port {port} within 120s "
                f"(pid={mcp_proc.pid}). log tail:\n{err}"
            )
        log.info("MCP server ready on %s", mcp_url)

        # 6. Build prompt and run session
        from archbench.core.prompt_builder import PromptBuilder
        prompt = PromptBuilder.build_prompt_md(challenge, anonymizer=anon)

        rt_spec = challenge.runtime_for(runtime.name)
        # Push the challenge's submit budget onto the runtime instance so its
        # start_session() can pass the right --max-submits (mini's early-stop).
        # Mirrors the runtime.model push above; start_session()'s signature does
        # not carry the challenge, so we set an attribute. Without this the mini
        # runner's historical hardcoded `--max-submits 1` (a smoke-test leftover)
        # force-stops the agent after ONE submit on EVERY tier — silently
        # collapsing L1 (10) / L2 (5) / L3 (1) to a single submit each.
        try:
            runtime.max_submits = int(challenge.eval.max_submissions)
        except Exception:
            pass
        try:
            trajectory_path = runtime.start_session(
                agent=agent,
                mcp_url=mcp_url,
                prompt=prompt,
                round_timeout=rt_spec.round_timeout,
            )
        except NotImplementedError as e:
            log.error("runtime.start_session not implemented for %s: %s", runtime.name, e)
            rc = 5
        else:
            rc = 0
            out_traj = out_dir / "trajectory.jsonl"
            try:
                out_traj.write_text(Path(trajectory_path).read_text())
            except Exception as e:
                log.warning("could not persist trajectory: %s", e)

            # Canonical trajectory — the ONE schema analysis + trajectory-reading
            # evaluators consume. Convert AT RECORD TIME via the runtime's own
            # adapter (anti-corruption layer), so nothing downstream re-parses a
            # native format. A runtime without an adapter is logged + skipped
            # (the native trajectory.jsonl still lands; the run is unaffected).
            try:
                from archbench.core import trajectory as _canon
                _steps = runtime.to_canonical_trajectory(out_traj)
                _canon.write(
                    _canon.meta(run_name, runtime.name,
                                getattr(runtime, "model", None), challenge.id,
                                getattr(challenge, "tier_name", None)),
                    _steps, out_dir / "trajectory.canonical.jsonl",
                )
                log.info("wrote trajectory.canonical.jsonl (%d steps)", len(_steps))
            except NotImplementedError:
                log.info("runtime %s has no canonical-trajectory adapter; "
                         "skipping trajectory.canonical.jsonl", runtime.name)
            except Exception as e:
                log.warning("could not write canonical trajectory: %s", e)
            # Inspect runtime-side flags (Bug 5 fix): the runtime tells
            # us whether its subprocess timed out or exited non-zero so
            # we can pick a distinct rc instead of always claiming rc=0.
            # Runtimes that don't set these (legacy) leave them None.
            if getattr(runtime, "last_session_timed_out", False):
                round_timeout_fired = True
            elif getattr(runtime, "last_session_rc", 0) not in (0, None):
                claude_nonzero_exit = True
    except Exception as e:
        # Uncaught failure inside the orchestration block — make sure
        # session.json records the actual failure instead of falsely
        # claiming rc=0 in the finally block. Distinct from rc=5
        # (NotImplementedError) so post-mortem can tell apart "runtime
        # not implemented" from "anything else broke" (RuntimeError,
        # TimeoutError, ContainerDeadError, ...).
        log.exception("run_session raised: %s", e)
        caught_exception = True
        rc = 6
        raise
    finally:
        # 7a. Grace period for in-flight submits (Bug 3 fix; required for
        # Bug 1 async-submit refactor to not lose outcomes). The MCP
        # server writes submit_outcomes.jsonl on each completion; we
        # wait up to 1800s for the line count to catch up to the number
        # of submit threads still working. Skip if MCP never started.
        _wait_for_in_flight_submits(mcp_proc, out_dir, grace_seconds=1800)

        # 7b. Copy out /workspace/ from the agent container BEFORE
        # tearing it down (Bug 4 fix). Without this, the judge cannot
        # see the agent's deliverables.md files, tests, etc.
        if agent is not None:
            _copy_out_workspace(agent, out_dir)

        # 7b''. Replay trajectory.jsonl Write/Edit events to produce
        # per-turn workspace snapshots in
        # ``results/<challenge>/<run>/workspace_history/``. This is a
        # diagnostic aid: bisecting at the granularity of one tool call
        # when a run goes sideways. Best-effort -- any exception is
        # swallowed so a malformed trajectory never blocks teardown.
        try:
            from archbench.core.workspace_history import replay_workspace_history
            starter = {
                f"/workspace/{name}": content
                for name, content in (challenge.starter_code or {}).items()
            }
            snaps = replay_workspace_history(
                trajectory_path=out_dir / "trajectory.jsonl",
                starter_files=starter,
                out_dir=out_dir / "workspace_history",
            )
            log.info(
                "workspace_history: wrote %d snapshots to workspace_history/",
                snaps,
            )
        except Exception as e:
            log.warning("workspace_history failed (non-fatal): %s", e)

        # 7b''. MONITOR: distill token spend + wall-clock into profile.json
        # (CLAUDE.md cost-vs-benefit rule). Pure over the archive
        # (trajectory + submit_outcomes), best-effort so telemetry never
        # breaks teardown.
        try:
            from archbench.core.run_profile import write_profile
            if write_profile(out_dir):
                log.info("profile: wrote profile.json")
        except Exception as e:
            log.warning("profile write failed (non-fatal): %s", e)

        # 7b'. Post-session evaluators (declarative judge layer).
        # Runs AFTER workspace copy-out and BEFORE container teardown so
        # evaluators that need to re-run the sim can still talk to the
        # sim container if they want — currently none do, but the order
        # is the safer one. Each evaluator's failure is contained: log,
        # write a partial report, and continue to the next.
        _run_post_session_evaluators(challenge, out_dir)

        # 7c. Pick the post-hoc-meaningful rc (Bug 5 fix). Order matters:
        #   - rc=5 (NotImplementedError) was set explicitly inside try.
        #   - rc=6 (caught exception) was set in the except block.
        #   - rc=7 (claude exited non-zero, e.g. expired OAuth) ranks
        #     above rc=8 (round_timeout fired) — non-zero is the
        #     stronger signal of "agent died badly", while timeout is
        #     "agent ran out the clock".
        # If none of those apply, rc stays at whatever the try set (0).
        if rc == 0:
            if caught_exception:
                rc = 6
            elif claude_nonzero_exit:
                rc = 7
            elif round_timeout_fired:
                rc = 8

        # 7d. Save session metadata even on failure
        metadata = {
            "challenge_id": challenge.id,
            "runtime": runtime.name,
            "anonymize": anonymize,
            "run_name": run_name,
            "rc": rc,
            "provenance": {
                "image_digest_sim": sim_digest,
                "image_digest_agent": agent_digest,
                "harness_commit": git_head_commit(REPO_ROOT),
            },
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        try:
            (out_dir / "session.json").write_text(json.dumps(metadata, indent=2))
        except Exception:
            pass

        # 7e. eval_summary: roll up every eval_*.json + submit_outcomes.jsonl
        # + the session.json we just wrote into a single at-a-glance
        # report (eval_summary.json + eval_summary.md). Best-effort —
        # a summary write failure must not affect anything else.
        # Runs AFTER session.json so it can pick up rc / runtime /
        # harness_commit (would otherwise be None).
        try:
            _write_eval_summary(challenge, out_dir)
        except Exception as e:
            log.warning("eval_summary write failed: %s", e)

        # 8. Cleanup ladder (atexit is the safety net; explicit order is observable)
        # Order: MCP first (no more submits), then proxy (no more LLM
        # calls), then agent container (no more host:port consumers),
        # then sim. The proxy must outlive the agent's session so any
        # in-flight chat completion can finish writing back; we tear it
        # down here AFTER the agent stops accepting new requests.
        if mcp_proc is not None:
            mcp_proc.terminate()
            try:
                mcp_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                mcp_proc.kill()
        if agent is not None:
            agent.stop()
        if proxy_proc is not None:
            proxy_proc.terminate()
            try:
                proxy_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proxy_proc.kill()
        if sim is not None:
            sim.stop()
        # Extra sim containers (multi-sim). atexit (registered by
        # ContainerManager.start) is the unconditional safety net (§1.8);
        # this is the explicit-order teardown. Each stop is independent so
        # one failing doesn't strand the others.
        for emgr in extra_sim_mgrs:
            try:
                emgr.stop()
            except Exception as e:
                log.warning("extra-sim container stop failed: %s", e)
    return rc


def _wait_for_in_flight_submits(
    mcp_proc: Optional[subprocess.Popen],
    out_dir: Path,
    grace_seconds: int = 1800,
) -> None:
    """Wait until the MCP server's worker threads have all completed, or
    ``grace_seconds`` elapses.

    Signal: ``<out_dir>/.in_flight/<submission_id>`` marker files. The
    submit handler writes one when a submission is queued and removes it
    in the worker's ``finally`` block. The grace-period is drained when
    that directory is empty (or doesn't exist).

    Previous heuristic ("submit_outcomes.jsonl stable size for 10 s")
    was unsafe: if the worker hadn't yet finished its FIRST submission,
    the file didn't exist (size=0 forever) and we declared the queue
    drained after 10 s — tearing down a still-running 30 min sim. That
    bug invalidated branch_haiku in the Phase I run.
    """
    if mcp_proc is None or mcp_proc.poll() is not None:
        return
    inflight_dir = out_dir / ".in_flight"
    deadline = time.time() + grace_seconds
    log.info(
        "grace-period: waiting up to %ds for in-flight submits to flush",
        grace_seconds,
    )
    while time.time() < deadline:
        if mcp_proc.poll() is not None:
            log.info("grace-period: MCP server already exited; stopping wait")
            return
        # Empty / missing .in_flight/ means no worker thread holds a
        # submission. The submit handler removes its marker only after
        # the outcome is written to submit_outcomes.jsonl.
        try:
            pending = list(inflight_dir.iterdir()) if inflight_dir.exists() else []
        except OSError:
            pending = []
        if not pending:
            log.info("grace-period: no in-flight submits; releasing wait")
            return
        time.sleep(2.0)
    log.warning(
        "grace-period: %ds elapsed with %d submit(s) still in-flight; "
        "tearing down anyway (pending: %s)",
        grace_seconds, len(pending),
        ", ".join(p.name for p in pending),
    )


def _run_post_session_evaluators(challenge: "Challenge", out_dir: Path) -> None:
    """Iterate `challenge.evaluations`; write one eval_<name>.json per entry.

    Best-effort: an evaluator may raise (no API key, malformed config,
    re-eval timeout, …) but that must NOT crash the session-end path.
    On failure we still write an eval_<name>.json carrying ``error``
    and ``traceback`` so post-mortem can tell apart "didn't run" from
    "ran and failed".
    """
    evaluations = getattr(challenge, "evaluations", None) or []
    if not evaluations:
        return
    # Local import: importing the evaluator registry at module load time
    # would pull in optional deps (anthropic SDK) even for sessions that
    # don't need post-session evaluation.
    from archbench.evaluators import get_evaluator

    for ev_config in evaluations:
        name = ev_config.get("evaluator", "<unknown>")
        out_path = out_dir / f"eval_{name}.json"
        try:
            ev = get_evaluator(name)
            report = ev.evaluate(
                challenge, out_dir, ev_config.get("config", {}) or {},
            )
            out_path.write_text(json.dumps(report, indent=2, default=str))
            log.info("evaluator %s wrote %s", name, out_path.name)
        except Exception as e:
            log.error("evaluator %s failed: %s", name, e)
            try:
                out_path.write_text(json.dumps({
                    "ok": False,
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                    "evaluator": name,
                }, indent=2))
            except Exception:
                pass
            # Continue — one evaluator's failure must not block the others.

    # NOTE: eval_summary is written AFTER session.json lands (see
    # run_session step 7e). Done there, not here, so the summary picks
    # up rc / runtime / anonymize / harness_commit fields that aren't
    # known yet at this point in the lifecycle.


def _write_eval_summary(challenge: "Challenge", out_dir: Path) -> None:
    """Write ``results/<run>/eval_summary.{json,md}`` — one-glance roll-up.

    Reads every ``eval_<name>.json`` that the prior loop just wrote
    plus ``submit_outcomes.jsonl`` + ``session.json``, then groups
    findings by tier per ``challenge.rubric_mapping``. Two outputs:

      * ``eval_summary.json`` — structured, for downstream tooling
        (cross-model comparators, results dashboards).
      * ``eval_summary.md`` — human-readable table for ``cat`` /
        for pasting into reports.
    """
    # Per-evaluator extracted summaries. Keep this dispatch table small
    # and forgiving — an evaluator we don't recognize still gets a
    # generic {ok, error?} row instead of being dropped.
    eval_files = sorted(out_dir.glob("eval_*.json"))
    per_eval: dict[str, dict] = {}
    for f in eval_files:
        name = f.stem.removeprefix("eval_")
        try:
            blob = json.loads(f.read_text())
        except Exception as e:
            per_eval[name] = {"ok": False, "error": f"unreadable: {e}"}
            continue
        per_eval[name] = _summarize_evaluator(name, blob)

    # Final SIM_OK metric (if any) from submit_outcomes.jsonl
    submit_summary = _summarize_submits(out_dir / "submit_outcomes.jsonl")

    # Session metadata
    sess_path = out_dir / "session.json"
    session = {}
    if sess_path.exists():
        try:
            session = json.loads(sess_path.read_text())
        except Exception:
            pass

    # Group evaluators by tier. Default tier = 3 if not declared.
    rubric_mapping = getattr(challenge, "rubric_mapping", {}) or {}
    _int2word = {1: "basic", 2: "process", 3: "outcome"}
    buckets: dict[str, list[str]] = {"basic": [], "process": [], "outcome": []}
    for ev_name in per_eval:
        rs = rubric_mapping.get(ev_name, "outcome")
        if not isinstance(rs, list):
            rs = [rs]
        for r in rs:
            r = _int2word.get(r, r)  # tolerate legacy int
            if r in buckets:
                buckets[r].append(ev_name)

    summary = {
        "challenge_id": getattr(challenge, "id", session.get("challenge_id")),
        "run_name": session.get("run_name") or out_dir.name,
        "runtime": session.get("runtime"),
        "rc": session.get("rc"),
        "anonymize": session.get("anonymize"),
        "harness_commit": (session.get("provenance") or {}).get("harness_commit"),
        "submits": submit_summary,
        "rubric_basic": {n: per_eval[n] for n in buckets["basic"]},
        "rubric_process": {n: per_eval[n] for n in buckets["process"]},
        "rubric_outcome": {n: per_eval[n] for n in buckets["outcome"]},
    }

    (out_dir / "eval_summary.json").write_text(
        json.dumps(summary, indent=2, default=str),
    )
    (out_dir / "eval_summary.md").write_text(_render_eval_summary_md(summary))
    log.info("eval_summary: wrote eval_summary.{json,md}")


def _summarize_submits(jsonl_path: Path) -> dict:
    """Extract submit-count + final SIM_OK metric (if any) from submit_outcomes.jsonl."""
    if not jsonl_path.exists():
        return {"n_submits": 0, "outcomes": [], "final_sim_ok_metric": None}
    rows: list[dict] = []
    for line in jsonl_path.read_text().splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    outcomes = [r.get("outcome") for r in rows]
    sim_ok_with_metric = [r for r in rows if r.get("outcome") == "sim_ok" and r.get("metric")]
    final_metric = sim_ok_with_metric[-1].get("metric") if sim_ok_with_metric else None
    return {
        "n_submits": len(rows),
        "outcomes": outcomes,
        "final_sim_ok_metric": final_metric,
    }


def _summarize_evaluator(name: str, blob: dict) -> dict:
    """Per-evaluator one-line extract. Forgiving: unknown evaluators get a generic row.

    Returns a small dict with ``ok`` + the 1-3 most useful fields for
    that evaluator type. Full per-evaluator JSON is still at
    ``eval_<name>.json``; this is only the at-a-glance subset.
    """
    if not isinstance(blob, dict):
        return {"ok": False, "error": "not a dict"}
    ok = blob.get("ok", True)
    if name == "simulator_metric":
        return {
            "ok": ok,
            "source": blob.get("source"),
            "geomean_speedup": blob.get("geomean_speedup"),
            "baseline_average_ipc": blob.get("baseline_average_ipc"),
            "raw_metric": _pick(blob.get("raw_metric") or {},
                                ["ipc", "mpki", "speedup",
                                 "energy_per_inference_mJ", "total_cycles"]),
            "error": blob.get("error") if not ok else None,
        }
    if name == "deliverable_files":
        per_file = blob.get("per_file") or {}
        return {
            "ok": ok,
            "files_present": {k: v.get("exists") for k, v in per_file.items()},
            "judge_scores": {
                k: v.get("judge_score") for k, v in per_file.items()
            },
        }
    if name == "trajectory_audit":
        ts = blob.get("trajectory_summary") or {}
        checks = blob.get("checks") or {}
        return {
            "ok": ok,
            "turns": ts.get("total_assistant_turns"),
            "bash_calls": ts.get("bash_calls"),
            "other_tool_calls": ts.get("other_tool_calls"),
            "checks": {k: v.get("score") for k, v in checks.items()},
        }
    if name == "offline_sim_calibration":
        return {
            "ok": ok,
            "reason": blob.get("reason"),
            "sim_file": blob.get("sim_file"),
            "results": blob.get("results"),
        }
    if name == "objective_definition_quality":
        return {
            "ok": ok,
            "overall_score": blob.get("overall_score"),
            "sub_scores": blob.get("sub_scores"),
        }
    # Unknown evaluator → keep raw fields up to ~5 keys for visibility.
    return {"ok": ok, **{k: blob[k] for k in list(blob)[:5] if k != "ok"}}


def _pick(d: dict, keys: list[str]) -> dict:
    """Return d but only with the listed keys (skip missing)."""
    return {k: d[k] for k in keys if k in d}


def _render_eval_summary_md(s: dict) -> str:
    """Render the eval_summary dict into a markdown roll-up."""
    lines: list[str] = []
    lines.append(f"# Eval summary — {s.get('challenge_id')} / {s.get('run_name')}")
    lines.append("")
    lines.append(f"- runtime: `{s.get('runtime')}`")
    lines.append(f"- rc: `{s.get('rc')}`")
    lines.append(f"- anonymize: `{s.get('anonymize')}`")
    lines.append(f"- harness_commit: `{(s.get('harness_commit') or '')[:12]}`")
    lines.append("")

    submits = s.get("submits") or {}
    lines.append("## Submits")
    lines.append("")
    lines.append(f"- count: {submits.get('n_submits')}")
    lines.append(f"- outcomes: `{submits.get('outcomes')}`")
    fm = submits.get("final_sim_ok_metric")
    if fm:
        flat = {k: v for k, v in fm.items() if not isinstance(v, (list, dict))}
        lines.append(f"- final SIM_OK metric: `{flat}`")
    else:
        lines.append("- final SIM_OK metric: (none — no SIM_OK row)")
    lines.append("")

    for level, label in [("basic", "Rubric — Basic"), ("process", "Rubric — Process"),
                         ("outcome", "Rubric — Outcome")]:
        bucket = s.get(f"rubric_{level}") or {}
        if not bucket:
            continue
        lines.append(f"## {label}")
        lines.append("")
        for ev_name, summary in bucket.items():
            lines.append(f"### {ev_name}")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(summary, indent=2, default=str))
            lines.append("```")
            lines.append("")
    return "\n".join(lines) + "\n"


def _copy_out_workspace(agent: ContainerManager, out_dir: Path) -> None:
    """Pull /workspace/ from the agent container into results/<run>/workspace/.

    Best-effort: a copy failure must not break the session-end path
    (atexit must still run). Logs at WARN on failure.
    """
    try:
        dest = out_dir / "workspace"
        # Remove any partial prior content so the copy is observable
        if dest.exists():
            import shutil as _sh
            _sh.rmtree(dest, ignore_errors=True)
        agent.copy_out("/workspace/.", dest)
        log.info("workspace copied out to %s", dest)
    except Exception as e:
        log.warning("copy-out /workspace/ failed: %s", e)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, proc: subprocess.Popen, timeout: int = 120) -> bool:
    """Poll until TCP `port` accepts a connection, or `proc` dies, or
    `timeout` seconds elapse. Returns True iff the port came up."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.5)
    return False


def _log_judge_env_status() -> None:
    """Log presence/absence of judge-auth env vars at session start.

    The post-session evaluators are in-process (see
    ``_run_post_session_evaluators``) and read ``os.environ`` directly
    via ``archbench.evaluators.base.run_judge``. So whatever lives in the
    parent ``archbench run`` process's environment is what the judge sees.

    We surface presence at INFO and absence at WARN so the user knows
    *before* spending hours on a session whether the judge will work.
    """
    ak_present = bool(os.environ.get("ANTHROPIC_API_KEY"))
    jm_present = bool(os.environ.get("ARCHBENCH_JUDGE_MODEL"))
    if ak_present:
        log.info("judge-env: ANTHROPIC_API_KEY=present, ARCHBENCH_JUDGE_MODEL=%s",
                 "present" if jm_present else "missing")
    elif jm_present:
        log.info("judge-env: ANTHROPIC_API_KEY=missing, ARCHBENCH_JUDGE_MODEL=present "
                 "(judge will use ARCHBENCH_PROXY_URL fallback)")
    else:
        log.warning(
            "judge-env: ANTHROPIC_API_KEY=missing AND ARCHBENCH_JUDGE_MODEL=missing — "
            "post-session evaluators that call run_judge() will degrade to "
            "score=None. Export ANTHROPIC_API_KEY (`export ANTHROPIC_API_KEY=...`) "
            "or set ARCHBENCH_JUDGE_MODEL before `archbench run` to enable the judge."
        )


def _child_env() -> dict[str, str]:
    """Environment for harness subprocesses (MCP server, proxy, ...).

    Phase B fix: explicit env-propagation. The legacy implicit-inherit
    behavior of ``subprocess.Popen`` works UNLESS something else is
    passing ``env=<subset>`` upstream — which is exactly what happened
    in the Gemma 4 run, where ``ANTHROPIC_API_KEY`` (judge auth) and
    ``ARCHBENCH_JUDGE_MODEL`` reached the user's shell but never made it to
    the in-process evaluator imports, surfacing as
    ``judge: no LLM backend configured``.

    We forward the FULL parent env so judges, proxies, and the MCP
    server all see the same secrets the user exported. If you need to
    suppress something (e.g. a stale credential), unset it at the
    shell level; do not add an opt-out here.
    """
    return os.environ.copy()


def _start_mcp_server(
    ctx: SubmitContext, port: int, log_path: Path,
    results_dir: Optional[Path] = None,
    extra_sims: Optional[list[tuple[str, str, "ContainerManager"]]] = None,
) -> subprocess.Popen:
    """Spawn the MCP server in a subprocess.

    `results_dir` is forwarded as --results-dir; the server uses it to
    persist submit_outcomes.jsonl + session_end.requested. Without it,
    the async-submit refactor's outcome capture is a no-op.

    `extra_sims` binds ADDITIONAL simulator containers to the same MCP
    server for a multi-sim session (see ``docs/multi_sim_design.md``).
    Each entry is ``(sim_name, simulator_plugin, container_manager)``. The
    primary sim comes from ``ctx`` (sim_name defaults to its plugin name);
    extras are appended. When more than one sim is bound the server
    namespaces every sim's tools with a ``<sim_name>_`` prefix; with the
    single sim from ``ctx`` (the only thing the harness passes today) the
    tools keep their bare canonical names — byte-for-byte unchanged.

    Env is explicitly forwarded (see ``_child_env``) so the MCP-side
    handlers — and any in-process judge calls they make — see
    ANTHROPIC_API_KEY and friends.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, "w")
    # (sim_name, simulator_plugin, container_name) per bound sim. The
    # connector pairs --simulator / --sim-container / --sim-name positionally,
    # so they must be emitted in lockstep.
    sims: list[tuple[str, str, str]] = [
        (ctx.challenge.simulator, ctx.challenge.simulator, ctx.sim.name),
    ]
    for sim_name, simulator, sim_mgr in (extra_sims or []):
        sims.append((sim_name, simulator, sim_mgr.name))
    cmd = [
        sys.executable, "-m", "simulators.champsim.connector.server_subprocess",
        "--port", str(port),
        "--challenge-dir", str(ctx.challenge_dir),
        "--agent-container", ctx.agent.name,
        "--anonymize", str(ctx.anonymizer.enabled),
    ]
    for sim_name, simulator, container_name in sims:
        cmd += [
            "--sim-name", sim_name,
            "--simulator", simulator,
            "--sim-container", container_name,
        ]
    if results_dir is not None:
        cmd += ["--results-dir", str(results_dir)]
    # Per-tier MCP tool allowlist (Challenge.tier_tools). None/empty = all
    # tools (back-compat). L2 passes the Oracle + lifecycle tools only.
    for tool in (getattr(ctx.challenge, "tier_tools", None) or []):
        cmd += ["--tool", str(tool)]
    return subprocess.Popen(cmd, stdout=log_fh, stderr=log_fh, env=_child_env())


def _start_proxy_server(port: int, log_path: Path) -> subprocess.Popen:
    """Spawn the model proxy (archbench.serving.proxy) in a subprocess.

    Pattern mirrors `_start_mcp_server`: stdout/stderr → `<results>/proxy.log`,
    parent registers cleanup in the run_session finally block. The proxy
    reads `archbench/serving/routes.yaml` from its bundled path; no extra config
    flags needed for the MVP.

    Env is explicitly forwarded (see ``_child_env``) so API-backed
    backends in routes.yaml (OPENAI_API_KEY, etc) see the credentials.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, "w")
    cmd = [
        sys.executable, "-m", "archbench.serving.cli",
        "--port", str(port),
    ]
    return subprocess.Popen(cmd, stdout=log_fh, stderr=log_fh, env=_child_env())


def _check_baseline_provenance(
    challenge, current_sim_digest: str,
) -> list[str]:
    """Compare baseline.json's stamped provenance to live state.

    Compares ALL FOUR sha fields (image_digest, config_sha256,
    starter_sha256, trace_sha256) — image-only check would miss config /
    starter / trace swaps that also invalidate baseline numbers
    (sub-agent §1 finding).

    Hard RED on baseline lacking a `provenance` block — the legacy
    state of unstamped baselines is exactly the bug class lessons §1
    structurally rules out.

    Missing baseline.json file → silent pass (no baseline to compare).
    """
    if not challenge.challenge_dir:
        return []
    # Tier-aware resolution: tier-mode challenges keep baseline.json under
    # ``<family>/common/evaluation/`` (NOT under the per-tier dir). Using
    # the shared resolver means legacy 3-subdir challenges resolve to the
    # exact same path as before, while tier-mode challenges now hit the
    # real baseline rather than a non-existent per-tier file (which
    # silently bypassed the provenance gate — audit finding 2026-05-31).
    sim_dir, eval_dir, _ = resolved_dirs(challenge)
    baseline_path = eval_dir / Path(challenge.eval.baseline_file).name
    if not baseline_path.exists():
        # Legacy fallbacks: the path declared in challenge.yaml may still
        # point at the pre-Phase-H ``baseline/`` location, or at the
        # challenge root directly.
        alt = str(challenge.eval.baseline_file).replace(
            "baseline/", "evaluation/", 1,
        )
        alt_path = challenge.challenge_dir / alt
        if alt_path.exists():
            baseline_path = alt_path
        else:
            legacy_path = challenge.challenge_dir / challenge.eval.baseline_file
            if legacy_path.exists():
                baseline_path = legacy_path
            else:
                return []
    try:
        baseline = json.loads(baseline_path.read_text())
    except Exception as e:
        return [f"baseline.json unreadable: {e}"]
    if "provenance" not in baseline:
        return [
            "baseline.json is missing the `provenance` block — refusing to "
            "compare against an unstamped baseline (lessons §1). Regenerate it "
            "with `archbench baseline <challenge_dir>` — the unified, sim-"
            "agnostic path that runs evaluate.sh and stamps the provenance "
            "4-tuple."
        ]
    try:
        baseline_prov = Provenance.from_dict(baseline["provenance"])
    except Exception as e:
        return [f"baseline provenance unreadable: {e}"]

    drifts: list[str] = []
    # 1. Image digest
    if baseline_prov.image_digest != current_sim_digest:
        drifts.append(
            f"image_digest: baseline={baseline_prov.image_digest[:24]}…, "
            f"current={current_sim_digest[:24]}…"
        )

    # 2. Config sha — re-hash the live config.json. Tier-aware: use the
    # resolved sim_dir (which points at ``<family>/common/simulator/``
    # for tier mode and ``<challenge>/simulator/`` for legacy). Fall back
    # to the older ``eval/`` and challenge-root locations for the oldest
    # layouts.
    config_path = sim_dir / "config.json"
    if not config_path.exists():
        config_path = challenge.challenge_dir / "eval" / "config.json"
    if not config_path.exists():
        config_path = challenge.challenge_dir / "config.json"
    if config_path.exists():
        live_config = sha256_of_file(config_path)
        if baseline_prov.config_sha256 != live_config:
            drifts.append(
                f"config_sha256: baseline={baseline_prov.config_sha256[:24]}…, "
                f"current={live_config[:24]}…"
            )

    # 3. Starter sha — re-hash all starter files in canonical order.
    # Phase H layout: starter/ lives at challenge/starter/. Legacy: at
    # the challenge root. Pick whichever exists.
    #
    # Phase B (tiered challenges, 2026-05-31): a tier with
    # `starter_visibility: 'none'` ships NO scaffold to the agent, so
    # comparing the baseline's starter_sha256 against a non-existent
    # live starter is degenerate. Skip the starter check in that case
    # (the other three sha fields still run, preserving §1.7).
    starter_visibility = getattr(challenge, "starter_visibility", "full")
    reference_dir = getattr(challenge, "reference_dir", None)
    if reference_dir is not None and Path(reference_dir).is_dir():
        # Protocol v2 (2026-06-11): the baseline is stamped from the family
        # REFERENCE implementation (challenge.reference_dir — today
        # <family>/assisted/L1/starter), while the STAGED starter is the
        # unified family skeleton for every tier. Hashing the staged skeleton
        # against baseline.starter_sha256 would be a guaranteed false drift.
        # Re-hash the reference instead (the same pair doctor E3 verifies),
        # preserving the §1.7 4-tuple for every tier.
        from archbench.core.provenance import starter_dir_sha256
        live_starter = starter_dir_sha256(Path(reference_dir))
        if baseline_prov.starter_sha256 != live_starter:
            drifts.append(
                f"starter_sha256 (family reference): "
                f"baseline={baseline_prov.starter_sha256[:24]}…, "
                f"current={live_starter[:24]}…"
            )
    elif starter_visibility in ("none", "api_stub"):
        # `none` ships no scaffold; `api_stub` ships only a throwaway schema
        # stub the agent is told to replace. In BOTH cases the staged starter
        # is NOT the baseline's reference design — the baseline is a family-level
        # constant measured on the canonical full-scaffold reference (e.g. NACIM
        # for gibbon_codesign), so hashing the placeholder against the baseline's
        # starter_sha256 is degenerate (and would force the baseline to be
        # measured on the stub -> a 1e30 sentinel denominator). Skip the starter
        # check; the other three sha fields still run, preserving §1.7. See
        # lessons §22.
        log.warning(
            "skipping starter_sha256 provenance check: challenge "
            "starter_visibility=%r (no meaningful reference scaffold shipped "
            "to agent); image_digest + config_sha256 + trace_sha256 + "
            "harness_commit still verified.", starter_visibility,
        )
    else:
        # Tier mode: starter_dir resolves under tiers/<L>/starter via
        # resolved_dirs (see Phase A1 + path_resolution port). Legacy 3-subdir:
        # falls back to challenge_dir/challenge/starter then challenge_dir/starter.
        _sim_dir_unused, _eval_dir_unused, starter_dir = resolved_dirs(challenge)
        if starter_dir.is_dir():
            live_starter = sha256_of_bytes(b"".join(
                f.name.encode() + b":" + sha256_of_file(f).encode() + b"\n"
                for f in sorted(starter_dir.iterdir()) if f.is_file()
            ))
            if baseline_prov.starter_sha256 != live_starter:
                drifts.append(
                    f"starter_sha256: baseline={baseline_prov.starter_sha256[:24]}…, "
                    f"current={live_starter[:24]}…"
                )

    # 4. Trace sha — re-hash all per_trace files referenced in baseline.json.
    # `per_trace` is champsim's LIST of {"trace": name, ...} carrying rehashable
    # .champsimtrace.xz files. Other sims (ramulator/mnsim/...) bake their traces
    # into the sim image and either omit per_trace or use a DICT keyed by trace
    # name for reporting — those have no host trace files (trace_sha256 = 0), so
    # skip anything that isn't a champsim-style list entry (else iterating a dict
    # yields str keys and `t["trace"]` raises — broke every dict-per_trace family).
    _pt = baseline.get("per_trace", [])
    trace_names = [
        t["trace"] + ".champsimtrace.xz"
        for t in (_pt if isinstance(_pt, list) else [])
        if isinstance(t, dict) and "trace" in t
    ]
    # subtraces/ may live under simulator/ (Phase H), eval/ (legacy), or
    # at the challenge root (oldest layout). Some challenges (btb_design,
    # branch_predictor_design) use full SPEC traces from the repo's
    # workload_pools/champsim/ pool instead of per-challenge subtraces —
    # fall back to that if a name is missing from subtraces/ (matches
    # simulate.sh's resolution order).
    # Prefer the RESOLVED simulator dir (tier-aware: may be <family>/common/
    # or <family>/simulator) — the single source of truth, matching cli.py's
    # baseline path. Keep the legacy hand-constructed paths as fallbacks.
    _sim_dir, _, _ = resolved_dirs(challenge)
    subtraces_candidates = [
        _sim_dir / "subtraces",
        challenge.challenge_dir / "simulator" / "subtraces",
        challenge.challenge_dir / "eval" / "subtraces",
        challenge.challenge_dir / "subtraces",
    ]
    subtraces_dir = next(
        (d for d in subtraces_candidates if d.is_dir()),
        subtraces_candidates[0],
    )
    # repo root via the challenges/ component — challenge_dir.parents[1] is
    # WRONG for assisted tiers (challenges/<fam>/assisted/<L> -> lands on the
    # family dir); that bug falsely refused every assisted-tier champsim run.
    workload_pool_dir = (
        repo_root_from_challenge_dir(challenge.challenge_dir)
        / "workload_pools" / "champsim"
    )

    def _resolve_trace(tn: str) -> Path:
        sub = subtraces_dir / tn
        if sub.is_file():
            return sub
        pool = workload_pool_dir / tn
        if pool.is_file():
            return pool
        raise FileNotFoundError(f"trace not in subtraces/ or workload_pools/: {tn}")

    if trace_names and (subtraces_dir.is_dir() or workload_pool_dir.is_dir()):
        try:
            live_trace = sha256_of_bytes(b"".join(
                tn.encode() + b":" + sha256_of_file(_resolve_trace(tn)).encode() + b"\n"
                for tn in trace_names
            ))
            if baseline_prov.trace_sha256 != live_trace:
                drifts.append(
                    f"trace_sha256: baseline={baseline_prov.trace_sha256[:24]}…, "
                    f"current={live_trace[:24]}…"
                )
        except FileNotFoundError as e:
            drifts.append(f"trace_sha256: cannot rehash — {e}")

    return drifts
