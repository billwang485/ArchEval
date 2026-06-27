"""AgentRuntime ABC — the contract every agent runtime must satisfy.

This abstraction is new in the open-ended rewrite. In the legacy repo,
each runner (`runner.py`, `runner_codex.py`, `runner_gemini.py`,
`runner_archharness.py`, `runner_mini.py`) reimplemented the same
lifecycle with subtly different regexes for version parsing, auth
checks, and trajectory recording. That fork is what we collapse here.

A runtime brings:
- An agent docker image (with a baked `/work/verify.sh`).
- A way to assert the version of whatever it's wrapping (CLI binary,
  vLLM endpoint, …).
- A way to start an interactive session against an MCP server.
- A way to parse its trajectory stream into a normalized format.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional

from archbench.core.path_resolution import resolved_dirs

if TYPE_CHECKING:
    from archbench.core.container import ContainerManager


@dataclass
class RuntimeAuth:
    """How a runtime authenticates against its model backend.

    Exactly one of these fields should be populated:

    - `env_vars`: secrets passed via environment (Claude OAuth, OpenAI key).
    - `mount_files`: host paths to mount read-only into the container
      (Codex auth.json, Gemini OAuth creds).
    - `endpoint_url`: URL of a self-hosted model server (vLLM, etc.).
      No auth, but the runtime must HTTP-probe this before main loop.
    """

    env_vars: dict[str, str] = field(default_factory=dict)
    mount_files: dict[Path, str] = field(default_factory=dict)  # host → container
    endpoint_url: Optional[str] = None


class AgentRuntime(ABC):
    """Per-runtime backend interface.

    Lifecycle, in order, called by the runner:

        1. runtime.docker_image                       # identify image
        2. ensure_image(runtime.docker_image)         # load from tar
        3. runtime.verify_runtime(host_paths)         # preflight host-side
        4. agent = ContainerManager(...).start(auth)  # per-run, fresh
        5. runtime.verify_in_container(agent)         # version + tools + endpoint
        6. runtime.start_session(agent, mcp_url, prompt)  # blocking, returns trajectory
        7. agent.stop()                               # atexit-registered

    Per-run isolation rule: containers from runtime N never touch
    containers from runtime N-1. New uuid, new container name, atexit
    cleanup is unconditional.
    """

    # Set by session.run_session when --dev is passed. Subclasses MAY use
    # this to set up bind-mount volumes via the ContainerConfig.volumes
    # field (e.g., overlay <runtime_dir>/src/ over the image's baked path).
    # Off-the-shelf (bake_only) runtimes MUST ignore it (or assert False);
    # session.run_session enforces mode compatibility before --dev reaches
    # the runtime so bake_only runtimes never see dev_mode=True.
    dev_mode: bool = False

    # ---- identity ----

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable runtime slug, e.g. 'claude_code', 'archharness', 'mini'."""

    @property
    @abstractmethod
    def docker_image(self) -> str:
        """Fully-qualified image tag for the agent sandbox."""

    @property
    def docker_tar_name(self) -> Optional[str]:
        """Tarball filename on NFS. See SimulatorPlugin.docker_tar_name."""
        slug = self.docker_image.rsplit("/", 1)[-1].replace(":", "-")
        return f"{slug}.tar"

    @property
    @abstractmethod
    def expected_version(self) -> str:
        """Pinned version string. Asserted against `verify_in_container`.

        For CLI runtimes: the `--version` output. For self-hosted model
        runtimes: the model identifier or runtime image tag. Used to
        catch silent CLI-binary swaps.
        """

    # ---- preflight ----

    @abstractmethod
    def auth(self) -> RuntimeAuth:
        """Return the auth bundle for this runtime.

        Called by the runner before container start. The runner fails
        fast if any referenced host file is missing or any required env
        var is unset (no defaults, no silent fallback).
        """

    def verify_runtime(self, _challenge_dir: Path) -> list[str]:
        """Host-side preflight checks. Empty list = OK.

        Override to add checks beyond the auth bundle (e.g. a self-hosted
        vLLM endpoint is reachable). Default: no extra checks.
        """
        return []

    @abstractmethod
    def verify_in_container(self, agent: ContainerManager) -> list[str]:
        """In-container verify after start. Empty list = OK.

        MUST assert the runtime's binary/endpoint is callable AND its
        version matches `expected_version`. Past bug: archharness/mini
        skipped the endpoint probe; a dead vLLM only failed mid-round.
        """

    # ---- staging (P7 — agent/sim separation) ----

    def stage_workspace(
        self,
        agent: ContainerManager,
        challenge,
        starter_visibility: Literal["full", "none", "api_stub"] = "full",
    ) -> None:
        """Drop challenge files into the agent container's /workspace/ and /api/.

        Default impl handles the universal staging (protocol v3):
          • starter_code → /workspace/starter/ (read-only reference) for every
            tier; ADDITIONALLY mirrored to the /workspace/ root (the agent's
            authoring/submission position) for the L1 tier ONLY
          • pre-submit validate.py (legacy: check_storage.py) → /workspace/ when present
          • API docs (challenge.challenge_dir/docs/) → /api/
          • requirements.txt (if present) → pip install --user
          • chown -R agent:agent

        `starter_visibility` is a deprecated no-op: protocol v3 stages the
        starter to /workspace/starter/ for every tier (and additionally to the
        /workspace/ root for L1) regardless of its value. It is still validated
        for legacy callers (unknown values raise) but does not change staging.

        Runtimes that need extra setup (e.g. Claude writes prompt.md
        because its CLI takes --prompt-file) override AND call super().
        """
        from pathlib import Path as _P
        if not challenge.challenge_dir:
            return
        # Tier-aware resolution: `sim_dir` / `eval_dir` / `starter_dir`
        # point at the canonical locations regardless of layout (tier vs
        # legacy 3-subdir). In tier mode these can resolve under
        # `<family>/common/`; in legacy mode they collapse to
        # `<challenge_dir>/simulator` / `<challenge_dir>/evaluation` so
        # the candidate lists below stay backward-compatible.
        sim_dir, eval_dir, starter_dir = resolved_dirs(challenge)
        agent.exec(
            "mkdir -p /workspace/starter /api /traces/decoded",
            timeout=10,
        )
        # Protocol v3 (2026-06-19): the family starter is ALWAYS staged to
        # /workspace/starter/ (read-only reference). It is ADDITIONALLY mirrored
        # to the /workspace/ root (the agent's authoring/submission position)
        # ONLY for the L1 tier — the most-help arm — so L1 tunes the runnable
        # baseline config in place and its floor is ~baseline (the starter
        # reproduces baseline.json), i.e. the most-help arm can't embarrassingly
        # dip below baseline. L2/L3 author from an EMPTY root (build from
        # scratch) and so may fall below baseline; that gap is exactly the
        # ablation signal (value of the L1 scaffold). v2 = reference-only for
        # every tier; v3 = + L1 root. v2/v3 are NOT poolable. The legacy
        # starter_visibility values are accepted but IGNORED; tiers differ by
        # approach (budget / environment / evaluators), not by what they show.
        if starter_visibility not in ("full", "none", "api_stub"):
            raise ValueError(
                f"unknown starter_visibility={starter_visibility!r}; "
                "expected one of 'full', 'none', 'api_stub' (all staged identically)"
            )
        _cid = str(getattr(challenge, "id", "") or "")
        _cdir = str(getattr(challenge, "challenge_dir", "") or "").replace("\\", "/").rstrip("/")
        is_l1 = _cid.endswith("_L1") or _cdir.endswith("/assisted/L1")
        import logging as _logging
        _logging.getLogger("archbench.runtime").info(
            "protocol v3: staging starter to /workspace/starter/ (reference)%s; "
            "starter_visibility=%r ignored",
            " AND /workspace/ root (L1 authoring)" if is_l1 else " only (L2/L3 author from empty root)",
            starter_visibility,
        )
        for fname, content in challenge.starter_code.items():
            agent.write_file(fname, content, base_dir="/workspace/starter")
            if is_l1:
                agent.write_file(fname, content, base_dir="/workspace")

        # Stage the conventional advisory self-check helper when present.
        # This does not participate in submit classification: authoritative
        # validation belongs to each challenge's evaluate.sh. Keeping this
        # helper in /workspace lets agents run the existing checker themselves.
        #
        # Tier-mode canonical location is `sim_dir/validate.py` —
        # which may live under `<family>/common/simulator/` and is NOT
        # reachable from `challenge.challenge_dir` alone. Probe it first;
        # in legacy mode `sim_dir == challenge_dir/simulator` so the
        # second candidate is identical (backward compat preserved).
        # New canonical name is validate.py; check_storage.py is the legacy
        # name (champsim, where it checks a storage bit-budget). Probe both,
        # stage the one found under its OWN name.
        _check_names = ["validate.py", "check_storage.py"]
        _check_dirs = [
            sim_dir,                                       # tier-mode canonical
            challenge.challenge_dir / "simulator",         # legacy 3-subdir
            challenge.challenge_dir,                        # very-old (root)
            challenge.challenge_dir / "eval",              # legacy "eval/"
            challenge.challenge_dir / "evaluation",        # post-Phase-H
        ]
        for src in [d / n for n in _check_names for d in _check_dirs]:
            if src.exists():
                agent.write_file(src.name, src.read_text(), base_dir="/workspace")
                agent.exec(f"chmod +x /workspace/{src.name}", timeout=10)
                break

        # API docs: probe tier-aware locations first (sim_dir/docs,
        # eval_dir/docs — under <family>/common/ in tier mode), then
        # the legacy challenge_dir/docs/, then repo-root docs/<sim>/.
        # In legacy 3-subdir mode sim_dir == challenge_dir/simulator,
        # so the sim_dir/docs probe is harmless (rarely exists there)
        # and challenge_dir/docs remains the canonical legacy hit.
        docs_candidates = [
            sim_dir / "docs",                       # tier-mode canonical
            eval_dir / "docs",                      # tier-mode alt
            challenge.challenge_dir / "docs",       # legacy challenge-local
        ]
        docs_dir = next((d for d in docs_candidates if d.is_dir()), None)
        if docs_dir is None:
            repo_root = _P(__file__).resolve().parents[2]
            docs_dir = repo_root / "docs" / challenge.simulator
        if docs_dir.is_dir():
            for fpath in sorted(docs_dir.rglob("*")):
                if fpath.is_file():
                    rel = fpath.relative_to(docs_dir)
                    ctr_dir = f"/api/{rel.parent}" if str(rel.parent) != "." else "/api"
                    agent.exec(f"mkdir -p {ctr_dir}", timeout=5)
                    agent.write_file(rel.name, fpath.read_text(),
                                     base_dir=ctr_dir)

        # Install Python deps declared in challenge's requirements.txt.
        # System-wide (no --user): docker exec defaults to root, but the
        # actual agent loop runs as `agent` (uid 1000) via --user agent.
        # System-wide install (/usr/local/lib/python3.X/site-packages) is
        # readable by both users; --user would land in /root/.local and
        # the agent process wouldn't see it.
        # requirements.txt may sit at the challenge root or under one of
        # the standard helper subdirs (eval/ legacy, simulator/ or
        # evaluation/ post-Phase-H). In tier mode the canonical location
        # is `sim_dir/requirements.txt` under `<family>/common/simulator/` —
        # probe it FIRST so deps actually install for tier-mode challenges.
        # In legacy mode sim_dir == challenge_dir/simulator, so the second
        # candidate is identical (backward compat preserved).
        reqs_candidates = [
            sim_dir / "requirements.txt",                               # tier-mode canonical
            challenge.challenge_dir / "simulator" / "requirements.txt", # legacy 3-subdir
            eval_dir / "requirements.txt",                              # tier-mode (eval-only deps)
            challenge.challenge_dir / "evaluation" / "requirements.txt",
            challenge.challenge_dir / "eval" / "requirements.txt",
            challenge.challenge_dir / "requirements.txt",
        ]
        reqs = next((p for p in reqs_candidates if p.is_file()), reqs_candidates[0])
        if reqs.is_file():
            agent.write_file("requirements.txt", reqs.read_text(),
                             base_dir="/workspace")
            agent.exec(
                "pip install --no-warn-script-location --break-system-packages "
                "-r /workspace/requirements.txt 2>&1 | tail -5",
                timeout=180,
            )

        agent.exec(
            "chown -R agent:agent /workspace /api /traces 2>/dev/null || true",
            timeout=10,
        )

    # ---- run ----

    @abstractmethod
    def start_session(
        self,
        agent: ContainerManager,
        mcp_url: str,
        prompt: str,
        round_timeout: int,
    ) -> Path:
        """Start one agent session. Returns path to its trajectory file.

        Blocking. The runner enforces `round_timeout` as a SIGTERM
        deadline; the runtime is expected to flush partial state to
        disk on signal (so SLURM preemption doesn't lose trajectories).
        """

    def to_canonical_trajectory(self, native_trajectory_path) -> list[dict]:
        """Convert THIS runtime's native trajectory into the canonical schema
        (``archbench/core/trajectory.py``) — the per-agent anti-corruption layer.

        Every runtime emits a different native trajectory; downstream analysis
        AND trajectory-reading evaluators consume ONLY the canonical form, so
        they never re-implement per-runtime parsing. Each runtime overrides this
        with its own adapter; the default raises so a runtime without one is
        loud (session.py catches it, logs, and continues — the run still
        produces the native trajectory.jsonl).

        Returns a list of canonical STEP dicts (build them with
        ``archbench.core.trajectory.step``).
        """
        raise NotImplementedError(
            f"runtime {self.name!r} has no to_canonical_trajectory adapter "
            f"(see archbench/core/trajectory.py for the schema)"
        )

    # ---- shared helper for self-healing verify.sh injection ----

    def _ensure_verify_script(self, agent: ContainerManager,
                              runtime_subdir: str) -> None:
        """If /work/verify.sh isn't in the image, inject it from host.

        Idempotent. Used during the open-ended rewrite transition: legacy
        agent images were built without verify.sh; once they're rebuilt
        from this repo's Dockerfile (which COPYs verify.sh), this becomes
        a fast no-op.

        `runtime_subdir` is the runtime's subdirectory under `runtimes/`
        (e.g. "claude_code", "codex"); the script is sourced from
        `runtimes/<runtime_subdir>/verify.sh`.
        """
        from pathlib import Path as _P
        out, _ = agent.exec(
            "test -x /work/verify.sh && echo OK || echo MISS",
            timeout=10,
        )
        if "OK" in out:
            return
        repo_root = _P(__file__).resolve().parents[2]
        host_path = repo_root / "runtimes" / runtime_subdir / "verify.sh"
        if not host_path.exists():
            return  # nothing we can do; verify_in_container will surface this
        agent.exec("mkdir -p /work", timeout=10)
        agent.copy_in(host_path, "/work/verify.sh")
        agent.exec("chmod +x /work/verify.sh", timeout=10)
