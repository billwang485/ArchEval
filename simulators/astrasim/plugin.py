"""AstraSimPlugin — SimulatorPlugin for ASTRA-sim distributed AI system sim.

Ported from the legacy `ARCHEVAL/simulators/astrasim.py`. Key changes:

1. `cleanup_simulator()` calls the in-container `/work/cleanup.sh`
   rather than duplicating cleanup logic in Python (single source of
   truth contract — see champsim plugin & lessons_learned).

2. `verify_simulator()` calls `/work/verify.sh` — same script the
   Dockerfile RUNs at bake time. Self-heals missing scripts so we
   don't need a full image rebuild for tweaks.

3. `run_submit()` mirrors champsim/scalesim's dispatch: if the
   challenge ships `evaluate.sh`, run it host-side; if it ships
   `simulate.sh`, dispatch into the long-lived sim container via
   simulate.sh; otherwise drive `/work/build_and_run.sh` directly
   inside the sim container with build_and_run_args.

4. All paths/markers/image tags rewritten to the `archbench-*` / `/work/`
   / `ARCHBENCH_*` namespace.
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

log = logging.getLogger("archbench.astrasim")


class AstraSimPlugin(SimulatorPlugin):

    @property
    def name(self) -> str:
        return "astrasim"

    @property
    def docker_image(self) -> str:
        from archbench.image_management import manifest as images
        return images.fully_qualified("simulators", self.name)

    @property
    def docker_tar_name(self) -> Optional[str]:
        # Legacy plugin returned f"archeval-astrasim-{IMAGE_VERSION}.tar".
        # New convention: archbench-<name>-<tag>.tar.
        return "archbench-astrasim-v6.tar"

    # Self-heal scripts on first verify so we don't force an image rebuild
    # for script tweaks during the open-ended rewrite.
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
                f"ASTRA-sim cleanup failed (rc={rc}):\n{out}"
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
        """ASTRA-sim needs no per-challenge configure step.

        Workloads, networks, and remote_memory configs are baked into
        the image. Agent submits system.json directly.
        """
        sim.exec("mkdir -p /work/submission", timeout=10)

    # ---- v6 / build_and_run dispatch ----------------------------------------

    def build_and_run_args(self, challenge: Challenge) -> list[str]:
        """Return CLI args for /work/build_and_run.sh.

        Args: <workload_prefix> <network_yml> <remote_memory_json>
              [--congestion-aware]
        """
        sim = challenge.simulator_config or {}
        workload = sim.get(
            "workload",
            "workloads/astrasim/workload/microbenchmarks/"
            "all_reduce/8npus_1MB/all_reduce",
        )
        network = sim.get("network", "Ring_8npus.yml")
        remote_memory = sim.get(
            "remote_memory", "no_memory_expansion.json"
        )

        args = [workload, network, remote_memory]

        if sim.get("congestion_aware", False):
            args.append("--congestion-aware")

        return args

    def submission_files(self, challenge: Challenge) -> list[str]:
        """Files to copy from agent workspace to simulator."""
        return list(challenge.output_files)

    def run_submit(
        self, sim: ContainerManager, challenge: Challenge,
        agent_files: dict[str, str],
    ) -> str:
        """Build + run one submit. Returns raw stdout for parse_output.

        Mirrors champsim's dispatch: evaluate.sh host-side if present;
        otherwise simulate.sh into the long-lived sim container.
        Fallback: run build_and_run.sh directly inside the sim container
        with build_and_run_args (for challenges that ship neither).
        """
        # Resolve via the shared helper so legacy 3-subdir layouts and
        # tier-mode layouts (common/) both work. Encapsulates the legacy
        # fallback to challenge-root paths. Mirrors champsim's dispatch
        # so the agent path and `archbench baseline` both flow through
        # evaluation/evaluate.sh (comparability invariant,
        # unified_eval_baseline_design §3.2).
        sim_dir, eval_dir, _starter_dir = resolved_dirs(challenge)
        evaluate_sh = eval_dir / "evaluate.sh"
        simulate_sh = sim_dir / "simulate.sh"

        with tempfile.TemporaryDirectory(prefix="archbench_astrasim_") as tmp:
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

            # Fallback: drive build_and_run.sh directly inside the sim container.
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
        """Parse ASTRA-sim build_and_run.sh output into metrics dict.

        Primary path: extract JSON from ARCHBENCH_JSON_START/END markers.
        Fallback: regex parse from raw output.
        """
        if "SIMULATION_OK" not in raw_output:
            return None

        metrics: dict = {}

        # Extract JSON between markers
        start_marker = "ARCHBENCH_JSON_START"
        end_marker = "ARCHBENCH_JSON_END"
        si = raw_output.rfind(start_marker)
        ei = raw_output.rfind(end_marker)
        if si >= 0 and ei > si:
            json_str = raw_output[si + len(start_marker):ei].strip()
            try:
                metrics = json.loads(json_str)
                # Remove nested per_npu data from top-level metrics
                metrics.pop("per_npu", None)
            except json.JSONDecodeError:
                pass

        # Fallback: regex parse from raw output
        if not metrics:
            pattern = (
                r"sys\[(\d+)\]\s+finished,\s+(\d+)\s+cycles"
                r".*?exposed\s+communication\s+(\d+)\s+cycles"
            )
            matches = re.findall(pattern, raw_output)
            if matches:
                total_cycles = [int(m[1]) for m in matches]
                exposed_comm = [int(m[2]) for m in matches]
                metrics = {
                    "total_cycles": max(total_cycles),
                    "exposed_comm_cycles": max(exposed_comm),
                    "num_npus": len(matches),
                }

        return metrics if metrics else None

    # ---- introspection ------------------------------------------------------

    def validate_challenge(self, challenge: Challenge) -> list[str]:
        """Validate challenge YAML has required fields."""
        errors = []
        sim = challenge.simulator_config or {}
        if "workload" not in sim:
            errors.append("simulator_config.workload is required")
        if "network" not in sim:
            errors.append("simulator_config.network is required")
        return errors

    def default_source_blocklist(self, challenge: Challenge) -> list[str]:
        base = super().default_source_blocklist(challenge)
        return base

    def build_system_prompt_extra(self, challenge: Challenge) -> str:
        """Kept for compatibility with external callers (legacy hook).

        Docs are surfaced via /api/ now, so return empty.
        """
        return ""
