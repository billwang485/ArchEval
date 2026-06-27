"""TimeloopPlugin — SimulatorPlugin for the Timeloop/Accelergy DNN accelerator sim.

Ported from the legacy `ARCHEVAL/simulators/timeloop.py`. Key changes:

1. `cleanup_simulator()` calls the in-container `/work/cleanup.sh`
   rather than duplicating the cleanup script in Python.

2. `verify_simulator()` calls `/work/verify.sh` — same script the
   Dockerfile RUNs at bake time and the `archbench verify-all` CLI uses.
   Self-heals missing scripts on first verify.

3. `run_submit()` mirrors ChampSim's dispatch: evaluate.sh host-side
   when present, otherwise simulate.sh against the long-lived sim
   container. Falls back to driving build_and_run.sh directly if
   neither script ships with the challenge.
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

log = logging.getLogger("archbench.timeloop")


class TimeloopPlugin(SimulatorPlugin):

    @property
    def name(self) -> str:
        return "timeloop"

    @property
    def docker_image(self) -> str:
        from archbench.image_management import manifest as images
        return images.fully_qualified("simulators", self.name)

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
                f"Timeloop cleanup failed (rc={rc}):\n{out}"
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
        """One-time per-challenge setup. Timeloop is config-only —
        no compile step, just make sure the submission dir is ready."""
        sim.exec("mkdir -p /work/submission", timeout=10)

    # ---- v6 / build_and_run dispatch ----------------------------------------

    def build_and_run_args(self, challenge: Challenge) -> list[str]:
        """Return CLI args for /work/build_and_run.sh.

        Timeloop: pass the problem YAML filename.
        """
        sim = challenge.simulator_config
        problem = sim.get("problem", "simple_conv.yaml")
        return [problem]

    def run_submit(
        self, sim: ContainerManager, challenge: Challenge,
        agent_files: dict[str, str],
    ) -> str:
        """Build + run one submit. Returns raw stdout for parse_output.

        Mirrors champsim's dispatch: evaluate.sh host-side, simulate.sh
        into the long-lived container, or fallback to direct
        build_and_run.sh in the sim container.
        """
        # Resolve via the shared helper so legacy 3-subdir layouts and
        # tier-mode layouts (<family>/common/) both work. Encapsulates the
        # legacy fallback to challenge-root paths — without it, tier-mode
        # challenges fall through to the in-container /work/build_and_run.sh
        # path, BYPASSING the canonical evaluate.sh and violating the
        # comparability invariant (unified_eval_baseline_design §3.2). For
        # cnn_accelerator_codesign in particular, that would skip the trio
        # aggregator (accuracy + energy + latency + judge).
        sim_dir, eval_dir, _starter_dir = resolved_dirs(challenge)
        evaluate_sh = eval_dir / "evaluate.sh"
        simulate_sh = sim_dir / "simulate.sh"
        # evaluate.sh is single-arg + tier-agnostic: it reads the FIXED arch +
        # workloads + fallback mappings from common/workload/ (tier-invariant),
        # and the agent's mappings from $1. Same single-arg contract `archbench
        # baseline` uses, so baseline and agent submit hit the identical script
        # (§1.7 comparability).

        with tempfile.TemporaryDirectory(prefix="archbench_timeloop_") as tmp:
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

            # Fallback: drive build_and_run.sh directly inside the sim.
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
        """Parse Timeloop mapper stdout into metrics dict.

        Two paths:

        1. **Custom evaluate.sh wraps a JSON block in
           ``ARCHBENCH_JSON_START``/``ARCHBENCH_JSON_END`` (or legacy
           ``ARCHEVAL_JSON_START``/``ARCHEVAL_JSON_END``).** Used by
           the cnn_accelerator_codesign challenge, where evaluate.sh
           composes accuracy + energy + latency into a single trio
           payload via aggregate.py. We trust the wrapped JSON and
           return its top-level fields as the metric.

        2. **Fallback: raw timeloop-mapper Summary Stats**, format::

               Cycles: 147456
               Energy: 1.58 uJ
               Utilization: 100.00%
               ...

           Used by the in-container /work/build_and_run.sh path when
           the challenge ships neither evaluate.sh nor simulate.sh.
        """
        # Path 1: marker-wrapped JSON (preferred when evaluate.sh runs
        # the show — see cnn_accelerator_codesign/evaluation/evaluate.sh).
        for start_tag, end_tag in (
            ("ARCHBENCH_JSON_START", "ARCHBENCH_JSON_END"),
            ("ARCHEVAL_JSON_START", "ARCHEVAL_JSON_END"),
        ):
            si = raw_output.find(start_tag)
            ei = raw_output.find(end_tag, si + 1) if si >= 0 else -1
            if si >= 0 and ei > si:
                blob = raw_output[si + len(start_tag):ei].strip()
                try:
                    import json as _json
                    metrics = _json.loads(blob)
                    if isinstance(metrics, dict) and metrics:
                        metrics["runs_successfully"] = 1
                        return metrics
                except Exception:
                    # malformed JSON inside markers → fall through to
                    # raw-stats parser; better to report SOMETHING than
                    # silently return None.
                    pass

        # Path 2: raw timeloop-mapper Summary Stats (in-container path).
        if "SIMULATION_OK" not in raw_output:
            return None

        metrics: dict = {}

        # Cycles (Summary Stats prints "Cycles: <N>")
        m = re.search(r"^Cycles\s*:\s*(\d+)", raw_output, re.MULTILINE)
        if m:
            metrics["total_cycles"] = int(m.group(1))

        # Energy — Summary Stats prints "Energy: 1.58 uJ"; convert to pJ
        m = re.search(
            r"^Energy\s*:\s*([0-9.eE+\-]+)\s*(pJ|nJ|uJ|mJ)",
            raw_output,
            re.MULTILINE,
        )
        if m:
            value = float(m.group(1))
            unit = m.group(2)
            scale = {"pJ": 1, "nJ": 1e3, "uJ": 1e6, "mJ": 1e9}.get(unit, 1)
            metrics["energy_pJ"] = round(value * scale)

        # Utilization — "Utilization: 100.00%" (percentage)
        m = re.search(r"Utilization\s*:\s*([0-9.]+)\s*%", raw_output)
        if m:
            metrics["utilization"] = round(float(m.group(1)) / 100.0, 4)

        # Energy per compute in fJ — "Total = 10693.79" under fJ/Compute section
        m = re.search(
            r"fJ/Compute.*?^\s*Total\s*=\s*([0-9.eE+\-]+)",
            raw_output,
            re.MULTILINE | re.DOTALL,
        )
        if m:
            fj = float(m.group(1))
            metrics["energy_per_mac_pJ"] = round(fj / 1000.0, 2)  # fJ -> pJ

        # Total computes — "Computes = 147456" or "Computes (total) : 147456"
        m = re.search(r"Computes\s*(?:\(total\)\s*)?[=:]\s*(\d+)", raw_output)
        if m:
            metrics["total_computes"] = int(m.group(1))

        # Derived metric: total energy in millijoules (from energy_pJ)
        if "energy_pJ" in metrics:
            metrics["total_energy_mj"] = round(metrics["energy_pJ"] / 1e9, 6)

        # Throughput: MACs/Cycle (or parse from output if available)
        m_throughput = re.search(
            r"(?:Throughput|MACs/Cycle)\s*[=:]\s*([0-9.eE+\-]+)",
            raw_output,
        )
        if m_throughput:
            metrics["throughput"] = round(float(m_throughput.group(1)), 4)
        elif "total_computes" in metrics and "total_cycles" in metrics:
            metrics["throughput"] = round(
                metrics["total_computes"] / metrics["total_cycles"], 4
            )

        # Mark as successfully run only if real metrics were extracted
        if len(metrics) > 0:
            metrics["runs_successfully"] = 1
            return metrics
        return None

    # ---- introspection ------------------------------------------------------

    def submission_files(self, challenge: Challenge) -> list[str]:
        """Timeloop challenges submit arch YAML files (arch.yaml, etc.)."""
        return list(challenge.output_files)

    def get_workload_files(self, challenge: Challenge) -> list[tuple[str, str]]:
        sim = challenge.simulator_config
        problem = sim.get("problem", "")
        files = []
        if problem:
            files.append(
                (f"timeloop/{problem}", f"/work/workloads/timeloop/{problem}")
            )
        # Always include default mapper config
        files.append(
            ("timeloop/mapper.yaml", "/work/workloads/timeloop/mapper.yaml")
        )
        # NAS-Bench-201 data for cnn_accelerator_codesign. Two artifacts,
        # both optional / staged only if present on the host:
        #   1. nasbench201_fixture.json — compact (~5 MB), torch-free table
        #      covering all 15625 archs (PREFERRED; built by
        #      challenges/cnn_accelerator_codesign/simulator/build_nasbench_fixture.py).
        #      Agent-side path: /workspace/data/nasbench201_fixture.json.
        #   2. NAS-Bench-201-v1_1-096897.pth — the full ~2 GB official
        #      benchmark (optional; only useful if torch + nats_bench are
        #      installed). Agent-side path: /workspace/data/nasbench201.pth.
        # Absent both → nasbench_helper.py falls back to STUB mode.
        repo_root = Path(__file__).resolve().parents[2]
        fixture_host = (
            repo_root / "workload_pools" / "nasbench"
            / "nasbench201_fixture.json"
        )
        if fixture_host.is_file():
            files.append(
                (str(fixture_host), "/workspace/data/nasbench201_fixture.json")
            )
        nasbench_host = (
            repo_root / "workload_pools" / "nasbench"
            / "NAS-Bench-201-v1_1-096897.pth"
        )
        if nasbench_host.is_file():
            files.append((str(nasbench_host), "/workspace/data/nasbench201.pth"))
        return files

    def export_workload_files(
        self, sim: ContainerManager, agent: ContainerManager,
        challenge: Challenge,
    ) -> None:
        """Push host-side workload artifacts into the agent container.

        Today Timeloop only needs this for the cnn_accelerator_codesign
        challenge: it consumes NAS-Bench-201 data so the agent can pick a
        "top-10%" CNN. Two artifacts are staged into ``/workspace/data/``
        (whichever exist on the host):

          1. ``nasbench201_fixture.json`` (PREFERRED) — a compact (~5 MB),
             torch-free table covering all 15625 architectures, built by
             ``challenges/cnn_accelerator_codesign/simulator/build_nasbench_fixture.py``.
             Lands at ``/workspace/data/nasbench201_fixture.json``. The
             bundled ``nasbench_helper.py`` prefers this.
          2. ``NAS-Bench-201-v1_1-096897.pth`` (OPTIONAL) — the full ~2 GB
             official benchmark, useful only if torch + nats_bench are
             present. Lands at ``/workspace/data/nasbench201.pth``.

        If NEITHER host file is present, this is (almost) a no-op — the
        helper script's STUB fallback kicks in and the agent gets
        deterministic synthetic data for indices 0..199. We always create
        ``/workspace/data`` so the agent's ``ls /workspace/data/`` doesn't
        surface a misleading "no such directory" error.

        Treating NAS-Bench-201 data as a "workload artifact" mirrors how
        ChampSim treats its decoded SPEC traces (which also flow through
        this hook): both are large immutable inputs the agent reads but
        can't legally edit.
        """
        repo_root = Path(__file__).resolve().parents[2]
        agent.exec("mkdir -p /workspace/data", timeout=10)

        staged_any = False

        # 1) Compact fixture JSON (preferred; cheap to copy).
        fixture_host = (
            repo_root / "workload_pools" / "nasbench"
            / "nasbench201_fixture.json"
        )
        if fixture_host.is_file():
            agent.copy_in(
                fixture_host, "/workspace/data/nasbench201_fixture.json")
            staged_any = True
            log.info(
                "exported NAS-Bench-201 fixture to agent: "
                "/workspace/data/nasbench201_fixture.json (%d bytes)",
                fixture_host.stat().st_size,
            )

        # 2) Full official .pth (optional; large).
        nasbench_host = (
            repo_root / "workload_pools" / "nasbench"
            / "NAS-Bench-201-v1_1-096897.pth"
        )
        if nasbench_host.is_file():
            agent.copy_in(nasbench_host, "/workspace/data/nasbench201.pth")
            staged_any = True
            log.info(
                "exported NAS-Bench-201 .pth to agent: "
                "/workspace/data/nasbench201.pth (%d bytes)",
                nasbench_host.stat().st_size,
            )

        if staged_any:
            agent.exec(
                "chown -R agent:agent /workspace/data 2>/dev/null || true",
                timeout=10,
            )
        else:
            log.info(
                "export_workload_files: no NAS-Bench-201 fixture or .pth "
                "under %s; agent will run nasbench_helper.py in STUB mode",
                repo_root / "workload_pools" / "nasbench",
            )

    def validate_challenge(self, challenge: Challenge) -> list[str]:
        errors = []
        sim = challenge.simulator_config
        if "problem" not in sim:
            errors.append("simulator_config.problem is required")
        return errors

    def default_source_blocklist(self, challenge: Challenge) -> list[str]:
        base = super().default_source_blocklist(challenge)
        # Block workloads to prevent leaking problem specs
        base.append("/work/workloads/timeloop/*")
        return base

    def build_system_prompt_extra(self, challenge: Challenge) -> str:
        """Kept for compatibility with legacy callers. Docs surfaced via /api/."""
        return ""
