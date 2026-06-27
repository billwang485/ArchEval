"""ScaleSimPlugin — SimulatorPlugin for the SCALE-Sim v2 systolic-array sim.

Ported from the legacy `ARCHEVAL/simulators/scalesim.py`. Key changes:

1. `cleanup_simulator()` calls the in-container `/work/cleanup.sh`
   rather than duplicating the cleanup script in Python (per the new
   single-source-of-truth contract; see champsim plugin & lessons_learned).

2. `verify_simulator()` calls `/work/verify.sh` — same script the
   Dockerfile RUNs at bake time and the `archbench verify-all` CLI uses.
   Self-heals missing scripts the same way ChampSim does so we don't
   need a full image rebuild for script tweaks.

3. `run_submit()` mirrors ChampSim's dispatch: if the challenge ships
   `evaluate.sh`, run it host-side; otherwise dispatch into the
   long-lived sim container's `/work/build_and_run.sh` via simulate.sh.
"""

from __future__ import annotations

import json
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

log = logging.getLogger("archbench.scalesim")


class ScaleSimPlugin(SimulatorPlugin):

    @property
    def name(self) -> str:
        return "scalesim"

    @property
    def docker_image(self) -> str:
        from archbench.image_management import manifest as images
        return images.fully_qualified("simulators", self.name)

    # Self-heal scripts on first verify so we don't force an image
    # rebuild for script tweaks during the open-ended rewrite.
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
        """Reset the sim container; raise on failure."""
        self._ensure_scripts_present(sim)
        out, rc = sim.exec("/work/cleanup.sh", timeout=60)
        if rc != 0 or "CLEANUP_OK" not in out:
            raise RuntimeError(
                f"ScaleSim cleanup failed (rc={rc}):\n{out}"
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
        """One-time per-challenge setup. ScaleSim needs no config step;
        workloads (topology CSVs) are mounted via get_workload_files()."""
        sim.exec("mkdir -p /work/submission", timeout=10)

    # ---- v6 / build_and_run dispatch ----------------------------------------

    def build_and_run_args(self, challenge: Challenge) -> list[str]:
        """Return CLI args for /work/build_and_run.sh.

        Args: <topology> [--constraints <max_pe> <max_sram_kb> <max_bw>]
                         [--timeout <seconds>]
        """
        sim = challenge.simulator_config
        topology = sim.get("topology", "test_small.csv")
        args = [topology]

        constraints = sim.get("constraints")
        if constraints:
            args.extend([
                "--constraints",
                str(constraints.get("max_pe_count", 0)),
                str(constraints.get("max_sram_kb", 0)),
                str(constraints.get("max_bandwidth", 0)),
            ])

        sim_timeout = sim.get("sim_timeout")
        if sim_timeout:
            args.extend(["--timeout", str(sim_timeout)])

        return args

    def run_submit(
        self, sim: ContainerManager, challenge: Challenge,
        agent_files: dict[str, str],
    ) -> str:
        """Build + run one submit. Returns raw stdout for parse_output.

        Mirrors champsim's dispatch: evaluate.sh host-side if present;
        otherwise simulate.sh into the long-lived sim container.
        Fallback: run build_and_run.sh directly inside the sim
        container with build_and_run_args (for challenges that ship
        neither evaluate.sh nor simulate.sh).
        """
        sim_dir, eval_dir, _starter_dir = resolved_dirs(challenge)
        evaluate_sh = eval_dir / "evaluate.sh"
        simulate_sh = sim_dir / "simulate.sh"

        with tempfile.TemporaryDirectory(prefix="archbench_scalesim_") as tmp:
            tmp_path = Path(tmp)
            for fname, content in agent_files.items():
                (tmp_path / fname).write_text(content)

            if evaluate_sh.exists():
                log.info("dispatch: evaluate.sh (host-side)")
                cmd = ["bash", str(evaluate_sh), str(tmp_path)]
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=1800,
                )
                return result.stdout + result.stderr

            if simulate_sh.exists():
                log.info(
                    "dispatch: simulate.sh --container %s", sim.name,
                )
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
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=1800,
                )
                return result.stdout + result.stderr

            # Fallback: drive build_and_run.sh directly via the sim container.
            log.info("dispatch: /work/build_and_run.sh (in-container, fallback)")
            sim.exec(
                "mkdir -p /work/submission && rm -rf /work/submission/*",
                timeout=10,
            )
            for fname in agent_files:
                sim.copy_in(tmp_path / fname, f"/work/submission/{fname}")
            args = self.build_and_run_args(challenge)
            quoted = " ".join(self._shquote(a) for a in args)
            out, _rc = sim.exec(
                f"/work/build_and_run.sh {quoted}", timeout=1800,
            )
            return out

    @staticmethod
    def _shquote(s: str) -> str:
        if not s or any(c in s for c in " \t'\"$`\\"):
            return "'" + s.replace("'", "'\\''") + "'"
        return s

    # ---- parsing ------------------------------------------------------------

    def parse_output(self, raw_output: str) -> Optional[dict]:
        """Parse SCALE-Sim build_and_run.sh output into metrics dict.

        Primary path: extract JSON from ARCHBENCH_JSON_START/END markers.
        Fallback: regex-based parsing of the metrics section.
        """
        if "SIMULATION_OK" not in raw_output:
            return None

        metrics: dict = {}

        # Primary: ARCHBENCH_JSON markers from build_and_run.sh
        start_marker = "ARCHBENCH_JSON_START"
        end_marker = "ARCHBENCH_JSON_END"
        si = raw_output.rfind(start_marker)
        ei = raw_output.rfind(end_marker)
        if si >= 0 and ei > si:
            json_str = raw_output[si + len(start_marker):ei].strip()
            try:
                metrics = json.loads(json_str)
            except json.JSONDecodeError:
                pass

        # Fallback: regex-based parsing from metrics section
        if not metrics:
            metrics_section = raw_output
            marker = "=== mini-architect-bench: Metrics ==="
            idx = raw_output.rfind(marker)
            if idx < 0:
                # Fallback: legacy marker if running against a non-rebuilt image
                idx = raw_output.rfind("=== ARCHEVAL: Metrics ===")
            if idx >= 0:
                metrics_section = raw_output[idx:]

            m = re.search(
                r"(?:Total\s+Cycles?|total_cycles)[:\s]+(\d+)",
                metrics_section, re.IGNORECASE,
            )
            if m:
                metrics["total_cycles"] = int(m.group(1))

            m = re.search(
                r"(?:Compute\s+Cycles?|compute_cycles)[:\s]+(\d+)",
                metrics_section, re.IGNORECASE,
            )
            if m:
                metrics["compute_cycles"] = int(m.group(1))

            m = re.search(
                r"(?:Overall\s+Utilization|Utilization|utilization)[:\s]+([0-9.]+)",
                metrics_section, re.IGNORECASE,
            )
            if m:
                metrics["utilization"] = round(float(m.group(1)), 2)

            m = re.search(
                r"(?:Mapping\s+Efficiency|mapping_efficiency)[:\s]+([0-9.]+)",
                metrics_section, re.IGNORECASE,
            )
            if m:
                metrics["mapping_efficiency"] = round(float(m.group(1)), 2)

            m = re.search(
                r"(?:Stall\s+Cycles?|stall_cycles)[:\s]+(\d+)",
                metrics_section, re.IGNORECASE,
            )
            if m:
                metrics["stall_cycles"] = int(m.group(1))

        # Hardware cost (emitted by constraint check, before metrics section)
        m = re.search(r"Hardware\s+Cost[:\s]+([0-9.]+)", raw_output)
        if m:
            metrics["hardware_cost"] = round(float(m.group(1)), 4)

        return metrics if metrics else None

    # ---- introspection ------------------------------------------------------

    def submission_files(self, challenge: Challenge) -> list[str]:
        """Agent submits config.cfg for SCALE-Sim."""
        return ["config.cfg"]

    def get_workload_files(self, challenge: Challenge) -> list[tuple[str, str]]:
        sim = challenge.simulator_config
        topology = sim.get("topology", "")
        if not topology:
            return []
        return [
            (f"scalesim/{topology}", f"/work/workloads/scalesim/{topology}"),
        ]

    def validate_challenge(self, challenge: Challenge) -> list[str]:
        errors = []
        sim = challenge.simulator_config
        if "topology" not in sim:
            errors.append("simulator_config.topology is required")
        return errors

    def default_source_blocklist(self, challenge: Challenge) -> list[str]:
        base = super().default_source_blocklist(challenge)
        base.append("/work/runtimes/scalesim/*")
        return base

    def build_system_prompt_extra(self, challenge: Challenge) -> str:
        """Kept for compatibility with external callers (legacy hook).

        Docs are surfaced via /api/ now, so return empty.
        """
        return ""
