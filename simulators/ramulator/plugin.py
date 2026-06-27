"""RamulatorPlugin — SimulatorPlugin for the Ramulator 2.0 DRAM simulator.

Ported from the legacy `ARCHEVAL/simulators/ramulator.py`. Notable changes:

1. `cleanup_simulator()` calls the in-container `/work/cleanup.sh`
   instead of duplicating logic in Python.

2. `verify_simulator()` calls `/work/verify.sh` — one verifier across
   Dockerfile bake-time, runtime, and operator preflight.

3. `run_submit()` orchestrates the per-submit dispatch by copying
   `agent_files` into `/work/submission/`, then invoking
   `/work/build_and_run.sh <challenge> <component>` against the
   long-lived sim container.

Ramulator 2.0 supports two challenge shapes:
  - **C++ component mode**: agent submits .cpp/.h files; build_and_run.sh
    drops them into /work/runtimes/ramulator/src/work/ and rebuilds.
  - **Config-only mode**: agent submits config.yaml only.

`build_and_run_args` is preserved from the legacy plugin for external
callers (eval scripts).
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from archbench.core.path_resolution import resolved_dirs
from archbench.core.plugin_base import SimulatorPlugin

if TYPE_CHECKING:
    from archbench.core.challenge import Challenge
    from archbench.core.container import ContainerManager

log = logging.getLogger("archbench.ramulator")

RAMULATOR_DIR = "/work/runtimes/ramulator"


class RamulatorPlugin(SimulatorPlugin):

    _IN_CONTAINER_SCRIPTS = ("verify.sh", "cleanup.sh", "build_and_run.sh")

    # ---- identity ----------------------------------------------------------

    @property
    def name(self) -> str:
        return "ramulator"

    @property
    def docker_image(self) -> str:
        from archbench.image_management import manifest as images
        return images.fully_qualified("simulators", self.name)

    @property
    def docker_tar_name(self) -> Optional[str]:
        return "archbench-ramulator-v6.tar"

    # ---- lifecycle ---------------------------------------------------------

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
        """Reset the sim container; raise on failure."""
        self._ensure_scripts_present(sim)
        out, rc = sim.exec("/work/cleanup.sh", timeout=60)
        if rc != 0 or "CLEANUP_OK" not in out:
            raise RuntimeError(
                f"Ramulator cleanup failed (rc={rc}):\n{out}"
            )

    def _ensure_scripts_present(self, sim: ContainerManager) -> None:
        """Inject any missing /work/*.sh from the host. Idempotent."""
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
                continue
            sim.copy_in(host_path, f"/work/{script}")
            sim.exec(f"chmod +x /work/{script}", timeout=10)

    def configure_simulator(
        self, sim: ContainerManager, challenge: Challenge,
    ) -> None:
        """Copy challenge config.yaml into simulator container.

        Per-submit code injection happens in run_submit. config.yaml is
        staged once here so all later submits see the same baseline.
        """
        sim.exec(
            "mkdir -p /work/submission && rm -rf /work/submission/*",
            timeout=10,
        )
        challenge_dir = challenge.challenge_dir
        if challenge_dir is None:
            return
        config_yaml = challenge_dir / "config.yaml"
        if config_yaml.exists():
            sim.copy_in(config_yaml, "/work/challenge_config.yaml")

    # ---- the submit dispatch ----------------------------------------------

    def run_submit(
        self, sim: ContainerManager, challenge: Challenge,
        agent_files: dict[str, str],
    ) -> str:
        """Build + run one submit. Returns raw stdout for parse_output.

        Mirrors champsim/scalesim (design §3.2): probe the challenge's
        ``evaluation/evaluate.sh`` (then root ``evaluate.sh``) and dispatch
        host-side via ``subprocess.run(["bash", evaluate_sh, tmp_path])``.
        An evaluate.sh-mode ramulator challenge owns its own sim container
        (e.g. a fresh ``podman run``) and stages its own
        ``challenge_config.yaml``; it forwards stdout carrying
        ``SIMULATION_OK`` + ``ARCHBENCH_JSON_START/END`` so parse_output works
        unchanged.

        Today's only ramulator challenge, ``dram_pride_rfm_design``, DOES
        ship an ``evaluation/evaluate.sh`` — but a refuse-to-score scaffold
        one (emits a ``ARCHBENCH_JSON`` block with NO ``SIMULATION_OK`` and exits
        non-zero). So it now takes this host-side path; ``parse_output``
        finds no ``SIMULATION_OK`` marker → returns None → the submit
        handler classifies it as a typed BUILD_FAIL (no fabricated score,
        §1.9). This is the intended behavior for a scaffold.

        Fallback (back-compat): if a challenge ships NO evaluate.sh at all,
        drive the in-container ``/work/build_and_run.sh <challenge>
        <component>`` against the long-lived sim container (which has
        ``challenge_config.yaml`` staged by configure_simulator).

        component_name is empty for config-only challenges; non-empty
        triggers the C++ rebuild path inside build_and_run.sh.
        """
        # Resolve sim/eval/starter via the shared helper — handles
        # legacy (3-subdir) AND tier-mode layouts identically.
        _sim_dir, eval_dir, _starter_dir = resolved_dirs(challenge)
        # Multi-sim: a per-sim ``evaluate_<sim>.sh`` (here evaluate_ramulator.sh)
        # wins over the shared ``evaluate.sh`` so each sim's submit runs ITS
        # own simulator. Single-sim challenges ship only ``evaluate.sh`` and
        # take the same path as before (docs/multi_sim_design.md).
        evaluate_sh = next((p for p in (
            eval_dir / f"evaluate_{self.name}.sh",
            eval_dir / "evaluate.sh",
        ) if p.exists()), None)

        sim_cfg = challenge.simulator_config or {}
        component_name = sim_cfg.get("component_name", "")
        cname = (
            challenge.challenge_dir.name
            if challenge.challenge_dir else challenge.id
        )

        with tempfile.TemporaryDirectory(prefix="archbench_ramulator_") as tmp:
            tmp_path = Path(tmp)
            for fname, content in agent_files.items():
                host_file = tmp_path / fname
                host_file.parent.mkdir(parents=True, exist_ok=True)
                host_file.write_text(content)

            if evaluate_sh is not None:
                log.info("dispatch: evaluate.sh (host-side)")
                # evaluate.sh encodes agent-code failure in JSON+marker
                # (rc 0); rc≠0 means infra failure (§1.9). Return
                # stdout+stderr regardless — parse_output keys on the marker.
                result = subprocess.run(
                    ["bash", str(evaluate_sh), str(tmp_path)],
                    capture_output=True, text=True, timeout=1800,
                )
                log.info(
                    "ramulator evaluate.sh rc=%d stdout %d B",
                    result.returncode, len(result.stdout),
                )
                return result.stdout + result.stderr

            # Fallback: in-container build_and_run.sh on the long-lived sim
            # (which has challenge_config.yaml staged by configure_simulator).
            log.info("dispatch: /work/build_and_run.sh (in-container, fallback)")
            sim.exec(
                "mkdir -p /work/submission && rm -rf /work/submission/*",
                timeout=10,
            )
            for fname in agent_files:
                sim.copy_in(tmp_path / fname, f"/work/submission/{fname}")
            # 30 min cap on per-submit sim time: ramulator2 rebuild +
            # simulate can hit ~10 min for the larger SPEC traces.
            cmd = f"bash /work/build_and_run.sh {cname!r} {component_name!r}"
            out, rc = sim.exec(cmd, timeout=1800)
            log.info(
                "ramulator build_and_run rc=%d stdout %d B", rc, len(out),
            )
            return out

    # ---- v6-style external hook (kept for backward compat) ----------------

    def build_and_run_args(self, challenge: Challenge) -> list[str]:
        """Return CLI args for /work/build_and_run.sh.

        Args passed: <challenge_name> <component_name>
        component_name is empty string for config-only challenges.
        """
        sim = challenge.simulator_config or {}
        component_name = sim.get("component_name", "")
        cname = (
            challenge.challenge_dir.name
            if challenge.challenge_dir else challenge.id
        )
        return [cname, component_name]

    # ---- parsing -----------------------------------------------------------

    def parse_output(self, raw_output: str) -> Optional[dict]:
        """Parse Ramulator 2.0 stdout into a metrics dict.

        Two formats supported:

        1. ARCHBENCH_JSON_START/END-wrapped JSON block (preferred).
        2. Bare ramulator2 textual stats (legacy):
             [Memory] total_num_read_requests 6
             [Memory] memory_system_cycles 86
             [Memory] avg_read_latency_0 46.5
             [Memory] row_hits_0 ...
             [Memory] row_misses_0 ...
        """
        if "SIMULATION_OK" not in raw_output:
            return None

        # Strategy 1: ARCHBENCH_JSON_START/END markers
        import json
        si = raw_output.find("ARCHBENCH_JSON_START")
        ei = raw_output.find("ARCHBENCH_JSON_END")
        if si >= 0 and ei > si:
            blob = raw_output[si + len("ARCHBENCH_JSON_START"):ei].strip()
            try:
                parsed = json.loads(blob)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass  # fall through

        metrics: dict = {}

        # Total cycles / memory system cycles
        m = re.search(r"memory_system_cycles[:\s]+(\d+)", raw_output)
        if m:
            metrics["cycles"] = int(m.group(1))

        # Read requests
        m = re.search(r"total_num_read_requests[:\s]+(\d+)", raw_output)
        if m:
            metrics["read_requests"] = int(m.group(1))

        # Write requests
        m = re.search(r"total_num_write_requests[:\s]+(\d+)", raw_output)
        if m:
            metrics["write_requests"] = int(m.group(1))

        # Total requests
        if "read_requests" in metrics:
            metrics["total_requests"] = (
                metrics["read_requests"]
                + metrics.get("write_requests", 0)
            )

        # Average read latency
        m = re.search(
            r"avg_read_latency[_0-9]*[:\s]+([0-9.]+)", raw_output,
        )
        if m:
            metrics["latency_avg"] = round(float(m.group(1)), 2)

        # Row buffer hit rate — compute from row_hits and row_misses
        hits_m = re.search(r"row_hits_0[:\s]+(\d+)", raw_output)
        misses_m = re.search(r"row_misses_0[:\s]+(\d+)", raw_output)
        if hits_m and misses_m:
            hits = int(hits_m.group(1))
            misses = int(misses_m.group(1))
            total = hits + misses
            if total > 0:
                metrics["row_buffer_hit_rate"] = round(
                    hits / total, 4,
                )

        # Bandwidth (if printed)
        m = re.search(
            r"bandwidth[_0-9]*[:\s]+([0-9.]+)\s*(?:GB/s|Gb/s)?",
            raw_output, re.IGNORECASE,
        )
        if m:
            metrics["bandwidth_gbps"] = round(float(m.group(1)), 2)

        return metrics if metrics else None

    # ---- helpers -----------------------------------------------------------

    def submission_files(self, challenge: Challenge) -> list[str]:
        """Files to copy from agent workspace to simulator.

        Multi-sim (docs/multi_sim_design.md): if the challenge declares a
        per-sim split under ``simulator_config.submission_files`` keyed by
        sim name, return THIS sim's entry verbatim (so ``ramulator_submit``
        accepts only the ramulator config, not the dramsys files in the
        shared ``output_files`` union). Single-sim challenges don't set the
        key and keep the legacy behavior below.
        """
        per_sim = self._per_sim_submission_files(challenge)
        if per_sim is not None:
            return per_sim
        files = list(challenge.output_files)
        # Config-only challenges may submit yaml files
        if not challenge.simulator_config.get("component_name"):
            if "config.yaml" not in files and "ddr4_custom.yaml" not in files:
                files.append("config.yaml")
        return files

    def get_workload_files(
        self, challenge: Challenge,
    ) -> list[tuple[str, str]]:
        sim = challenge.simulator_config or {}
        trace = sim.get("trace", "")
        if not trace:
            return []
        return [
            (
                f"ramulator/{trace}",
                f"/work/workloads/ramulator/{trace}",
            ),
        ]

    def validate_challenge(self, challenge: Challenge) -> list[str]:
        return []

    def default_source_blocklist(
        self, challenge: Challenge,
    ) -> list[str]:
        base = super().default_source_blocklist(challenge)
        base.append("/work/runtimes/ramulator/src/work/*")
        return base

    def build_system_prompt_extra(self, challenge: Challenge) -> str:
        """Return empty — docs are now in /api/ directory."""
        return ""
