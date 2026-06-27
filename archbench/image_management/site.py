"""site.py — the per-machine "where" layer (docs/docker_management.md §6).

K2. This is the single place that knows ``site.yaml`` exists. Everything
machine-specific (tar pools, scratch dirs, workload roots, which container
CLI, a registry namespace) is resolved here, so the six *code* dirs stay
free of any ``/n/…`` absolute path — the sacred invariant the CI gate in
``tests/test_no_cluster_paths.py`` enforces (§10).

Two design constraints, both load-bearing:

1. **Portable, zero-config.** With NO ``site.yaml`` present and no ``ARCHBENCH_*``
   env overrides, every field resolves to a PORTABLE default that works on
   any cluster, cloud VM, or laptop — and reproduces TODAY's behavior on
   the origin box. A fresh ``git clone`` runs with nothing configured.

2. **Read-on-demand, never auto-exported (CLAUDE.md §1.14 / §1.15).** Like
   ``archbench.core.env_file.read_env``, this module reads the file on demand and
   resolves env overrides at read time. It does NOT inject anything into
   ``os.environ`` — so a tar-pool path or a registry namespace never leaks
   into a child container that does not need it.

Resolution precedence per field (highest first):

    1. explicit ``ARCHBENCH_*`` env var   (a one-off run / sbatch line override)
    2. ``site.yaml`` value          (repo-root, gitignored, ``yaml.safe_load``)
    3. portable built-in default    (works anywhere; == today on this box)

The nested ``site.yaml`` body is real YAML and is parsed with
``yaml.safe_load`` — ``read_env`` is a strictly line-based ``KEY=value``
``.env`` parser and *cannot* see nested keys (docs §6). ``read_env`` stays
for secret-style ``.env`` overrides only; this module owns the YAML site
config.

Pure module: ``yaml`` / ``os`` / ``pathlib`` / ``functools`` / ``dataclasses``
only. No container calls, no side effects at import time. The result is
cached; clear it in tests via ``load_site.cache_clear()``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

# Repo root = two levels up (archbench/image_management/site.py -> repo/). Mirrors
# manifest.REPO_ROOT and container.default_tar_search_dirs()'s own derivation.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The gitignored, repo-root site file. Absent on a fresh clone -> defaults.
DEFAULT_SITE_PATH = REPO_ROOT / "site.yaml"

# --- env override names (precedence hop 1) ---------------------------------
_ENV_TAR_DIR = "ARCHBENCH_TAR_DIR"
_ENV_CONTAINER_CLI = "ARCHBENCH_CONTAINER_CLI"
_ENV_REGISTRY = "ARCHBENCH_REGISTRY"
_ENV_SCRATCH_DIR = "ARCHBENCH_SCRATCH_DIR"
_ENV_WORKLOADS_DIR = "ARCHBENCH_WORKLOADS_DIR"
_ENV_LOCK_DIR = "ARCHBENCH_LOCK_DIR"

#: Legacy colon-separated extra tar dirs, appended to the search list.
#: Honored verbatim so existing pool setups keep working (docs §6, §11).
_ENV_LEGACY_TAR_DIR = "ARCHBENCH_LEGACY_TAR_DIR"


@dataclass(frozen=True)
class SiteConfig:
    """Resolved per-machine settings. All fields already have the full
    precedence applied (env > site.yaml > portable default).

    Paths are stored as ``Path`` where they name a directory the harness
    creates/reads; ``container_cli`` is ``None`` to mean "let the engine
    auto-detect" (NOT the string "auto"), and ``registry`` is the empty
    string to mean "build, don't push" — the K0/today default. ``registry``
    is stored only; it is not consumed until K5.
    """

    tar_dir: Path
    container_cli: Optional[str]
    registry: str
    scratch_dir: Path
    workloads_dir: Path
    lock_dir: Path
    #: Extra tar dirs from ARCHBENCH_LEGACY_TAR_DIR, appended to the search list.
    legacy_tar_dirs: tuple[Path, ...] = ()


def _read_site_file(path: Path) -> dict[str, Any]:
    """Parse ``site.yaml`` if it exists, else return ``{}`` (zero-config).

    Pure: one ``yaml.safe_load``; no env mutation. A present-but-empty file
    parses to ``{}``. A non-mapping body is a hard error — a typo'd top-level
    list would otherwise silently disable every site override.
    """
    if not path.is_file():
        return {}
    with open(path, "r") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(
            f"site.yaml at {path} must be a mapping (got "
            f"{type(data).__name__}); see site.example.yaml"
        )
    return data


def _resolve_str(
    env_name: str, site: dict[str, Any], key: str, default: str
) -> str:
    """env > site.yaml > default, as a plain string. Empty env is ignored
    (an empty ARCHBENCH_* must NOT shadow site/default — mirrors engine.py)."""
    env_val = os.environ.get(env_name)
    if env_val:
        return env_val
    site_val = site.get(key)
    if site_val is not None:
        return str(site_val)
    return default


def _legacy_tar_dirs() -> tuple[Path, ...]:
    """Extra tar dirs from ``ARCHBENCH_LEGACY_TAR_DIR`` (colon-separated). Honored
    verbatim and appended to the search list — see default_tar_search_dirs()."""
    raw = os.environ.get(_ENV_LEGACY_TAR_DIR, "")
    return tuple(Path(p.strip()) for p in raw.split(":") if p.strip())


@lru_cache(maxsize=8)
def load_site(path: Optional[Path] = None) -> SiteConfig:
    """Resolve every per-machine setting into a :class:`SiteConfig`.

    With no ``site.yaml`` and no ``ARCHBENCH_*`` env overrides this returns the
    PORTABLE defaults, which on the origin box reproduce today's behavior
    byte-for-byte:

      - ``tar_dir``       = ``<repo>/docker``                  (env ARCHBENCH_TAR_DIR)
      - ``container_cli`` = ``None`` -> engine auto-detects    (env ARCHBENCH_CONTAINER_CLI)
      - ``registry``      = ``""`` -> build, store only        (env ARCHBENCH_REGISTRY)
      - ``scratch_dir``   = ``$TMPDIR`` or ``/tmp``            (env ARCHBENCH_SCRATCH_DIR)
      - ``workloads_dir`` = ``<repo>/workload_pools``          (env ARCHBENCH_WORKLOADS_DIR)
      - ``lock_dir``      = ``scratch_dir``                    (env ARCHBENCH_LOCK_DIR)

    Read-on-demand + cached per resolved path; does NOT mutate ``os.environ``
    (CLAUDE.md §1.14/§1.15). Clear the cache in tests after mutating env or
    writing a temp ``site.yaml``: ``load_site.cache_clear()``.
    """
    site_path = Path(path) if path is not None else DEFAULT_SITE_PATH
    site = _read_site_file(site_path)

    # tar_dir: env > site.yaml > <repo>/docker.
    tar_dir = Path(
        _resolve_str(_ENV_TAR_DIR, site, "tar_dir", str(REPO_ROOT / "docker"))
    )

    # container_cli: env > site.yaml > None (None -> engine auto-detects).
    # An empty env value is ignored (falls through to site/default), matching
    # engine.py's empty-override handling.
    cli_env = os.environ.get(_ENV_CONTAINER_CLI)
    if cli_env:
        container_cli: Optional[str] = cli_env
    else:
        site_cli = site.get("container_cli")
        container_cli = str(site_cli) if site_cli else None

    # registry: env > site.yaml > "" ("" -> build; stored only, used in K5).
    # Honor an explicitly-empty site value (registry: "") as a real choice;
    # only fall through to the default when the key is absent.
    reg_env = os.environ.get(_ENV_REGISTRY)
    if reg_env is not None:
        registry = reg_env
    elif "registry" in site:
        registry = "" if site["registry"] is None else str(site["registry"])
    else:
        registry = ""

    # scratch_dir: env > site.yaml > $TMPDIR or /tmp.
    scratch_default = os.environ.get("TMPDIR") or "/tmp"
    scratch_dir = Path(
        _resolve_str(_ENV_SCRATCH_DIR, site, "scratch_dir", scratch_default)
    )

    # workloads_dir: env > site.yaml > <repo>/workload_pools.
    workloads_dir = Path(
        _resolve_str(
            _ENV_WORKLOADS_DIR, site, "workloads_dir",
            str(REPO_ROOT / "workload_pools"),
        )
    )

    # lock_dir: env > site.yaml > scratch_dir (the already-resolved value).
    lock_dir = Path(
        _resolve_str(_ENV_LOCK_DIR, site, "lock_dir", str(scratch_dir))
    )

    return SiteConfig(
        tar_dir=tar_dir,
        container_cli=container_cli,
        registry=registry,
        scratch_dir=scratch_dir,
        workloads_dir=workloads_dir,
        lock_dir=lock_dir,
        legacy_tar_dirs=_legacy_tar_dirs(),
    )
