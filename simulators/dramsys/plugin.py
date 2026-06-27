"""DRAMSysPlugin — SimulatorPlugin for the DRAMSys DRAM simulator.

Ported from the legacy `ARCHEVAL/simulators/dramsys.py`. Notable changes:

1. `cleanup_simulator()` calls the in-container `/work/cleanup.sh`
   instead of duplicating the cleanup logic in Python (mirrors
   ChampSimPlugin's fix; see `docs/lessons_learned.md`).

2. `verify_simulator()` calls `/work/verify.sh` for a single source of
   truth across Dockerfile bake-time, runtime verify, and operator
   preflight.

3. `run_submit()` orchestrates the build + simulate by copying
   `agent_files` into `/work/submission/` inside the long-lived sim
   container, then invoking `/work/build_and_run.sh <trace>`. DRAMSys
   has no per-submit compilation (config-only), but the script driver
   still validates JSON, copies configs into the DRAMSys tree, and runs
   the binary.

`build_and_run_args` is preserved from the legacy plugin — some
external callers (notably eval scripts) still rely on it.
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

log = logging.getLogger("archbench.dramsys")

DRAMSYS_DIR = "/work/runtimes/dramsys"


class DRAMSysPlugin(SimulatorPlugin):

    # Scripts the plugin expects baked into the image at /work/.
    # Self-healed at verify time for images built before this layout.
    _IN_CONTAINER_SCRIPTS = ("verify.sh", "cleanup.sh", "build_and_run.sh")

    # ---- identity ----------------------------------------------------------

    @property
    def name(self) -> str:
        return "dramsys"

    @property
    def docker_image(self) -> str:
        from archbench.image_management import manifest as images
        return images.fully_qualified("simulators", self.name)

    @property
    def docker_tar_name(self) -> Optional[str]:
        return "archbench-dramsys-v6.tar"

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
        """Reset the sim container; raise on failure (no silent best-effort)."""
        self._ensure_scripts_present(sim)
        out, rc = sim.exec("/work/cleanup.sh", timeout=60)
        if rc != 0 or "CLEANUP_OK" not in out:
            raise RuntimeError(
                f"DRAMSys cleanup failed (rc={rc}):\n{out}"
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
        """One-time setup for a DRAMSys challenge.

        DRAMSys is config-only (no compilation). We ensure /work/submission
        exists and is empty so per-submit copies don't see stale files.
        """
        sim.exec(
            "mkdir -p /work/submission && rm -rf /work/submission/*",
            timeout=10,
        )

    # ---- the submit dispatch ----------------------------------------------

    def run_submit(
        self, sim: ContainerManager, challenge: Challenge,
        agent_files: dict[str, str],
    ) -> str:
        """Build + run one submit. Returns raw stdout for parse_output.

        Mirrors champsim/scalesim (design §3.2): probe the challenge's
        ``evaluation/evaluate.sh`` (then root ``evaluate.sh``) and dispatch
        host-side via ``subprocess.run(["bash", evaluate_sh, tmp_path])``.
        evaluate.sh owns the sim container (a fresh ``podman run --rm`` that
        mounts the submission tmpdir) and forwards stdout carrying
        ``SIMULATION_OK`` + ``ARCHBENCH_JSON_START/END`` so parse_output works
        unchanged.

        Fallback (back-compat): if no evaluate.sh ships, drive the
        in-container ``/work/build_and_run.sh <trace>`` against the
        long-lived sim container.
        """
        # Tier-aware resolution: eval_dir is the legacy ``<challenge>/evaluation``
        # OR the tier-mode ``<family>/common/evaluation``; resolved_dirs hides
        # which (CLAUDE.md §1 path-resolution invariant).
        _sim_dir, eval_dir, _starter_dir = resolved_dirs(challenge)
        challenge_dir = challenge.challenge_dir or Path()
        # Multi-sim: a per-sim ``evaluate_<sim>.sh`` (here evaluate_dramsys.sh)
        # wins over the shared ``evaluate.sh`` so each sim's submit runs ITS
        # own simulator. Single-sim challenges ship only ``evaluate.sh`` and
        # take the same path as before (docs/multi_sim_design.md).
        evaluate_sh = next((p for p in (
            eval_dir / f"evaluate_{self.name}.sh",
            eval_dir / "evaluate.sh",
            challenge_dir / "evaluate.sh",
        ) if p.exists()), None)

        with tempfile.TemporaryDirectory(prefix="archbench_dramsys_") as tmp:
            tmp_path = Path(tmp)
            for fname, content in agent_files.items():
                host_file = tmp_path / fname
                host_file.parent.mkdir(parents=True, exist_ok=True)
                host_file.write_text(content)

            if evaluate_sh is not None:
                log.info("dispatch: evaluate.sh (host-side, fresh podman run)")
                # evaluate.sh encodes agent-code failure in JSON+marker
                # (rc 0); rc≠0 means infra failure (§1.9). We return
                # stdout+stderr regardless — parse_output keys on the marker
                # and the connector classifies SubmitOutcome from there.
                result = subprocess.run(
                    ["bash", str(evaluate_sh), str(tmp_path)],
                    capture_output=True, text=True, timeout=1800,
                )
                log.info(
                    "dramsys evaluate.sh rc=%d stdout %d B",
                    result.returncode, len(result.stdout),
                )
                return result.stdout + result.stderr

            # Fallback: in-container build_and_run.sh on the long-lived sim.
            log.info("dispatch: /work/build_and_run.sh (in-container, fallback)")
            sim_cfg = challenge.simulator_config or {}
            trace = sim_cfg.get("trace", "example.stl")
            sim.exec(
                "mkdir -p /work/submission && rm -rf /work/submission/*",
                timeout=10,
            )
            for fname in agent_files:
                sim.copy_in(tmp_path / fname, f"/work/submission/{fname}")
            out, rc = sim.exec(
                f"bash /work/build_and_run.sh {trace}",
                timeout=600,
            )
            log.info(
                "dramsys build_and_run rc=%d stdout %d B", rc, len(out),
            )
            return out

    # ---- v6-style external hook (kept for backward compat) ----------------

    def build_and_run_args(self, challenge: Challenge) -> list[str]:
        """Return CLI args for /work/build_and_run.sh.

        DRAMSys is simple: just pass the trace filename.
        """
        sim = challenge.simulator_config or {}
        trace = sim.get("trace", "example.stl")
        return [trace]

    # ---- parsing -----------------------------------------------------------

    def parse_output(self, raw_output: str) -> Optional[dict]:
        """Parse DRAMSys stdout into a metrics dict.

        Two formats supported:

        1. ARCHBENCH_JSON_START/END-wrapped JSON block (preferred, written by
           build_and_run.sh once metrics are aggregated).
        2. Bare DRAMSys textual stats lines (legacy):
             DRAMSys.controller0  Total Time:  13249920 ps
             DRAMSys.controller0  AVG BW:  72.49  Gb/s | 9.06   GB/s | 60.71 %
             DRAMSys.controller0  MAX BW:  119.40 Gb/s | 14.93  GB/s | 100.00 %
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
                pass  # fall through to regex extraction

        metrics: dict = {}

        # Extract average bandwidth
        bw_match = re.search(
            r"(?:AVG\s+BW|Average\s+Bandwidth)[:\s]+([0-9.]+)\s*Gb/s",
            raw_output, re.IGNORECASE,
        )
        if bw_match:
            metrics["bandwidth_gbps"] = round(float(bw_match.group(1)), 2)

        # Extract max bandwidth
        max_bw_match = re.search(
            r"(?:MAX\s+BW|Max(?:imum)?\s+Bandwidth)[:\s]+([0-9.]+)\s*Gb/s",
            raw_output, re.IGNORECASE,
        )
        if max_bw_match:
            metrics["max_bandwidth_gbps"] = round(
                float(max_bw_match.group(1)), 2,
            )

        # Total time: prefer ps form, fall back to ns
        time_match_ps = re.search(
            r"Total\s+Time[:\s]+([0-9.]+)\s*ps",
            raw_output, re.IGNORECASE,
        )
        time_match_ns = re.search(
            r"Total\s+Time[:\s]+([0-9.]+)\s*ns",
            raw_output, re.IGNORECASE,
        )
        if time_match_ps:
            metrics["total_time_ns"] = round(
                float(time_match_ps.group(1)) / 1000.0, 2,
            )
        elif time_match_ns:
            metrics["total_time_ns"] = round(
                float(time_match_ns.group(1)), 2,
            )

        # Average latency (if present)
        lat_match = re.search(
            r"(?:AVG\s+Latency|Average\s+Latency)[:\s]+([0-9.]+)\s*(?:ns|ps)",
            raw_output, re.IGNORECASE,
        )
        if lat_match:
            val = float(lat_match.group(1))
            unit = re.search(
                r"(ns|ps)", lat_match.group(0), re.IGNORECASE,
            )
            if unit and unit.group(1).lower() == "ps":
                val = val / 1000.0
            metrics["latency_ns"] = round(val, 2)

        return metrics if metrics else None

    # ---- helpers -----------------------------------------------------------

    def submission_files(self, challenge: Challenge) -> list[str]:
        """Files to copy from agent workspace to simulator.

        DRAMSys challenges submit JSON config files (config.json,
        optionally mc_config.json, memspec.json).

        Multi-sim (docs/multi_sim_design.md): in a session that binds more
        than one simulator each sim's ``submit`` tool must accept only ITS
        OWN files — the shared ``challenge.output_files`` is the union. A
        challenge expresses the per-sim split via
        ``simulator_config.submission_files``, a mapping keyed by sim name
        (e.g. ``{dramsys: [config.json, mc_config.json], ramulator:
        [ramulator_config.yaml]}``). Single-sim challenges that don't set the
        key fall through to ``challenge.output_files`` — byte-identical.
        """
        per_sim = self._per_sim_submission_files(challenge)
        if per_sim is not None:
            return per_sim
        return list(challenge.output_files)

    def get_workload_files(
        self, challenge: Challenge,
    ) -> list[tuple[str, str]]:
        sim = challenge.simulator_config or {}
        trace = sim.get("trace", "")
        if not trace:
            return []
        return [
            (
                f"dramsys/{trace}",
                f"/work/runtimes/dramsys/configs/traces/{trace}",
            ),
        ]

    def validate_challenge(self, challenge: Challenge) -> list[str]:
        errors = []
        sim = challenge.simulator_config or {}
        if "trace" not in sim:
            errors.append("simulator_config.trace is required")
        return errors

    def default_source_blocklist(
        self, challenge: Challenge,
    ) -> list[str]:
        base = super().default_source_blocklist(challenge)
        # Block the entire DRAMSys runtime to prevent leaking configs
        base.append("/work/runtimes/dramsys/*")
        return base

    def build_system_prompt_extra(self, challenge: Challenge) -> str:
        """Kept for compatibility with legacy callers. Docs surfaced via /api/."""
        return ""
