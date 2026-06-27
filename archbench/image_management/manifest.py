"""manifest.py — read the `images.yaml` manifest, the single source of truth
for image identity (docs/docker_management.md §2, §5, §7-K0, §8).

This is K0: a PURE, READ-ONLY module. It makes NO docker calls and has NO
side effects — `yaml.safe_load` + `pathlib` only. Its one job today is to
reproduce, byte-for-byte, the image strings the code already uses:

    fully_qualified("simulators", "champsim") == "localhost/archbench-champsim:v6"
    fully_qualified("agents", "claude_code")  == "localhost/archbench-agent:v6"

A golden test (tests/test_images_manifest.py) asserts that for every
simulator plugin and every runtime. Nothing else consumes this module in
K0 — plugins/info.yaml/session.py are untouched; wiring lands in K3.

Naming rules:
  - simulators.<key>  -> {registry}/archbench-<key>:<tag>     (name defaults to archbench-<key>)
  - agents.<key>      -> {registry}/<entry.name>:<tag>  (explicit name; this is
                         how claude_code resolves to "archbench-agent" with NO suffix)
  - registry default  -> "localhost" (the manifest's top-level `registry:`)
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

# Repo root = two levels up from this file (archbench/image_management/manifest.py -> repo/).
# Mirrors archbench.cli.REPO_ROOT and container.default_tar_search_dirs().
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST_PATH = REPO_ROOT / "images.yaml"

# Categories the manifest groups images under. SIMULATORS + AGENTS are
# populated in K0; SIM_AGENTS (combined _l2agent images) and CHALLENGES
# (challenge_centric) are interface-only placeholders (empty dicts).
CATEGORIES: tuple[str, ...] = ("simulators", "agents", "sim_agents", "challenges")

# Default registry prefix when the manifest omits `registry:`. "localhost"
# preserves today's offline, no-push behavior (a per-site override is K2).
DEFAULT_REGISTRY = "localhost"


@lru_cache(maxsize=8)
def load_manifest(path: Optional[Path] = None) -> dict[str, Any]:
    """Parse and return the `images.yaml` manifest as a dict.

    Result is cached per resolved path. Pure: reads one file with
    `yaml.safe_load`, no docker, no env mutation.

    Raises FileNotFoundError if the manifest is missing (in K0 the manifest
    is committed and required; callers that want a soft-fallback can catch).
    """
    p = Path(path) if path is not None else DEFAULT_MANIFEST_PATH
    if not p.is_file():
        raise FileNotFoundError(f"images.yaml manifest not found at {p}")
    with open(p, "r") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"images.yaml at {p} did not parse to a mapping")
    return data


def registry(manifest: Optional[dict[str, Any]] = None) -> str:
    """The registry namespace prefix (e.g. 'localhost'). K0: manifest value
    or the built-in default; a per-site env override comes in K2."""
    m = manifest if manifest is not None else load_manifest()
    return m.get("registry") or DEFAULT_REGISTRY


def _entry(manifest: dict[str, Any], category: str, key: str) -> dict[str, Any]:
    if category not in CATEGORIES:
        raise KeyError(
            f"unknown image category {category!r}; "
            f"expected one of {CATEGORIES}"
        )
    group = manifest.get(category) or {}
    if key not in group:
        raise KeyError(
            f"no image entry for {category}.{key} in images.yaml "
            f"(have: {sorted(group)})"
        )
    entry = group[key]
    if not isinstance(entry, dict):
        raise ValueError(
            f"images.yaml {category}.{key} must be a mapping, got {type(entry).__name__}"
        )
    return entry


def repo_name(category: str, key: str, entry: dict[str, Any]) -> str:
    """The bare image repo name (no registry, no tag).

    - simulators: default to ``archbench-<key>`` (override with an explicit `name:`).
    - everything else (agents, sim_agents, challenges): the entry's `name:`
      is required — this is how claude_code -> "archbench-agent" (no -claude_code).
    """
    name = entry.get("name")
    if name:
        return str(name)
    if category == "simulators":
        return f"archbench-{key}"
    raise ValueError(
        f"images.yaml {category}.{key} has no `name:` (required outside "
        f"`simulators`, where it defaults to archbench-<key>)"
    )


def fully_qualified(
    category: str, key: str, manifest: Optional[dict[str, Any]] = None,
) -> str:
    """Return '{registry}/{repo}:{tag}' for one manifest entry.

    Reproduces today's exact docker_image strings for every simulator and
    runtime (the non-breaking anchor — see the golden test). For K0 the
    registry prefix is always 'localhost' (from the manifest).
    """
    m = manifest if manifest is not None else load_manifest()
    entry = _entry(m, category, key)
    tag = entry.get("tag")
    if not tag:
        raise ValueError(f"images.yaml {category}.{key} has no `tag:`")
    name = repo_name(category, key, entry)
    return f"{registry(m)}/{name}:{tag}"


def keys(category: str, manifest: Optional[dict[str, Any]] = None) -> list[str]:
    """Sorted keys declared under one category (e.g. all simulator names)."""
    m = manifest if manifest is not None else load_manifest()
    if category not in CATEGORIES:
        raise KeyError(
            f"unknown image category {category!r}; expected one of {CATEGORIES}"
        )
    return sorted((m.get(category) or {}).keys())


def iter_images(
    manifest: Optional[dict[str, Any]] = None,
) -> list[tuple[str, str, str]]:
    """Enumerate every declared image as (category, key, fully_qualified).

    Walks CATEGORIES in declared order so callers (e.g. `archbench images status`)
    get a stable grouping. Empty categories (challenges in K5) simply
    contribute nothing.
    """
    m = manifest if manifest is not None else load_manifest()
    out: list[tuple[str, str, str]] = []
    for category in CATEGORIES:
        for key in keys(category, m):
            out.append((category, key, fully_qualified(category, key, m)))
    return out


# ---------------------------------------------------------------------------
# Reverse lookup (K5): image tag -> manifest entry / build recipe.
#
# `fully_qualified` is the FORWARD direction (key -> tag). The autobuild
# fallback (container.py ensure_image hop 4) and `archbench images build` need the
# REVERSE: given a concrete tag like "localhost/archbench-champsim:v6", find the
# manifest entry it came from so we can locate the Dockerfile + build context.
# ---------------------------------------------------------------------------


def find_by_tag(
    image_tag: str, manifest: Optional[dict[str, Any]] = None,
) -> Optional[tuple[str, str]]:
    """Reverse of ``fully_qualified``: return ``(category, key)`` for the
    manifest entry whose fully-qualified tag equals ``image_tag``, or ``None``.

    Matching is exact on the fully-qualified string first; as a convenience an
    unprefixed local tag (no ``/``) is also matched against the bare
    ``repo:tag`` of each entry, so ``archbench-champsim:v6`` finds the champsim entry
    even though the manifest stores ``localhost/archbench-champsim:v6``.
    """
    m = manifest if manifest is not None else load_manifest()
    bare_target = image_tag.split("/")[-1] if "/" not in image_tag else None
    for category, key, fq in iter_images(m):
        if fq == image_tag:
            return (category, key)
        if bare_target is not None and fq.split("/")[-1] == bare_target:
            return (category, key)
    return None


def build_context_for(
    image_tag: str, manifest: Optional[dict[str, Any]] = None,
    repo_root: Optional[Path] = None,
) -> Optional[tuple[Path, Path]]:
    """Map a fully-qualified image tag to its ``(dockerfile, context)`` paths.

    Reads the manifest entry's ``build:`` value (a directory, e.g.
    ``simulators/champsim``) and returns the absolute Dockerfile path
    (``<build>/Dockerfile``) and the build CONTEXT (always the repo root — the
    Dockerfiles do ``COPY simulators/<sim>/...`` / ``COPY workload_pools/...``
    relative to the repo root, mirroring scripts/build_sim_image.sh).

    Returns ``None`` when the tag is unknown OR the entry has no ``build:`` key
    (e.g. a ``sim_agents`` combined image, which builds via a ``recipe:`` script
    rather than a plain Dockerfile — use :func:`recipe_for` for those). The
    autobuild fallback uses this to decide "can I build this from a Dockerfile?"
    """
    rr = Path(repo_root) if repo_root is not None else REPO_ROOT
    found = find_by_tag(image_tag, manifest)
    if found is None:
        return None
    category, key = found
    entry = _entry(manifest if manifest is not None else load_manifest(),
                   category, key)
    build = entry.get("build")
    if not build:
        return None
    build_dir = rr / str(build)
    return (build_dir / "Dockerfile", rr)


def recipe_for(
    image_tag: str, manifest: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """For a combined (``sim_agents``) image tag, return its build recipe info.

    Returns a dict ``{"category", "key", "recipe", "base"}`` where ``recipe`` is
    the script path (e.g. ``scripts/build_l2agent_image.sh``) and ``base`` is the
    simulator key the combined image layers on (e.g. ``champsim``). Returns
    ``None`` for non-combined tags or unknown tags. ``archbench images build`` uses
    this to delegate a combined-image build to the existing script (which it
    must NOT generalize — docs §5: the l2agent build does ``COPY --from=``,
    ``pip --network host``, and a rootless-UID chown; keep it a script).
    """
    found = find_by_tag(image_tag, manifest)
    if found is None:
        return None
    category, key = found
    if category != "sim_agents":
        return None
    entry = _entry(manifest if manifest is not None else load_manifest(),
                   category, key)
    return {
        "category": category,
        "key": key,
        "recipe": entry.get("recipe"),
        "base": entry.get("base"),
    }
