"""SimulatorPlugin ABC — the contract every simulator backend must satisfy.

A plugin encapsulates everything simulator-specific: which docker image to
use, how to verify it's wired up correctly, how to reset it between runs,
how to inject agent code, and how to parse the simulator's output into a
metrics dict.

Adding a new simulator = subclass this + write a Dockerfile + register in
`archbench/simulators/__init__.py`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from archbench.core.container import ContainerManager
    from archbench.core.challenge import Challenge


class SimulatorPlugin(ABC):
    """Per-simulator backend interface.

    Lifecycle, in order, called by the runner:

        1. plugin.docker_image                  # identify the image
        2. ensure_image(plugin.docker_image)    # load from tar if needed
        3. sim = ContainerManager(...).start()  # per-run, long-lived
        4. plugin.verify_simulator(sim)         # must return [] (no errors)
        5. plugin.configure_simulator(sim, ch)  # one-time per-challenge setup
        6. for each submit:
               plugin.run_submit(sim, agent_files)   -> raw_output
               plugin.parse_output(raw_output)       -> metrics dict
               plugin.cleanup_simulator(sim)         # reset for next submit
        7. sim.stop()  # registered with atexit at start time

    The same `verify_simulator` is also called *after* every
    `cleanup_simulator` to confirm the reset was complete. If a cleanup
    bug leaves stale state behind, the next submit fails loudly instead of
    silently inheriting the previous run's code.
    """

    # ---- identity ----

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable simulator slug, e.g. 'champsim'. Used in paths."""

    @property
    @abstractmethod
    def docker_image(self) -> str:
        """Fully-qualified image tag, e.g. 'localhost/archbench-champsim:v6'."""

    @property
    def docker_tar_name(self) -> Optional[str]:
        """Filename of the pre-built image tarball on shared storage.

        Returning None means "image must already be present on this node".
        Default convention: 'archbench-<name>-<tag>.tar' under
        `docker/<name>/`.
        """
        slug = self.docker_image.rsplit("/", 1)[-1].replace(":", "-")
        return f"{slug}.tar"

    # ---- lifecycle ----

    @abstractmethod
    def verify_simulator(self, sim: ContainerManager) -> list[str]:
        """Return a list of error strings; empty list = OK.

        Must check whatever invariants the simulator container needs.
        Called on a freshly started container AND after every cleanup.
        Should be fast (< 5s).
        """

    @abstractmethod
    def configure_simulator(
        self, sim: ContainerManager, challenge: Challenge
    ) -> None:
        """One-time per-challenge setup (e.g. copy config, run config.sh).

        Called once after `verify_simulator` and before the first submit.
        """

    @abstractmethod
    def cleanup_simulator(self, sim: ContainerManager) -> None:
        """Reset the container to a state where `verify_simulator` passes.

        Called after every submit. Must remove:
        - Agent-submitted source files
        - Build artifacts
        - Per-submit temp files

        Convention: call the in-container `/work/cleanup.sh` script
        baked into the image — DO NOT re-implement the cleanup logic in
        Python. (Past bug: inline Python and the .sh diverged.)
        """

    @abstractmethod
    def run_submit(
        self, sim: ContainerManager, challenge: Challenge,
        agent_files: dict[str, str],
    ) -> str:
        """Inject agent_files into the sim container, build + run, return raw stdout.

        Raises on infrastructure failure (container died, etc.).
        Returns raw output even on build/sim failure — the parsing layer
        decides whether to emit BUILD_FAIL, SIM_TIMEOUT, or SIM_OK.
        """

    # ---- parsing ----

    @abstractmethod
    def parse_output(self, raw_output: str) -> Optional[dict]:
        """Parse `run_submit` stdout into a metrics dict.

        Returns None only if parsing genuinely cannot extract anything
        (caller turns this into BUILD_FAIL / SIM_TIMEOUT based on raw
        markers). Never raises on missing fields — be lenient on input,
        strict on output.
        """

    # ---- introspection (for the connector) ----

    def submission_files(self, challenge: Challenge) -> list[str]:
        """Filenames the connector will copy from agent workspace into sim.

        Default: whatever `challenge.output_files` declares. Override if
        the simulator needs additional files (e.g. a config.json).
        """
        per_sim = self._per_sim_submission_files(challenge)
        if per_sim is not None:
            return per_sim
        return list(challenge.output_files)

    def _per_sim_submission_files(
        self, challenge: Challenge,
    ) -> Optional[list[str]]:
        """Per-sim submission-file split for multi-sim challenges, or None.

        In a multi-sim session (docs/multi_sim_design.md) each sim's
        ``submit`` tool must accept only ITS files — but ``submit`` validates
        the agent's ``implementation_paths`` against
        ``plugin.submission_files(challenge)``, and every bound sim shares
        ONE ``challenge`` whose ``output_files`` is the union of all sims'
        files. Without a split, ``dramsys_submit`` would demand the ramulator
        file too (count mismatch → BUILD_FAIL).

        A challenge declares the split under
        ``simulator_config.submission_files`` as a mapping keyed by sim name::

            simulator_config:
              submission_files:
                dramsys:   [config.json, mc_config.json]
                ramulator: [ramulator_config.yaml]

        This method returns the entry for THIS plugin's ``name`` if such a
        mapping is present, else None (so single-sim challenges, which never
        set the key, fall through to ``challenge.output_files`` unchanged).
        A plain list value (not a mapping) is ALSO honored — it applies to
        every sim, which is only meaningful single-sim.
        """
        sim_cfg = getattr(challenge, "simulator_config", None) or {}
        spec = sim_cfg.get("submission_files")
        if isinstance(spec, dict):
            files = spec.get(self.name)
            if isinstance(files, list) and files:
                return list(files)
            return None
        if isinstance(spec, list) and spec:
            return list(spec)
        return None

    def default_source_blocklist(self, challenge: Challenge) -> list[str]:
        """Paths the agent must NOT be able to read via the connector.

        Prevents the agent from peeking at solution files or
        challenge-specific source inside the sim container. Subclasses
        should call super() and extend.
        """
        return [
            "/work/challenges/*/solution/*",
            "/work/challenges/*/solution",
        ]

    def validate_challenge(self, challenge: Challenge) -> list[str]:
        """Return list of YAML-validation errors for this challenge.

        Default: no checks. Override to enforce simulator-specific schema.
        """
        return []

    # ---- workloads ----

    def get_workload_files(
        self, challenge: Challenge
    ) -> list[tuple[str, str]]:
        """Return (host_rel_path, container_abs_path) pairs to mount.

        host_rel_path is resolved relative to ARCHBENCH_WORKLOADS_DIR (see
        README §Setup). The runner fails fast if any host path is missing.
        """
        return []

    def export_workload_files(
        self, sim: ContainerManager, agent: ContainerManager,
        challenge: Challenge,
    ) -> None:
        """Push workload artifacts from sim → agent for inspection.

        For ChampSim this is "copy decoded *.trace.txt out of sim into the
        agent's /traces/decoded/". The plugin owns this because only it
        knows where the workload artifacts live inside the sim image and
        what format the agent expects.

        Default: no-op (text-only or no-workload simulators).
        Called once at session setup, after configure_simulator + agent
        verify, before start_session.
        """
        return None
