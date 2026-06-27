"""Container-engine shim — resolve the container CLI binary name once.

Historical incident (engine-shim phase, see docs/docker_management.md §7 +
§10): every subprocess in ``archbench/core/container.py`` hardcoded the literal
``"docker"`` despite the module claiming to be a "per-run podman/docker
wrapper." On a box where ``docker`` is not aliased to ``podman`` (a
podman-only node), all of those calls — start/exec/cp/inspect/load/rmi —
fail, and so do the two agent-LAUNCH ``docker exec`` argvs the runners
build directly. The agent never starts.

Meanwhile ``provenance.py::docker_image_digest`` already did the right
thing: it loops ``for binary in ("docker", "podman")``. This module lifts
that pattern into one resolver so every container call routes through a
single source of truth for "which binary do I shell out to?"

Resolution precedence (highest first):

1. ``ARCHBENCH_CONTAINER_CLI`` — if set AND on PATH, use it verbatim. Lets a
   one-off run or sbatch line force an engine without editing code. If it
   is set but NOT on PATH we raise — an explicit override that can't be
   satisfied is an error, never a silent fall-through (no-silent-failure
   discipline, CLAUDE.md §1.9).
2. ``site.yaml`` ``container_cli`` (via ``load_site``, K2) — a per-machine
   default for a box where docker is unavailable. Same not-on-PATH-raises
   rule as the env override. Absent (``None``) -> fall through to probe.
3. Auto-detect — probe PATH preferring ``docker`` FIRST, then ``podman``.
   docker-FIRST is REQUIRED for behavior preservation: the pre-shim code
   called the literal ``"docker"`` and it works on the current box, so
   trying docker first resolves to the same binary -> byte-identical
   behavior. With no ``site.yaml`` and no env override (the default),
   ``load_site().container_cli`` is ``None`` and we land here exactly as
   the pre-K2 code did.
4. Neither found -> ``RuntimeError`` (no silent fallback to a
   ``docker pull`` or an assumed binary).

Pure-ish module: ``shutil``/``os``/``functools`` + ``load_site`` only. No
container calls at import time. The resolved value is cached (the engine on
a box does not change mid-process); the cache can be cleared in tests via
``container_engine.cache_clear()``.
"""

from __future__ import annotations

import functools
import os
import shutil

from archbench.image_management.site import load_site

#: Auto-detect probe order. docker FIRST for behavior preservation — see
#: the module docstring. Do not reorder without re-reading §7 of
#: docs/docker_management.md.
_PROBE_ORDER = ("docker", "podman")

#: The env var a site / one-off run uses to force a specific engine.
_ENV_OVERRIDE = "ARCHBENCH_CONTAINER_CLI"


@functools.lru_cache(maxsize=1)
def container_engine() -> str:
    """Return the container CLI binary name (``"docker"`` or ``"podman"``).

    See module docstring for the full precedence. Raises ``RuntimeError``
    if no usable engine is found (no silent fallback).

    The result is memoized. Because ``ARCHBENCH_CONTAINER_CLI`` is read here, a
    test that mutates the env must call ``container_engine.cache_clear()``
    to force a re-probe.
    """
    forced = os.environ.get(_ENV_OVERRIDE)
    if forced:
        if shutil.which(forced):
            return forced
        raise RuntimeError(
            f"{_ENV_OVERRIDE}={forced!r} is set but {forced!r} is not on "
            "PATH. Install it or unset the override."
        )

    # Hop 2: a per-machine default from site.yaml (K2). None (the zero-config
    # default) falls through to the docker-first probe -> pre-K2 behavior.
    # load_site() also folds in ARCHBENCH_CONTAINER_CLI, but the env override is
    # already handled above with its own not-on-PATH-raises semantics; here we
    # only consult the file-sourced value, which carries the same rule.
    site_cli = load_site().container_cli
    if site_cli:
        if shutil.which(site_cli):
            return site_cli
        raise RuntimeError(
            f"site.yaml container_cli={site_cli!r} but {site_cli!r} is not on "
            "PATH. Install it or change site.yaml."
        )

    for candidate in _PROBE_ORDER:
        if shutil.which(candidate):
            return candidate

    raise RuntimeError(
        "No container engine found: need 'docker' or 'podman' on PATH "
        f"(set {_ENV_OVERRIDE} to force one)."
    )
