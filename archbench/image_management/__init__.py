"""archbench.image_management — the docker/image-management subsystem (one discoverable home).

Read ``archbench/image_management/README.md`` first: it is the map of this package. Full
design lives in ``docs/docker_management.md``.

This package gathers the four formerly-scattered ``archbench/core`` modules plus the
``archbench images`` CLI verbs into one place:

  - :mod:`archbench.image_management.manifest`  — loads ``../../images.yaml`` (the single source
                                  of truth for image identity).
  - :mod:`archbench.image_management.site`      — per-machine config from ``../../site.yaml``.
  - :mod:`archbench.image_management.engine`    — the docker/podman binary shim.
  - :mod:`archbench.image_management.plan`      — :func:`resolve_images`: agent_image_mode +
                                  evaluation_sim_image -> :class:`ImagePlan`.
  - :mod:`archbench.image_management.cli`       — the ``archbench images`` verbs.

The public names below are re-exported so callers can do ``from archbench.image_management
import fully_qualified, load_site, container_engine, resolve_images`` instead of
reaching into the submodules.
"""

from __future__ import annotations

from .engine import container_engine
from .manifest import (
    CATEGORIES,
    DEFAULT_MANIFEST_PATH,
    DEFAULT_REGISTRY,
    REPO_ROOT,
    build_context_for,
    find_by_tag,
    fully_qualified,
    iter_images,
    keys,
    load_manifest,
    recipe_for,
    registry,
    repo_name,
)
from .plan import (
    VALID_AGENT_IMAGE_MODES,
    ImagePlan,
    _l2agent_image,
    resolve_images,
)
from .site import DEFAULT_SITE_PATH, SiteConfig, load_site

__all__ = [
    # manifest (images.yaml — image identity)
    "load_manifest",
    "fully_qualified",
    "iter_images",
    "keys",
    "CATEGORIES",
    "find_by_tag",
    "build_context_for",
    "recipe_for",
    "registry",
    "repo_name",
    "DEFAULT_MANIFEST_PATH",
    "DEFAULT_REGISTRY",
    "REPO_ROOT",
    # site (site.yaml — per-machine "where")
    "load_site",
    "SiteConfig",
    "DEFAULT_SITE_PATH",
    # engine (docker/podman shim)
    "container_engine",
    # plan (the pure image resolver)
    "resolve_images",
    "ImagePlan",
    "VALID_AGENT_IMAGE_MODES",
    "_l2agent_image",
]
