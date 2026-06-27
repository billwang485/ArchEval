"""ChampSimPlugin — SimulatorPlugin for the ChampSim trace-driven CPU sim.

Notable changes vs. the legacy harness plugin:

1. `cleanup_simulator()` calls the in-container `/work/cleanup.sh`
   instead of duplicating the cleanup logic in Python. Past bug: two
   independent implementations drifted, and the Python one masked the
   shell one's exit codes. (See `docs/lessons_learned.md` — the
   structural fix is one cleanup script, called from both places.)

2. `verify_simulator()` calls `/work/verify.sh`, the same script
   the Dockerfile RUNs at bake time and the `archbench verify-all` CLI
   uses. One verifier, three call sites — they can't drift.

3. No internal anonymization. The connector's `Anonymizer` handles all
   trace-name scrubbing; the plugin sees and emits real names. Cleaner
   separation; impossible to forget a "scrub here too" site.

4. `run_submit()` dispatches to either the challenge's `evaluate.sh`
   (multi-workload host-side parallel) or `simulate.sh` (single-trace
   long-lived sim container), picking up the existing pattern from
   the legacy `cache_replacement_fast` vs `cache_replacement` split.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from archbench.core.path_resolution import resolved_dirs
from archbench.core.plugin_base import SimulatorPlugin

if TYPE_CHECKING:
    from archbench.core.challenge import Challenge
    from archbench.core.container import ContainerManager

log = logging.getLogger("archbench.champsim")

CHAMPSIM_DIR = "/work/runtimes/champsim"


class ChampSimPlugin(SimulatorPlugin):

    @property
    def name(self) -> str:
        return "champsim"

    @property
    def docker_image(self) -> str:
        from archbench.image_management import manifest as images
        return images.fully_qualified("simulators", self.name)

    # Scripts the plugin expects in /work/. The :v6 image as built
    # from the LEGACY Dockerfile shipped build_and_run.sh + cleanup.sh
    # but NOT the new verify.sh. We patch them in on first verify so we
    # don't force a 30-min image rebuild during the open-ended rewrite.
    # Once :v6 is rebuilt from this repo's Dockerfile (which COPYs all
    # three), this patching becomes a fast no-op.
    _IN_CONTAINER_SCRIPTS = ("verify.sh", "cleanup.sh", "build_and_run.sh")

    # ---- lifecycle ----------------------------------------------------------

    def verify_simulator(self, sim: ContainerManager) -> list[str]:
        """Self-heal missing scripts, then call the in-container verifier."""
        self._ensure_scripts_present(sim)
        out, rc = sim.exec("/work/verify.sh", timeout=30)
        if rc == 0 and "VERIFY_OK" in out:
            return []
        errors = [
            line.strip() for line in out.splitlines()
            if "CHECK_FAILED" in line or "VERIFY_FAILED" in line
        ]
        if not errors:
            errors = [f"verify.sh rc={rc}: {out[:500]}"]
        return errors

    def cleanup_simulator(self, sim: ContainerManager) -> None:
        """Reset the sim container; raise on failure (no silent best-effort)."""
        self._ensure_scripts_present(sim)
        out, rc = sim.exec("/work/cleanup.sh", timeout=60)
        if rc != 0 or "CLEANUP_OK" not in out:
            raise RuntimeError(
                f"ChampSim cleanup failed (rc={rc}):\n{out}"
            )

    def _ensure_scripts_present(self, sim: ContainerManager) -> None:
        """Inject any missing /work/*.sh from the host. Idempotent.

        Resolves host scripts from this plugin's own directory
        (`simulators/champsim/`) — Dockerfile + scripts + Python code
        all colocate per the per-image organization scheme.
        """
        host_docker_dir = Path(__file__).resolve().parent
        for script in self._IN_CONTAINER_SCRIPTS:
            out, _ = sim.exec(
                f"test -x /work/{script} && echo OK || echo MISSING",
                timeout=10,
            )
            if "OK" in out:
                continue
            host_path = host_docker_dir / script
            if not host_path.exists():
                # Fall through — verify.sh will surface this as a CHECK_FAILED
                continue
            sim.copy_in(host_path, f"/work/{script}")
            sim.exec(f"chmod +x /work/{script}", timeout=10)

    def configure_simulator(
        self, sim: ContainerManager, challenge: Challenge,
    ) -> None:
        """One-time setup: copy config.json + starter + traces to sim, run config.sh.

        Per-submit code injection happens in run_submit. Traces are staged
        once here, not per-submit (they don't change). Required when the
        challenge uses chunked sub-traces (cache_replacement_fast etc.)
        that aren't baked into the :v6 image.
        """
        sim_dir, eval_dir, _starter_dir = resolved_dirs(challenge)

        # 0. Ensure the workload-pool dirs exist before any copy_in. On a
        # fresh (non-baked) image these don't exist, and `podman cp <src>
        # <ctr>:/work/workload_pools/champsim/<f>` fails with "could not be
        # found on container" when the PARENT dir is missing. The baked
        # image shipped these dirs; the reload-from-tarball path does not.
        # (lessons_learned.md §17.)
        sim.exec("mkdir -p /work/workload_pools/champsim/decoded", timeout=10)

        # 1. Copy challenge config.json into the sim container.
        # Phase H+ / tier mode: config.json under simulator/ (resolved by
        # `resolved_dirs` — for tier-mode this points at <family>/common/
        # simulator). Legacy fallback to eval_dir is preserved for the
        # transitional eval/config.json path.
        for config_json in (sim_dir / "config.json", eval_dir / "config.json"):
            if config_json.exists():
                sim.copy_in(config_json, "/tmp/config_challenge.json")
                break

        # 2. Stage any per-challenge trace files (chunked or otherwise) that
        # aren't baked into the image. Look in `subtraces/` then `traces/`
        # under simulator/ (resolved) and evaluation/ (legacy fallback), then
        # the repo's host-side full-trace pool `workload_pools/champsim/`.
        # The pool is found by WALKING UP from sim_dir (its depth differs
        # between legacy <id>/simulator/ and tier <family>/common/simulator/,
        # so a fixed parents[N] is wrong) and honors $ARCHBENCH_WORKLOADS_DIR.
        import os as _os
        host_pool = None
        _env_pool = _os.environ.get("ARCHBENCH_WORKLOADS_DIR")
        if _env_pool and (Path(_env_pool) / "champsim").is_dir():
            host_pool = Path(_env_pool) / "champsim"
        else:
            _d = sim_dir
            for _ in range(6):
                _d = _d.parent
                if (_d / "workload_pools" / "champsim").is_dir():
                    host_pool = _d / "workload_pools" / "champsim"
                    break

        sim_cfg = challenge.simulator_config or {}
        traces = sim_cfg.get("traces") or ([sim_cfg["trace"]] if sim_cfg.get("trace") else [])
        for trace_name in traces:
            host_path = None
            cands = [sim_dir / "subtraces" / trace_name,
                     sim_dir / "traces" / trace_name,
                     eval_dir / "subtraces" / trace_name,
                     eval_dir / "traces" / trace_name]
            if host_pool is not None:
                cands.append(host_pool / trace_name)
            for cand in cands:
                if cand.exists():
                    host_path = cand
                    break
            if host_path is None:
                # Trace might already be baked into the image; check.
                out, _ = sim.exec(
                    f"test -f /work/workload_pools/champsim/{trace_name} && echo OK || echo MISS",
                    timeout=10,
                )
                if "OK" in out:
                    continue
                raise FileNotFoundError(
                    f"Trace {trace_name!r} not in {sim_dir}/subtraces/, "
                    f"{sim_dir}/traces/, {eval_dir}/subtraces/, "
                    f"{eval_dir}/traces/, {host_pool or '<no workload_pools/champsim found>'}, "
                    f"or baked in image (set ARCHBENCH_WORKLOADS_DIR)"
                )
            log.info("staging trace into sim container: %s", trace_name)
            sim.copy_in(host_path, f"/work/workload_pools/champsim/{trace_name}")

        # 2. Drop starter files into /tmp/ so config.sh sees them when it
        # auto-discovers components (legacy behavior preserved).
        for fname, code in challenge.starter_code.items():
            sim.write_file(f"/tmp/{fname}", code)

        # 3. Run config.sh on the challenge config.
        components = self._components(challenge)
        setup_lines = ["set -e", f"cd {CHAMPSIM_DIR}"]
        for comp_dir, comp_name in components:
            setup_lines.append(f"mkdir -p {comp_dir}/{comp_name}")
            # Copy any matching starter files into the component dir
            setup_lines.append(
                f"for f in /tmp/{comp_name}.h /tmp/{comp_name}.cc; do "
                f"[ -f \"$f\" ] && cp \"$f\" {comp_dir}/{comp_name}/; done"
            )
        setup_lines += [
            "cp /tmp/config_challenge.json config_challenge.json",
            "rm -rf .csconfig _configuration.mk obj .depend bin",
            "./config.sh --compile-all-modules config_challenge.json 2>&1",
            "echo CONFIGURE_OK",
        ]
        out, rc = sim.exec("\n".join(setup_lines), workdir=CHAMPSIM_DIR, timeout=180)
        if rc != 0 or "CONFIGURE_OK" not in out:
            raise RuntimeError(
                f"ChampSim configure failed (rc={rc}):\n{out[-2000:]}"
            )

    # ---- the submit dispatch ------------------------------------------------

    def run_submit(
        self, sim: ContainerManager, challenge: Challenge,
        agent_files: dict[str, str],
    ) -> str:
        """Build + run one submit. Returns raw stdout for parse_output.

        Two dispatch modes, picked by what's in the challenge dir:

        - **evaluate.sh present**: multi-workload, host-side parallel mode.
          We hand evaluate.sh a tmpdir of agent files; it spawns its own
          per-workload containers and aggregates. Used by cache_replacement_fast.

        - **simulate.sh present**: single-container mode. Code is copied
          into /work/submission/ of our long-lived `sim` container;
          simulate.sh's --container flag reuses it instead of spinning a
          fresh container per submit.

        Raises subprocess.TimeoutExpired or RuntimeError; the connector
        translates those to typed `SubmitOutcome`.
        """
        sim_dir, eval_dir, _starter_dir = resolved_dirs(challenge)
        # Phase H+ / tier mode: evaluate.sh under evaluation/ (resolved),
        # simulate.sh under simulator/ (resolved). `resolved_dirs` already
        # encapsulates the legacy 3-subdir vs tier <family>/common/ split,
        # so no manual probing of both layouts here.
        evaluate_sh = eval_dir / "evaluate.sh"
        simulate_sh = sim_dir / "simulate.sh"

        if not evaluate_sh.exists() and not simulate_sh.exists():
            raise RuntimeError(
                f"Challenge {challenge.id!r} has neither {evaluate_sh} nor "
                f"{simulate_sh} — cannot dispatch submit."
            )

        with tempfile.TemporaryDirectory(prefix="archbench_champsim_") as tmp:
            tmp_path = Path(tmp)
            for fname, content in agent_files.items():
                (tmp_path / fname).write_text(content)

            if evaluate_sh.exists():
                log.info("dispatch: evaluate.sh (multi-workload, host-side)")
                cmd = ["bash", str(evaluate_sh), str(tmp_path)]
            else:
                log.info(
                    "dispatch: simulate.sh --container %s (single-trace)", sim.name,
                )
                # simulate.sh's --container mode expects code at
                # /work/submission/ in the sim container; ship it now.
                sim.exec(
                    "mkdir -p /work/submission && rm -rf /work/submission/*",
                    timeout=10,
                )
                for fname in agent_files:
                    sim.copy_in(tmp_path / fname, f"/work/submission/{fname}")
                cmd = [
                    "bash", str(simulate_sh), str(tmp_path),
                    "--container", sim.name,
                ]
            # 30 min cap on per-submit sim time; decouples from the
            # agent's round_timeout (typically 7200s). ChampSim 6-trace
            # parallel runs land under 10 min in practice, so 1800s is
            # comfortable headroom. The connector converts a
            # TimeoutExpired into SIM_TIMEOUT.
            result = subprocess.run(
                cmd,
                capture_output=True, text=True,
                timeout=1800,
            )
            log.info(
                "submit dispatch rc=%d (stdout %d B, stderr %d B)",
                result.returncode, len(result.stdout), len(result.stderr),
            )
            return result.stdout + result.stderr

    # ---- parsing ------------------------------------------------------------

    def parse_output(self, raw_output: str) -> Optional[dict]:
        """Extract metrics from build_and_run.sh / simulate.sh / evaluate.sh stdout.

        Three formats supported (auto-detected):

        1. `ARCHBENCH_JSON_START`/`END`-wrapped blocks (build_and_run.sh,
           evaluate.sh via aggregate.py). Multi-block → average + per-trace.
        2. Bare ChampSim JSON dump anywhere in stdout (simulate.sh, which
           just `cat`s /tmp/result.json without wrapping). Found by
           locating the first `[{"name":"Simulation"` token.
        3. Bare ChampSim JSON object form `{"roi": ...}`.

        Returns None on parse failure (caller converts to BUILD_FAIL or
        SIM_TIMEOUT depending on the markers in the raw output).
        """
        # Guard: truncation through a JSON block makes parsing unsafe.
        if "[truncated" in raw_output:
            trunc_pos = raw_output.find("[truncated")
            last_json_end = raw_output.rfind("ARCHBENCH_JSON_END")
            if last_json_end < 0 or trunc_pos < last_json_end:
                return None

        # Strategy 1: ARCHBENCH_JSON_START/END markers
        json_blocks = self._extract_json_blocks(raw_output)

        # Strategy 2/3: bare ChampSim JSON (simulate.sh output)
        if not json_blocks:
            blocks = self._extract_bare_champsim_json(raw_output)
            if blocks:
                json_blocks = blocks

        if not json_blocks:
            return None

        per_trace = [
            m for b in json_blocks
            if (m := self._parse_single_result(b)) is not None
        ]
        if not per_trace:
            return None
        if len(per_trace) == 1:
            return per_trace[0]
        return self._average_metrics(per_trace)

    @staticmethod
    def _extract_json_blocks(raw_output: str) -> list[dict]:
        """Find every block bracketed by ARCHBENCH_JSON_START/END."""
        blocks: list[dict] = []
        start_marker = "ARCHBENCH_JSON_START"
        end_marker = "ARCHBENCH_JSON_END"
        pos = 0
        while True:
            si = raw_output.find(start_marker, pos)
            if si < 0:
                break
            ei = raw_output.find(end_marker, si + 1)
            if ei < 0:
                break
            try:
                blocks.append(json.loads(
                    raw_output[si + len(start_marker):ei].strip()
                ))
            except json.JSONDecodeError:
                pass
            pos = ei + len(end_marker)
        return blocks

    @staticmethod
    def _extract_bare_champsim_json(raw_output: str) -> list[dict]:
        """Bare ChampSim JSON: simulate.sh just `cat`s /tmp/result.json.

        Scan for a `[{` or `{"roi"` that starts a balanced JSON token.
        Returns at most one block (per-trace simulate.sh runs only ever
        emit one result file).

        Whitespace tolerance: btb_design / branch_predictor_design
        aggregate.py emits ``json.dumps(..., indent=2)`` so the leading
        ``[`` and ``{`` are separated by newlines and spaces. Use regex
        anchors to find the start position, then balanced-brace scan
        from there.
        """
        import re

        regex_anchors = [
            r'\[\s*\{\s*"name"\s*:\s*"Simulation"',
            r'\[\s*\{\s*"name"\s*:\s*"AggregatedMultiWorkload"',
            r'\[\s*\{\s*"roi"',
            r'\{\s*"roi"',
        ]
        candidates: list[int] = []
        for pat in regex_anchors:
            m = re.search(pat, raw_output)
            if m:
                candidates.append(m.start())
        candidates.sort()
        for start in candidates:
            depth = 0
            in_str = False
            esc = False
            for i, c in enumerate(raw_output[start:], start):
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
                            return [json.loads(raw_output[start:i + 1])]
                        except json.JSONDecodeError:
                            break
            # Found an anchor but couldn't parse; try next candidate
        return []

    @staticmethod
    def _parse_single_result(data: dict) -> Optional[dict]:
        if isinstance(data, list):
            if not data:
                return None
            data = data[0]
        roi = data.get("roi", data)
        cores = roi.get("cores", [roi])
        core = cores[0] if isinstance(cores, list) and cores else roi

        instructions = core.get("instructions", 0)
        cycles = core.get("cycles", 1) or 1
        ipc = instructions / cycles if cycles else 0.0

        mispred = core.get("mispredict", {}) or {}
        # ChampSim's raw JSON emits scalar counts; aggregate.py emits
        # list-wrapped totals (one entry per core, but we always run
        # single-core sims). Handle both shapes.
        def _coerce_mispredict(v):
            if isinstance(v, (int, float)):
                return int(v)
            if isinstance(v, list):
                return int(sum(x for x in v if isinstance(x, (int, float))))
            return 0
        total_mis = sum(_coerce_mispredict(v) for v in mispred.values())
        mpki = (total_mis / instructions) * 1000 if instructions else 0.0

        metrics: dict = {
            "ipc": round(ipc, 4),
            "mpki": round(mpki, 2),
            "instructions": instructions,
            "cycles": cycles,
            "mispredictions": total_mis,
        }
        # Cache stats — LLC / L2C / L1D / L1I
        for short, keys in [
            ("LLC", ["LLC"]),
            ("L2C", ["cpu0_L2C", "L2C"]),
            ("L1D", ["cpu0_L1D", "L1D"]),
            ("L1I", ["cpu0_L1I", "L1I"]),
        ]:
            cache = None
            for k in keys:
                if k in roi:
                    cache = roi[k]
                    break
            if not isinstance(cache, dict):
                continue
            hits, misses = ChampSimPlugin._extract_cache_stats(cache)
            total = hits + misses
            if total > 0:
                metrics[f"{short}_hit_rate"] = round(hits / total, 4)
                metrics[f"{short}_hits"] = hits
                metrics[f"{short}_misses"] = misses

        if "LLC_hit_rate" in metrics:
            metrics["hit_rate"] = metrics["LLC_hit_rate"]
        elif "L2C_hit_rate" in metrics:
            metrics["hit_rate"] = metrics["L2C_hit_rate"]
        metrics["speedup"] = metrics["ipc"]
        return metrics

    @staticmethod
    def _extract_cache_stats(cache_data: dict) -> tuple[int, int]:
        hits = 0
        misses = 0
        for at in ("LOAD", "RFO", "PREFETCH", "WRITE", "TRANSLATION"):
            at_data = cache_data.get(at, {})
            h = at_data.get("hit", [0])
            m = at_data.get("miss", [0])
            hits += sum(h) if isinstance(h, list) else int(h)
            misses += sum(m) if isinstance(m, list) else int(m)
        return hits, misses

    @staticmethod
    def _average_metrics(per_trace: list[dict]) -> dict:
        all_keys: set = set()
        for m in per_trace:
            all_keys.update(m.keys())
        out: dict = {}
        for k in sorted(all_keys):
            vals = [m[k] for m in per_trace if k in m]
            if not vals:
                continue
            if all(isinstance(v, (int, float)) for v in vals):
                avg = sum(vals) / len(vals)
                out[k] = round(avg, 4) if isinstance(vals[0], float) else round(avg, 2)
            else:
                out[k] = vals[0]
        out["_per_trace"] = per_trace
        return out

    # ---- workload export ----------------------------------------------------

    def export_workload_files(
        self, sim: ContainerManager, agent: ContainerManager,
        challenge: Challenge,
    ) -> None:
        """Copy decoded *.trace.txt from sim → agent /traces/decoded/.

        Without this, the agent's /traces/decoded/ is empty and the
        agent cannot inspect workload access patterns — defeating the
        whole point of an open-ended design challenge. Empirically: a
        Gemma-4 run with no decoded traces burned ~25 turns of confusion
        before timing out (mini-smoke #7).

        Decoded versions live at
        /work/workload_pools/champsim/decoded/<base>.trace.txt in the
        sim image. For per-challenge chunks NOT baked into the image
        (cache_replacement_fast's chunk0 splits), decode on the fly via
        simulators/champsim/decode_traces.py.
        """
        import tempfile
        sim_cfg = challenge.simulator_config or {}
        traces = sim_cfg.get("traces") or (
            [sim_cfg["trace"]] if sim_cfg.get("trace") else []
        )
        if not traces:
            return
        agent.exec("mkdir -p /traces/decoded", timeout=10)

        decoder_uploaded = False
        for trace_name in traces:
            decoded_name = trace_name.replace(".champsimtrace.xz", ".trace.txt")
            decoded_sim_path = (
                f"/work/workload_pools/champsim/decoded/{decoded_name}"
            )
            out, _ = sim.exec(
                f"test -f {decoded_sim_path} && echo EXISTS || echo MISSING",
                timeout=10,
            )
            if "MISSING" in out:
                if not decoder_uploaded:
                    decoder = (
                        Path(__file__).resolve().parent / "decode_traces.py"
                    )
                    if not decoder.exists():
                        raise FileNotFoundError(
                            f"decode_traces.py not found at {decoder}; "
                            f"cannot decode {trace_name} on the fly."
                        )
                    sim.copy_in(decoder, "/tmp/decode_traces.py")
                    decoder_uploaded = True
                input_xz = f"/work/workload_pools/champsim/{trace_name}"
                out, rc = sim.exec(
                    f"python3 -c 'import sys; sys.path.insert(0,\"/tmp\"); "
                    f"from decode_traces import decode_trace; "
                    f"from pathlib import Path as P; "
                    f"print(decode_trace(P(\"{input_xz}\"), "
                    f"P(\"{decoded_sim_path}\"), 200000))'",
                    timeout=300,
                )
                if rc != 0:
                    raise RuntimeError(
                        f"decode failed for {trace_name} (rc={rc}): {out[:500]}"
                    )

            with tempfile.NamedTemporaryFile(
                delete=False, suffix=".trace.txt",
            ) as f:
                host_tmp = Path(f.name)
            try:
                sim.copy_out(decoded_sim_path, host_tmp)
                agent.copy_in(host_tmp, f"/traces/decoded/{decoded_name}")
                log.info(
                    "exported decoded trace to agent: %s (%d bytes)",
                    decoded_name, host_tmp.stat().st_size,
                )
            finally:
                host_tmp.unlink(missing_ok=True)

    # ---- helpers ------------------------------------------------------------

    def submission_files(self, challenge: Challenge) -> list[str]:
        files = list(challenge.output_files)
        # Some challenges let the agent retune config.json; surface that.
        if challenge.simulator_config.get("config_tunable"):
            files.append("config.json")
        return files

    def default_source_blocklist(self, challenge: Challenge) -> list[str]:
        base = super().default_source_blocklist(challenge)
        comp_name = challenge.simulator_config.get("component_name", "")
        comp_dir = challenge.simulator_config.get("component_dir", "")
        if comp_name and comp_dir:
            base.append(f"{CHAMPSIM_DIR}/{comp_dir}/{comp_name}/*")
            base.append(f"{CHAMPSIM_DIR}/{comp_dir}/{comp_name}")
        return base

    def validate_challenge(self, challenge: Challenge) -> list[str]:
        errors = []
        sim = challenge.simulator_config or {}
        components = sim.get("components")
        if components:
            for i, c in enumerate(components):
                if "dir" not in c:
                    errors.append(f"simulator_config.components[{i}] missing 'dir'")
                if "name" not in c:
                    errors.append(f"simulator_config.components[{i}] missing 'name'")
        else:
            for req in ("component_dir", "component_name"):
                if req not in sim:
                    errors.append(f"simulator_config.{req} is required")
        for req in ("warmup", "simulation"):
            if req not in sim:
                errors.append(f"simulator_config.{req} is required")
        if not sim.get("trace") and not sim.get("traces"):
            errors.append("simulator_config must have 'trace' or 'traces'")
        return errors

    def _components(self, challenge: Challenge) -> list[tuple[str, str]]:
        """Return list of (component_dir, component_name) for this challenge."""
        sim = challenge.simulator_config or {}
        if sim.get("components"):
            return [(c["dir"], c["name"]) for c in sim["components"]]
        if sim.get("component_dir") and sim.get("component_name"):
            return [(sim["component_dir"], sim["component_name"])]
        return []
