"""ContainerManager — per-run podman/docker wrapper with hard cleanup.

Structural rules this module enforces (see docs/lessons_learned.md §5):

1. **Per-run names**: every container name includes a uuid hex prefix.
2. **atexit + SIGTERM cleanup**: `start()` registers an unconditional
   `docker rm -f` hook. SLURM preempted? Process killed? Container goes
   away regardless. No orphans.
3. **No silent fallbacks** in `ensure_image()`: image not local AND no
   tarball → raise. We never attempt `docker pull` or assume an image
   "will probably be there".
4. **Digest verification**: caller may pass `expected_digest`. If the
   loaded image's actual digest differs, `ensure_image()` raises.

The container *operations* (exec, copy_in, etc.) classify failures into
two typed exceptions so the caller layer can decide recovery:

- `ContainerDeadError` — the container exited unexpectedly; reset state
  needed.
- `ImageNotFoundError` — the image disappeared from local storage
  (NFS-cached podman rootless gets evicted under pressure).
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from archbench.image_management.engine import container_engine
from archbench.image_management.site import load_site

log = logging.getLogger("archbench.container")

# Output truncation limits (characters). Past bug: agent exec output
# overran the LLM context and broke streaming parsers.
OUTPUT_LIMIT = 30_000
READ_FILE_LIMIT = 500_000


# ---------------------------------------------------------------------------
# Typed exceptions — every recovery path keys off one of these
# ---------------------------------------------------------------------------


class ContainerDeadError(RuntimeError):
    """The container exited; further ops will fail until restart."""


class ImageNotFoundError(RuntimeError):
    """The docker image isn't loaded on this node; needs tarball reload."""


class ImageDigestMismatch(RuntimeError):
    """Loaded image's digest differs from the caller's `expected_digest`.

    Most likely cause: someone rebuilt the image under the same tag.
    The fix is to bump the tag or re-export the tarball, not to ignore.
    """


class ImageCardMismatch(RuntimeError):
    """The loaded image's live contents don't match its container card
    (archbench/core/container_card.py) — a stale/wrong image caught AT LOAD,
    naming exactly which paths/hashes/neutrality checks failed.
    """


# ---------------------------------------------------------------------------
# Container lifecycle
# ---------------------------------------------------------------------------


@dataclass
class ContainerConfig:
    """One container's start-time configuration."""

    image: str
    container_name: str
    # `mounts` entries are `(host_path, container_path, mode)` where mode
    # is "ro" or "rw". Hosts paths must exist; we fail-fast if not.
    mounts: list[tuple[Path, str, str]] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # "host" for MCP-over-localhost. "none" for strict isolation.
    network: str = "host"
    memory: str = "8g"
    cpus: str = "4"
    pids_limit: int = 512
    labels: dict[str, str] = field(default_factory=dict)
    # `volumes` is a simple host_path -> container_path map for bind-mounts
    # without explicit mode (defaults to rw). Used by dev-mode runtimes to
    # overlay their src/ over the baked binary path inside the image.
    # Host paths must exist; we fail-fast if not, same rule as `mounts`.
    volumes: dict[Path, str] = field(default_factory=dict)

    @classmethod
    def with_run_id(cls, image: str, name_prefix: str, **kwargs) -> "ContainerConfig":
        """Construct a config with a uuid-suffixed container name.

        Per-run rule: no two runs share a container name, ever.
        """
        run_id = uuid.uuid4().hex[:8]
        return cls(
            image=image,
            container_name=f"{name_prefix}_{run_id}",
            **kwargs,
        )


# Class-level registry of all live containers, for the SIGTERM/atexit
# sweep. Threading lock because runners may start multiple containers
# (agent + sim) concurrently.
_LIVE_CONTAINERS: set[str] = set()
_LIVE_LOCK = threading.Lock()
_CLEANUP_INSTALLED = False


def _install_cleanup_handlers() -> None:
    """Install atexit + SIGTERM handlers, once per process."""
    global _CLEANUP_INSTALLED
    if _CLEANUP_INSTALLED:
        return
    _CLEANUP_INSTALLED = True

    def _sweep():
        # Snapshot under lock; the rm calls don't need the lock.
        with _LIVE_LOCK:
            names = list(_LIVE_CONTAINERS)
        for name in names:
            try:
                subprocess.run(
                    [container_engine(), "rm", "-f", name],
                    capture_output=True, timeout=10,
                )
            except Exception:
                pass

    atexit.register(_sweep)

    # SIGTERM = SLURM scancel, kubernetes pod kill, etc. We re-raise
    # the default action after our sweep so the process still dies.
    prev_term = signal.getsignal(signal.SIGTERM)

    def _on_sigterm(signum, frame):
        _sweep()
        if callable(prev_term):
            prev_term(signum, frame)
        else:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGTERM)

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:
        # Not on main thread — signal install will fail. atexit alone is
        # still better than nothing.
        pass


class ContainerManager:
    """Per-run container handle. No challenge or simulator awareness."""

    _IMAGE_LOST_MARKERS = (
        "image not known", "image not found",
        "manifest unknown", "unable to find image",
    )
    _DEAD_MARKERS = (
        "container state improper",
        "is not running",
        "no such container",
    )

    def __init__(self, config: ContainerConfig):
        self.config = config
        self._running = False
        self._last_death_info: Optional[str] = None
        _install_cleanup_handlers()

    @property
    def name(self) -> str:
        return self.config.container_name

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        """`docker run -d ... sleep infinity` with cleanup-hook registration.

        Refuses to silently inherit a leftover container of the same
        name. If you have a name collision, that's a bug elsewhere — we
        `rm -f` only as a safety swab before our own run.
        """
        # Pre-clean any stale leftover with the same name (e.g. last run
        # crashed before atexit could fire). This is the only "silent rm"
        # we do, and only on the OWN run's name.
        subprocess.run(
            [container_engine(), "rm", "-f", self.config.container_name],
            capture_output=True, timeout=60,
        )

        cmd = [
            container_engine(), "run", "-d",
            "--name", self.config.container_name,
            "--memory", self.config.memory,
            "--cpus", self.config.cpus,
            "--pids-limit", str(self.config.pids_limit),
            "--network", self.config.network,
        ]
        for host, ctr, mode in self.config.mounts:
            host = Path(host)
            if not host.exists():
                raise FileNotFoundError(
                    f"Mount source missing on host: {host}. "
                    "Refusing to start container with a broken mount."
                )
            cmd.extend(["-v", f"{host}:{ctr}:{mode}"])
        for host, ctr in self.config.volumes.items():
            host = Path(host)
            if not host.exists():
                raise FileNotFoundError(
                    f"Volume source missing on host: {host}. "
                    "Refusing to start container with a broken volume."
                )
            cmd.extend(["-v", f"{host}:{ctr}"])
        for k, v in self.config.env.items():
            cmd.extend(["-e", f"{k}={v}"])
        for k, v in self.config.labels.items():
            cmd.extend(["--label", f"{k}={v}"])
        cmd.extend([self.config.image, "sleep", "infinity"])

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=900,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"docker run timed out after 900s for image "
                f"{self.config.image}"
            ) from e

        if result.returncode != 0:
            stderr_lc = result.stderr.lower()
            if any(m in stderr_lc for m in self._IMAGE_LOST_MARKERS):
                raise ImageNotFoundError(
                    f"Image {self.config.image!r} not found: "
                    f"{result.stderr.strip()}"
                )
            raise RuntimeError(
                f"docker run failed (rc={result.returncode}): "
                f"{result.stderr.strip()}"
            )

        self._running = True
        with _LIVE_LOCK:
            _LIVE_CONTAINERS.add(self.config.container_name)

    def stop(self) -> None:
        """`docker rm -f`. Idempotent; safe to call repeatedly."""
        with _LIVE_LOCK:
            _LIVE_CONTAINERS.discard(self.config.container_name)
        try:
            subprocess.run(
                [container_engine(), "rm", "-f", self.config.container_name],
                capture_output=True, timeout=30,
            )
        except Exception as e:
            log.warning("Container cleanup failed for %s: %s", self.name, e)
        self._running = False

    def is_alive(self) -> bool:
        """Probe `docker inspect`. Captures exit reason on death."""
        try:
            result = subprocess.run(
                [container_engine(), "inspect", "-f",
                 "{{.State.Running}}|{{.State.Status}}|{{.State.ExitCode}}|{{.State.Error}}",
                 self.config.container_name],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0 and "true" in result.stdout.lower():
                return True
            if result.returncode == 0:
                self._last_death_info = result.stdout.strip()
            else:
                self._last_death_info = f"inspect rc={result.returncode}: {result.stderr.strip()}"
            return False
        except Exception as e:
            self._last_death_info = f"inspect raised: {e}"
            return False

    # ---- exec / file ops ----

    def exec(
        self, command: str,
        workdir: str = "/",
        timeout: int = 600,
        output_limit: Optional[int] = None,
    ) -> tuple[str, int]:
        """Run a bash command. Returns (combined_output, returncode).

        Container death is detected and surfaced as `returncode == -2`
        with a CONTAINER_DEAD-prefixed output. Caller decides recovery —
        we do not auto-restart (that would silently lose challenge state).
        """
        try:
            result = subprocess.run(
                [
                    container_engine(), "exec",
                    "-w", workdir,
                    self.config.container_name,
                    "bash", "-c", command,
                ],
                capture_output=True, text=True, timeout=timeout,
            )
            output = result.stdout + result.stderr
            rc = result.returncode
        except subprocess.TimeoutExpired:
            # Kill the in-container child so it doesn't keep burning
            # resources after our timeout (legacy bug: orphan compiler
            # processes held locks for hours).
            try:
                subprocess.run(
                    [container_engine(), "exec", self.config.container_name,
                     "pkill", "-9", "-f", "build_and_run"],
                    timeout=10, capture_output=True,
                )
            except Exception:
                pass
            output = f"ERROR: command timed out after {timeout}s"
            rc = -1

        if any(m in output.lower() for m in self._DEAD_MARKERS):
            self._running = False
            output = (
                f"CONTAINER_DEAD: {self.config.container_name} is not "
                f"running. Original output: {output.strip()}"
            )
            rc = -2

        limit = output_limit or OUTPUT_LIMIT
        if len(output) > limit:
            half = limit // 2
            output = (
                output[:half]
                + f"\n\n... [truncated {len(output) - limit} chars] ...\n\n"
                + output[-half:]
            )
        return output, rc

    def write_file(self, path: str, content: str,
                   base_dir: str = "/") -> None:
        """Copy a string into a file inside the container.

        Raises `ContainerDeadError` on container death (do NOT swallow).
        """
        if ".." in os.path.normpath(path).split(os.sep):
            raise ValueError(f"Path traversal not allowed: {path!r}")

        ctr_path = path if path.startswith("/") else f"{base_dir.rstrip('/')}/{path}"
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=Path(path).suffix, delete=False,
        ) as f:
            f.write(content)
            tmp = f.name
        try:
            result = subprocess.run(
                [container_engine(), "cp", tmp,
                 f"{self.config.container_name}:{ctr_path}"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                err = result.stderr.lower()
                if any(m in err for m in self._DEAD_MARKERS):
                    self._running = False
                    raise ContainerDeadError(
                        f"{self.config.container_name} dead (write_file {ctr_path})"
                    )
                raise RuntimeError(f"docker cp failed: {result.stderr.strip()}")
        finally:
            os.unlink(tmp)

    def read_file(self, path: str, base_dir: str = "/",
                  limit: int = READ_FILE_LIMIT) -> str:
        if ".." in os.path.normpath(path).split(os.sep):
            raise ValueError(f"Path traversal not allowed: {path!r}")
        full = path if path.startswith("/") else f"{base_dir.rstrip('/')}/{path}"
        try:
            result = subprocess.run(
                [container_engine(), "exec", self.config.container_name, "cat", full],
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            return "ERROR: read timed out"
        if result.returncode != 0:
            err = result.stderr.lower()
            if any(m in err for m in self._DEAD_MARKERS):
                self._running = False
                raise ContainerDeadError(
                    f"{self.config.container_name} dead (read_file {full})"
                )
            return f"ERROR: {result.stderr.strip()}"
        out = result.stdout
        if len(out) > limit:
            out = out[:limit] + f"\n... [truncated, {len(result.stdout)} total bytes]"
        return out

    def list_files(self, path: str = "/") -> str:
        try:
            result = subprocess.run(
                [container_engine(), "exec", self.config.container_name, "ls", "-la", path],
                capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            return "ERROR: list timed out"
        if result.returncode != 0:
            err = result.stderr.lower()
            if any(m in err for m in self._DEAD_MARKERS):
                self._running = False
                raise ContainerDeadError(
                    f"{self.config.container_name} dead (list_files {path})"
                )
        return result.stdout + result.stderr

    def copy_in(self, host_path: Path, container_path: str) -> None:
        if not host_path.exists():
            raise FileNotFoundError(
                f"copy_in source missing on host: {host_path}"
            )
        try:
            # 300s allows for slow NFS or large binaries; staging to local
            # first (preferred) is in caller's hands.
            result = subprocess.run(
                [container_engine(), "cp", str(host_path),
                 f"{self.config.container_name}:{container_path}"],
                capture_output=True, text=True, timeout=300,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"docker cp timed out: {host_path} -> {container_path}"
            ) from e
        if result.returncode != 0:
            err = result.stderr.lower()
            if any(m in err for m in self._DEAD_MARKERS):
                self._running = False
                raise ContainerDeadError(
                    f"{self.config.container_name} dead (copy_in {container_path})"
                )
            raise RuntimeError(f"docker cp failed: {result.stderr.strip()}")

    def copy_out(self, container_path: str, host_path: Path) -> None:
        host_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                [container_engine(), "cp",
                 f"{self.config.container_name}:{container_path}",
                 str(host_path)],
                capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"docker cp timed out: {container_path} -> {host_path}"
            ) from e
        if result.returncode != 0:
            err = result.stderr.lower()
            if any(m in err for m in self._DEAD_MARKERS):
                self._running = False
                raise ContainerDeadError(
                    f"{self.config.container_name} dead (copy_out {container_path})"
                )
            raise RuntimeError(f"docker cp failed: {result.stderr.strip()}")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()


# ---------------------------------------------------------------------------
# Image lifecycle: ensure + digest verify
# ---------------------------------------------------------------------------


def get_image_digest(image: str) -> Optional[str]:
    """Return local image's sha256 ID, or None if not loaded."""
    try:
        result = subprocess.run(
            [container_engine(), "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def _load_from_tar(image: str, tar_path: Path) -> None:
    """Run `docker load` from a tarball. Raises on failure.

    Uses an exclusive flock on a tar-specific tmp file so concurrent
    SLURM array jobs / parallel evaluator subshells don't race to load
    the same tar (lessons §9). Double-check after acquiring the lock —
    a peer may have loaded it while we waited.

    Verifies the load actually produced the expected image tag — bare
    `docker load` exits 0 even if the tar contained a *different* tag.
    """
    import fcntl
    # Lock dir is the per-machine scratch dir (site.yaml lock_dir, default
    # scratch_dir = $TMPDIR or /tmp — identical to today's hardcoded /tmp when
    # TMPDIR is unset). docs/docker_management.md §6.
    lock_dir = load_site().lock_dir
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"archbench_docker_load_{tar_path.name}.lock"
    lock_path.touch(exist_ok=True)

    log.info("Loading %s from %s (%.1f GB) [flock %s]...",
             image, tar_path.name, tar_path.stat().st_size / 1e9, lock_path.name)
    with open(lock_path, "w") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        # Re-check after lock acquisition; peer may have loaded meanwhile.
        if get_image_digest(image) is not None:
            log.info("%s loaded by a peer during flock wait — done", image)
            return
        try:
            result = subprocess.run(
                [container_engine(), "load", "-i", str(tar_path)],
                capture_output=True, text=True, timeout=1800,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"docker load of {tar_path} timed out after 30 minutes"
            ) from e
    if result.returncode != 0:
        raise RuntimeError(
            f"docker load failed (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )
    if get_image_digest(image) is None:
        loaded_tag = "unknown"
        if "Loaded image:" in result.stdout:
            loaded_tag = result.stdout.split("Loaded image:")[-1].strip()
        raise RuntimeError(
            f"Tar {tar_path} loaded as {loaded_tag!r} but expected {image!r}. "
            "The tarball is stale relative to the requested tag; "
            "rebuild the image and re-save the tar."
        )


# ---------------------------------------------------------------------------
# Autobuild fallback (K5 — docs/docker_management.md §7 hop 4, §8 `build`).
#
# These make a fresh clone with no tar runnable: when an image is neither local
# nor in any tar pool, ensure_image() can BUILD it from its manifest Dockerfile
# (gated — see ensure_image). The same staging + build helpers back
# `archbench images build`. All engine calls route through container_engine().
# ---------------------------------------------------------------------------


def stage_workloads_for_build(sim: str, repo_root: Path) -> Optional[Path]:
    """Stage ``site.workloads_dir/<sim>`` into ``<repo>/workload_pools/<sim>``
    so a simulator Dockerfile's relative ``COPY workload_pools/<sim>/`` resolves
    on a fresh clone (docs §8 — the autobuild blocker).

    ``workload_pools/`` is a gitignored symlink (absent on a fresh clone), and
    an absent ``COPY`` source makes champsim/gem5 builds FAIL. If
    ``<repo>/workload_pools/<sim>/`` already exists (the origin box, where
    ``workload_pools`` is a symlink into the lab pool), this is a NO-OP and
    returns the existing path — behavior-preserving. Otherwise we create
    ``<repo>/workload_pools/`` and symlink ``<sim>`` -> ``site.workloads_dir/<sim>``.

    Returns the staged path, or ``None`` when the sim has no workloads in the
    site pool (e.g. gem5 compiles its binary inline; runtime-only images have
    none) — the caller then builds without staging.
    """
    pool_dir = repo_root / "workload_pools"
    target = pool_dir / sim
    if target.exists():
        return target  # already present (origin-box symlink or prior stage)

    site = load_site()
    src = site.workloads_dir / sim
    if not src.exists():
        # No workloads for this sim in the site pool. Not an error here — the
        # Dockerfile may not COPY workloads (gem5/runtime images). If it DOES,
        # the build will fail loudly at the COPY step, which is the correct
        # signal ("set workloads_dir", docs §9), not something to fabricate.
        log.warning(
            "no workloads for %r at %s; building without staging "
            "(if the Dockerfile COPYs workload_pools/%s/ the build will fail "
            "— set workloads_dir in site.yaml)", sim, src, sim,
        )
        return None

    pool_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(src, target)
        log.info("staged workloads: %s -> %s", target, src)
    except OSError as e:
        raise RuntimeError(
            f"could not stage workloads for {sim!r}: symlink {target} -> "
            f"{src} failed: {e}"
        ) from e
    return target


def build_image_from_manifest(
    image: str,
    repo_root: Optional[Path] = None,
    dry_run: bool = False,
) -> Optional[list[str]]:
    """Build ``image`` from its ``images.yaml`` Dockerfile via the engine.

    Resolves ``(dockerfile, context)`` from the manifest reverse lookup
    (images.build_context_for). For a SIMULATOR image, first stages
    ``site.workloads_dir`` into the build context (stage_workloads_for_build)
    so the relative ``COPY workload_pools/<sim>/`` resolves on a fresh clone.

    The build argv is exactly
    ``<engine> build -t <image> -f <dockerfile> <context>`` — mirroring
    scripts/build_sim_image.sh (context = repo root). All engine calls route
    through container_engine().

    ``dry_run=True``: return the argv WITHOUT building (and without staging
    workloads). Returns the argv on success/dry-run; raises ``ImageNotFoundError``
    if the tag isn't a manifest entry with a ``build:`` Dockerfile (combined
    sim_agents images build via a recipe script — handled by the CLI, not here).
    """
    from archbench.image_management import manifest as images_mod

    # Repo root = explicit arg, else images.REPO_ROOT (derived from this file's
    # path — the same anchor scripts/build_sim_image.sh uses for its context).
    rr = Path(repo_root) if repo_root is not None else images_mod.REPO_ROOT

    ctx = images_mod.build_context_for(image, repo_root=rr)
    if ctx is None:
        raise ImageNotFoundError(
            f"cannot build {image!r}: no manifest entry with a `build:` "
            "Dockerfile. (Combined sim_agents images build via a recipe "
            "script — use `archbench images build` for those.)"
        )
    dockerfile, context = ctx
    if not dockerfile.is_file():
        raise ImageNotFoundError(
            f"cannot build {image!r}: Dockerfile not found at {dockerfile}"
        )

    found = images_mod.find_by_tag(image, manifest=images_mod.load_manifest())
    category = found[0] if found else None

    argv = [container_engine(), "build", "-t", image,
            "-f", str(dockerfile), str(context)]
    if dry_run:
        return argv

    if category == "simulators":
        _, key = found  # type: ignore[misc]
        stage_workloads_for_build(key, rr)

    log.info("building %s: %s", image, " ".join(argv))
    try:
        result = subprocess.run(argv, timeout=7200)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"build of {image} timed out after 2 hours"
        ) from e
    if result.returncode != 0:
        raise RuntimeError(
            f"build of {image} failed (rc={result.returncode}); see output above"
        )
    return argv


def default_tar_search_dirs() -> list[Path]:
    """Tar search dirs for `ensure_image`, site- and env-overridable.

    Returns the per-machine tar pool first (`site.yaml` `tar_dir`, default
    `<repo>/docker` — identical to today), then any directories listed in
    `$ARCHBENCH_LEGACY_TAR_DIR` (colon-separated, folded in by `load_site`). The
    legacy env var lets users share one image-tar pool across multiple repo
    checkouts (e.g., a 13 GB ChampSim tar on shared storage); `tar_dir`
    relocates the primary pool without touching code (docs §6).

    No silent fallback: if a tar isn't in any of these dirs,
    `ensure_image` raises `ImageNotFoundError` with the search list.
    """
    site = load_site()
    return [site.tar_dir, *site.legacy_tar_dirs]


def ensure_image(
    image: str,
    tar_search_dirs: list[Path],
    expected_digest: Optional[str] = None,
    force_reload: bool = False,
    verify_card: bool = True,
) -> str:
    """Load `image` (digest logic in `_ensure_image_digest`) AND, if a container
    card exists for it, verify the LIVE image matches that card before returning
    — so a stale/wrong/drifted image fails AT LOAD, naming what's wrong, instead
    of surprising you mid-run. `verify_card=False` skips the content check.

    No card present -> the content check is a silent no-op (the digest logic +
    provenance still run), so this is a zero-risk no-op until images are stamped.
    """
    digest = _ensure_image_digest(image, tar_search_dirs, expected_digest, force_reload)
    if verify_card:
        _verify_card_on_load(image)
    return digest


def _verify_card_on_load(image: str) -> None:
    """Content-level card verify. Mismatch -> raise (fail-fast). A failure to
    even RUN the check (engine/infra) -> warn + continue (don't break loads on a
    verify bug)."""
    from archbench.core import container_card as cc
    try:
        card = cc.load_card(cc.card_path_for(image))
        if not card:
            return  # no card yet -> nothing to verify
        violations = cc.verify_against_image(card, image, container_engine())
    except Exception as e:  # noqa: BLE001 — infra failure must not break loads
        log.warning("container card verify could not run for %s: %s (continuing)", image, e)
        return
    if violations:
        raise ImageCardMismatch(
            f"{image} does not match its container card "
            f"({cc.card_path_for(image).name}) — refusing to run a drifted "
            f"container:\n  " + "\n  ".join(violations)
        )
    log.info("container card OK: %s (%s)", image, cc.card_path_for(image).name)


def _ensure_image_digest(
    image: str,
    tar_search_dirs: list[Path],
    expected_digest: Optional[str] = None,
    force_reload: bool = False,
) -> str:
    """Ensure `image` is loaded locally. Return its actual digest.

    Search order for the tarball: each `tar_search_dirs[i]/<slug>.tar`
    where slug derives from the image tag (e.g. `localhost/archbench-
    champsim:v6` → `archbench-champsim-v6.tar`).

    Behavior:
      - Image loaded AND `expected_digest` matches → return digest.
      - Image loaded AND digests differ → `rmi -f`, fall through to tar.
      - Image loaded AND no `expected_digest` → return digest (warn-only).
      - Image not loaded → load from first found tarball.
      - No tarball anywhere → raise `ImageNotFoundError` (no silent
        fallback to `docker pull` or "maybe it'll exist").

    `force_reload=True` (or env `ARCHBENCH_FORCE_IMAGE_RELOAD=1`):
      Always `rmi -f` first. Use after rebuilding the image so compute
      nodes pick up the new bytes instead of their stale cache.
    """
    if os.environ.get("ARCHBENCH_FORCE_IMAGE_RELOAD") == "1":
        force_reload = True

    local = get_image_digest(image)

    if local and force_reload:
        log.info("force_reload=True: removing cached %s", image)
        subprocess.run([container_engine(), "rmi", "-f", image],
                       capture_output=True, timeout=60)
        local = None

    if local and expected_digest:
        if local == expected_digest:
            return local
        log.warning(
            "%s digest mismatch (local=%s, expected=%s); reloading from tar",
            image, local[:24], expected_digest[:24],
        )
        subprocess.run([container_engine(), "rmi", "-f", image],
                       capture_output=True, timeout=60)
        local = None

    if local:
        return local  # no expected_digest to check against

    # Resolve tar path
    bare = image.split("/")[-1]               # archbench-champsim:v6
    name, _, tag = bare.partition(":")        # archbench-champsim, v6
    tag = tag or "latest"
    slug = name[len("archbench-"):] if name.startswith("archbench-") else name
    candidates = [d / slug / f"{name}-{tag}.tar" for d in tar_search_dirs]
    candidates += [d / f"{name}-{tag}.tar" for d in tar_search_dirs]

    for tar in candidates:
        if tar.exists():
            _load_from_tar(image, tar)
            digest = get_image_digest(image)
            if digest is None:
                raise RuntimeError(
                    f"Loaded {image} from {tar} but inspect returns no digest"
                )
            if expected_digest and digest != expected_digest:
                raise ImageDigestMismatch(
                    f"Loaded {image} digest {digest[:24]} but expected "
                    f"{expected_digest[:24]} (tar {tar} may be stale)"
                )
            return digest

    # ---- HOP 4 (K5): gated autobuild / pull fallback ----------------------
    # Image is neither local nor in any tar pool. Before the hard
    # ImageNotFoundError, try to MATERIALIZE it so a fresh clone with no tar is
    # runnable (docs §7 hop 4): if a registry is configured -> pull; else BUILD
    # from the manifest Dockerfile (staging workloads for sims).
    #
    # GATING (CLAUDE.md §1.7 / §1.16): a compute node / batch job MUST NEVER
    # silently rebuild — a from-scratch build won't bit-match committed baseline
    # digests, which would drift the provenance gate mid-campaign. So autobuild
    # is SKIPPED (we fall through to ImageNotFoundError, exactly as before) when
    # SLURM_JOB_ID is set OR ARCHBENCH_NO_AUTOBUILD=1. Autobuild is allowed only
    # interactively. On the origin box images load from local/tar, so this hop
    # never fires there — zero change to existing runs.
    autobuild_blocked = bool(os.environ.get("SLURM_JOB_ID")) or \
        os.environ.get("ARCHBENCH_NO_AUTOBUILD") == "1"
    if not autobuild_blocked:
        digest = _materialize_missing_image(image)
        if digest is not None:
            if expected_digest and digest != expected_digest:
                raise ImageDigestMismatch(
                    f"Built/pulled {image} digest {digest[:24]} but expected "
                    f"{expected_digest[:24]} (re-baseline against this image)"
                )
            return digest

    why = (
        "autobuild SKIPPED (SLURM_JOB_ID / ARCHBENCH_NO_AUTOBUILD set — a batch node "
        "must not rebuild and drift provenance; pre-build on the login node). "
        if autobuild_blocked else ""
    )
    raise ImageNotFoundError(
        f"Image {image!r} not loaded and no tarball found in "
        f"{[str(d) for d in tar_search_dirs]}. " + why +
        "Build the image first (`archbench images build <name>` or see "
        f"the manifest Dockerfile) and/or place its tarball at one of: "
        f"{[str(c) for c in candidates]}."
    )


def _materialize_missing_image(image: str) -> Optional[str]:
    """Pull-or-build a missing image (the unguarded body of hop 4).

    If a registry is configured in site.yaml -> ``<engine> pull
    <registry>/<repo:tag>`` then tag local. Else BUILD from the manifest
    Dockerfile (build_image_from_manifest, which stages workloads for sims).
    Logs LOUDLY that a fresh build's digest may differ from committed baselines.

    Returns the resulting local digest, or ``None`` if neither path could run
    (e.g. the tag is not a manifest entry with a buildable Dockerfile and no
    registry is set) — the caller then raises ImageNotFoundError.
    """
    site = load_site()
    if site.registry:
        # registry configured: prefer a pull (cheap, deterministic) over build.
        bare = image.split("/")[-1]  # repo:tag
        remote = f"{site.registry.rstrip('/')}/{bare}"
        log.warning(
            "image %s missing; pulling %s (registry=%s)",
            image, remote, site.registry,
        )
        rc = subprocess.run(
            [container_engine(), "pull", remote],
            capture_output=True, text=True, timeout=1800,
        )
        if rc.returncode == 0:
            subprocess.run([container_engine(), "tag", remote, image],
                           capture_output=True, timeout=60)
            return get_image_digest(image)
        log.warning("pull of %s failed (rc=%d): %s", remote, rc.returncode,
                    rc.stderr.strip())
        return None

    # No registry: build from the Dockerfile.
    log.warning(
        "image %s missing and no tarball; AUTOBUILDING from its Dockerfile. "
        "!! A from-scratch build's digest may DIFFER from committed "
        "baseline.json digests (apt/base-image drift); re-baseline if the "
        "provenance gate complains (CLAUDE.md §1.7).", image,
    )
    try:
        build_image_from_manifest(image)
    except ImageNotFoundError:
        # Not a buildable manifest entry (e.g. an unknown tag) — let the
        # caller fall through to its ImageNotFoundError with the tar paths.
        return None
    return get_image_digest(image)
