"""MNSIMPlugin — SimulatorPlugin for MNSIM 2.0.

MNSIM 2.0 (Tsinghua NICS-EFC, https://github.com/thu-nics/MNSIM-2.0) is a
behavior-level modeling tool for memristor / ReRAM Processing-In-Memory
(PIM) neural-network accelerators. Given a hardware-description file
(``SimConfig.ini`` — crossbar size, ADC/DAC choice, device tech, etc.) and
a NN topology, it estimates NN-on-crossbar accuracy plus the hardware
latency / area / power / energy of the resulting accelerator.

Shape-wise MNSIM is the same kind of sim as SCALE-Sim and Timeloop:
config in -> metrics out, pure Python. This plugin therefore mirrors
``simulators/scalesim/plugin.py``:

1. ``verify_simulator`` / ``cleanup_simulator`` call the baked
   ``/work/verify.sh`` / ``/work/cleanup.sh`` (single source of truth), and
   self-heal missing scripts so a script tweak doesn't force an image
   rebuild during the open-ended rewrite.

2. ``run_submit`` dispatches to the challenge's host-side
   ``evaluation/evaluate.sh`` when present (the unified comparability
   contract; see docs/unified_eval_baseline_design.md), falling back to
   ``simulate.sh`` and then the in-container ``/work/build_and_run.sh``.

3. ``parse_output`` keys on a ``SIMULATION_OK`` marker and extracts
   MNSIM's hardware metrics (latency / area / power / energy) plus
   accuracy when the accuracy path was enabled.
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

log = logging.getLogger("archbench.mnsim")


class MNSIMPlugin(SimulatorPlugin):

    @property
    def name(self) -> str:
        return "mnsim"

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
        # verify.sh runs only cheap structural checks (no e2e smoke), so the
        # fast cap (like scalesim) is correct; the real hw path is exercised by
        # evaluate.sh / submit, not here.
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
                f"MNSIM cleanup failed (rc={rc}):\n{out}"
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
        """One-time per-challenge setup. MNSIM needs no config step; the
        agent submits a SimConfig.ini, staged per submit into
        /work/submission."""
        sim.exec("mkdir -p /work/submission", timeout=10)

    # ---- v6 / build_and_run dispatch ----------------------------------------

    def build_and_run_args(self, challenge: Challenge) -> list[str]:
        """Return CLI args for /work/build_and_run.sh.

        Args: [--nn <name>] [--weights <path>] [--accuracy] [--timeout <s>]
        """
        sim = challenge.simulator_config
        args: list[str] = []

        nn = sim.get("nn")
        if nn:
            args.extend(["--nn", str(nn)])

        weights = sim.get("weights")
        if weights:
            args.extend(["--weights", str(weights)])

        if sim.get("accuracy"):
            args.append("--accuracy")

        sim_timeout = sim.get("sim_timeout")
        if sim_timeout:
            args.extend(["--timeout", str(sim_timeout)])

        return args

    def run_submit(
        self, sim: ContainerManager, challenge: Challenge,
        agent_files: dict[str, str],
    ) -> str:
        """Build + run one submit. Returns raw stdout for parse_output.

        Mirrors scalesim's dispatch: evaluate.sh host-side if present;
        otherwise simulate.sh into the long-lived sim container.
        Fallback: run build_and_run.sh directly inside the sim container
        with build_and_run_args (for challenges that ship neither
        evaluate.sh nor simulate.sh).

        Path resolution flows through ``resolved_dirs`` so tier-mode
        challenges (where ``evaluation/`` lives under ``<family>/common/``)
        resolve identically to legacy 3-subdir challenges.
        """
        sim_dir, eval_dir, _starter_dir = resolved_dirs(challenge)
        evaluate_sh = eval_dir / "evaluate.sh"
        simulate_sh = sim_dir / "simulate.sh"

        with tempfile.TemporaryDirectory(prefix="archbench_mnsim_") as tmp:
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
                log.info("dispatch: simulate.sh --container %s", sim.name)
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
        """Parse MNSIM build_and_run.sh output into a metrics dict.

        Primary path: extract JSON from ARCHBENCH_JSON_START/END markers.
        Fallback: regex-based parsing of MNSIM's summary lines.
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

        # Fallback: regex-based parsing from MNSIM's summary lines.
        if not metrics:
            def grab(pattern):
                m = re.search(pattern, raw_output)
                return float(m.group(1)) if m else None

            v = grab(r"Entire latency:\s*([0-9.eE+-]+)\s*ns")
            if v is not None:
                metrics["latency_ns"] = v
            v = grab(r"Hardware area:\s*([0-9.eE+-]+)\s*um\^2")
            if v is not None:
                metrics["area_um2"] = v
            v = grab(r"Hardware power:\s*([0-9.eE+-]+)\s*W")
            if v is not None:
                metrics["power_w"] = v
            v = grab(r"Hardware energy:\s*([0-9.eE+-]+)\s*nJ")
            if v is not None:
                metrics["energy_nj"] = v
            v = grab(r"PIM-based computing accuracy:\s*([0-9.eE+-]+)")
            if v is not None:
                metrics["pim_accuracy"] = v
            v = grab(r"Original accuracy:\s*([0-9.eE+-]+)")
            if v is not None:
                metrics["original_accuracy"] = v

        return metrics if metrics else None

    # ---- introspection ------------------------------------------------------

    def submission_files(self, challenge: Challenge) -> list[str]:
        """Files the agent submits. Honour what the challenge declares
        (``output_files`` -- e.g. ``design.json`` for a co-design challenge);
        fall back to MNSIM's hardware-description file (``SimConfig.ini``) for
        config-tuning challenges that declare no deliverable of their own."""
        files = list(challenge.output_files or [])
        return files if files else ["SimConfig.ini"]

    def get_workload_files(self, challenge: Challenge) -> list[tuple[str, str]]:
        """MNSIM's NN topology is selected by name (-NN) and built
        analytically from SimConfig.ini; no external workload file is
        mounted for the default hardware-modeling path."""
        return []

    def validate_challenge(self, challenge: Challenge) -> list[str]:
        # MNSIM has no strictly-required simulator_config keys for the
        # default hardware-modeling path (nn/weights/accuracy are optional).
        return []

    def default_source_blocklist(self, challenge: Challenge) -> list[str]:
        base = super().default_source_blocklist(challenge)
        base.append("/work/runtimes/mnsim/*")
        return base

    def build_system_prompt_extra(self, challenge: Challenge) -> str:
        """Kept for compatibility with external callers (legacy hook).

        Docs are surfaced via /api/ now, so return empty.
        """
        return ""
