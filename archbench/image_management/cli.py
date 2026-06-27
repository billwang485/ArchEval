"""cli.py — the ``archbench images`` lifecycle verbs (status/build/load/save/pull/rm/
gc/digest) over the ``images.yaml`` manifest (docs/docker_management.md §7-K0,
§7-K5, §8, §10).

These verbs are the human-facing front of the image-management subsystem. They
were previously inlined in ``archbench/cli.py``; they live here so the whole
subsystem (manifest + site + engine + plan + verbs) is one discoverable
package. ``archbench/cli.py`` keeps only a thin hook:
:func:`register_images_subcommand` builds the ``images`` subparser, and the
verb functions are re-exported into the ``archbench.cli`` namespace for backward
compatibility (tests + any caller that did ``cli.cmd_images_*``).

Every engine call routes through :func:`archbench.image_management.engine.container_engine` —
no bare ``"docker"``/``"podman"`` (CLAUDE.md §1; docs §10). ``ensure_image``
itself stays in ``archbench/core/container.py`` (the 4-hop resolver); these verbs are
the manual lifecycle around it.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Optional

from archbench.core.container import default_tar_search_dirs
from archbench.image_management import manifest as images_mod

log = logging.getLogger("archbench.image_management.cli")

# Repo root + tar pool: the same anchors archbench/cli.py uses (manifest.REPO_ROOT is
# the repo root, derived from this package's __file__). DEFAULT_TAR_SEARCH is
# resolved once here, mirroring archbench/cli.py + archbench/runtimes/session.py so the same
# logic (the ARCHBENCH_LEGACY_TAR_DIR override) is shared across CLI/session/tests.
REPO_ROOT = images_mod.REPO_ROOT
DEFAULT_TAR_SEARCH = default_tar_search_dirs()


# ---------------------------------------------------------------------------
# images — read-only inventory over the images.yaml manifest (K0)
# ---------------------------------------------------------------------------


# Human-facing category labels (manifest key -> display heading).
_IMAGE_CATEGORY_LABELS = {
    "simulators": "SIMULATORS",
    "agents": "AGENTS",
    "sim_agents": "SIM-AGENTS",
    "challenges": "CHALLENGES",
}


def _image_tar_candidates(image: str, tar_dirs: list[Path]) -> list[Path]:
    """Tar-pool paths for `image`, mirroring ensure_image()'s derivation
    EXACTLY (container.py): slug subdir form first, then flat form.

    e.g. localhost/archbench-champsim:v6 ->
       <dir>/champsim/archbench-champsim-v6.tar   (slug subdir)
       <dir>/archbench-champsim-v6.tar            (flat)
    """
    bare = image.split("/")[-1]              # archbench-champsim:v6
    name, _, tag = bare.partition(":")       # archbench-champsim, v6
    tag = tag or "latest"
    slug = name[len("archbench-"):] if name.startswith("archbench-") else name
    cands = [d / slug / f"{name}-{tag}.tar" for d in tar_dirs]
    cands += [d / f"{name}-{tag}.tar" for d in tar_dirs]
    return cands


def _image_pool_path(image: str, tar_dirs: list[Path]) -> Optional[Path]:
    """First existing tar for `image` in the pool, or None."""
    for cand in _image_tar_candidates(image, tar_dirs):
        if cand.exists():
            return cand
    return None


def _local_short_digest(image: str) -> Optional[str]:
    """Best-effort short local digest via get_image_digest; None on any
    failure so `status` never crashes when the daemon is down."""
    try:
        from archbench.core.container import get_image_digest
        full = get_image_digest(image)
    except Exception:
        return None
    if not full:
        return None
    # get_image_digest returns e.g. "sha256:abcd…" — show 12 hex chars.
    return full.split(":", 1)[-1][:12]


def cmd_images_status(args) -> int:
    """Read-only inventory: for every manifest image, show LOCAL digest,
    POOL (tar present?), and STATE (OK / NOT-LOADED / ABSENT). Also flags
    UNMANAGED local archbench-*/archeval-* images. Builds/loads/removes nothing.
    """
    try:
        manifest = images_mod.load_manifest()
    except Exception as e:
        log.error("could not load images.yaml: %s", e)
        return 1

    only = getattr(args, "category", None)
    if only and only not in images_mod.CATEGORIES:
        log.error(
            "unknown category %r; expected one of %s",
            only, ", ".join(images_mod.CATEGORIES),
        )
        return 1

    # Read DEFAULT_TAR_SEARCH off the archbench.cli module so an existing
    # `monkeypatch.setattr(cli, "DEFAULT_TAR_SEARCH", ...)` still takes effect
    # (the verbs historically lived there). Falls back to this module's value.
    tar_dirs = _default_tar_search()
    reg = images_mod.registry(manifest)

    print()
    print("=" * 72)
    print("archbench images status  (read-only)")
    print(f"  manifest: {images_mod.DEFAULT_MANIFEST_PATH}")
    print(f"  registry: {reg}    tar pool: {', '.join(str(d) for d in tar_dirs)}")
    print("=" * 72)

    # "managed" = EVERY manifest image, regardless of any display filter, so
    # the UNMANAGED check below is always computed against the full manifest.
    managed_tags: set[str] = {fq for _, _, fq in images_mod.iter_images(manifest)}
    grand_local = grand_pool = grand_absent = 0

    categories = [only] if only else list(images_mod.CATEGORIES)
    for category in categories:
        ckeys = images_mod.keys(category, manifest)
        label = _IMAGE_CATEGORY_LABELS.get(category, category.upper())
        n_local = n_pool = n_absent = 0
        rows: list[tuple[str, str, str, str]] = []  # tag, digest, pool, state
        for key in ckeys:
            image = images_mod.fully_qualified(category, key, manifest)
            digest = _local_short_digest(image)
            pool = _image_pool_path(image, tar_dirs)
            if digest:
                state = "OK"
                n_local += 1
            elif pool is not None:
                state = "NOT-LOADED"
                n_pool += 1
            else:
                state = "ABSENT"
                n_absent += 1
            rows.append((
                image,
                digest or "-",
                "tar+" if pool is not None else "tar-",
                state,
            ))

        grand_local += n_local
        grand_pool += n_pool
        grand_absent += n_absent

        print()
        print(
            f"{label}  ({len(ckeys)} declared · {n_local} local · "
            f"{n_pool} pool-only · {n_absent} absent)"
        )
        if not rows:
            print("  (none declared)")
        for tag, digest, pool, state in rows:
            print(f"  {tag:<34} {digest:<14} {pool:<6} {state}")

    # ---- UNMANAGED: local archbench-* / archeval-* not claimed by the manifest ----
    print()
    unmanaged = _list_unmanaged_images(managed_tags)
    if unmanaged is None:
        print("UNMANAGED  (skipped — could not query the container engine)")
    elif not unmanaged:
        print("UNMANAGED  (none — every local archbench-*/archeval-* is in the manifest)")
    else:
        print(f"UNMANAGED  ({len(unmanaged)} local archbench-*/archeval-* not in manifest)")
        for tag, digest in unmanaged:
            print(f"  {tag:<34} {digest:<14} {'':<6} UNMANAGED")

    print()
    print("-" * 72)
    total = grand_local + grand_pool + grand_absent
    print(
        f"TOTAL  {total} managed · {grand_local} local · "
        f"{grand_pool} pool-only · {grand_absent} absent"
    )
    print()
    return 0


def _list_unmanaged_images(
    managed_tags: set[str],
) -> Optional[list[tuple[str, str]]]:
    """Local 'localhost/archbench-*' / 'localhost/archeval-*' images NOT in the
    manifest. Returns None if the engine can't be queried (best-effort;
    status stays read-only and never crashes)."""
    import subprocess
    try:
        result = subprocess.run(
            ["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}\t{{.ID}}"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    out: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        tag = parts[0]
        digest = parts[1] if len(parts) > 1 else "-"
        # Normalize an unprefixed local repo to the localhost/ form so the
        # membership check matches manifest tags (which are localhost/-prefixed).
        norm = tag if "/" in tag else f"localhost/{tag}"
        repo = norm.split("/", 1)[-1]
        if not (repo.startswith("archbench-") or repo.startswith("archeval-")):
            continue
        if norm in managed_tags or tag in managed_tags:
            continue
        out.append((norm, digest.split(":", 1)[-1][:12]))
    return sorted(out)


# ---------------------------------------------------------------------------
# images — ergonomic lifecycle verbs (K5: build/load/save/pull/rm/gc/digest)
#
# Each verb takes a TARGET: a name (`champsim` or `sim/champsim`), a category
# (`simulators` | `agents` | `sim-agents`), or `all`. ALL engine calls route
# through container_engine() — no bare "docker"/"podman" (CLAUDE.md / docs §10).
# ---------------------------------------------------------------------------


# Accept the hyphenated CLI category spelling alongside the manifest's
# underscore key (`sim-agents` <-> `sim_agents`), plus the short singular
# aliases the pseudo-path vocabulary uses (`sim/`, `agent/`).
_IMAGE_TARGET_CATEGORY_ALIASES = {
    "simulators": "simulators",
    "simulator": "simulators",
    "sim": "simulators",
    "sims": "simulators",
    "agents": "agents",
    "agent": "agents",
    "runtimes": "agents",
    "runtime": "agents",
    "sim-agents": "sim_agents",
    "sim_agents": "sim_agents",
    "sim-agent": "sim_agents",
    "combined": "sim_agents",
    "challenges": "challenges",
    "challenge": "challenges",
}


def _default_tar_search() -> list[Path]:
    """The tar pool search list. Read off the ``archbench.cli`` module first so an
    existing ``monkeypatch.setattr(cli, "DEFAULT_TAR_SEARCH", ...)`` is honored
    (these verbs historically lived in ``archbench/cli.py``); else this module's own
    resolved value."""
    try:
        from archbench import cli as _cli
        return _cli.DEFAULT_TAR_SEARCH
    except Exception:
        return DEFAULT_TAR_SEARCH


def _resolve_image_targets(target: str, manifest) -> list[tuple[str, str, str]]:
    """Expand a verb TARGET into a list of (category, key, fully_qualified).

    TARGET may be:
      - ``all``                      -> every manifest image (iter_images).
      - a category                   -> every image in it (`simulators`,
        `agents`, `sim-agents`/`sim_agents`, `challenges`).
      - ``<cat>/<key>``              -> one image (e.g. `sim/champsim`).
      - ``<key>``                    -> one image, searched across categories
        (unambiguous: a key like `champsim` lives in both `simulators` AND
        `sim_agents`, so a bare name prefers `simulators` then `agents` then
        `sim_agents`; use the `<cat>/<key>` form to disambiguate).

    Raises ``KeyError``/``ValueError`` (caught by the caller, surfaced as a
    clean error) on an unknown target.
    """
    t = target.strip()
    if t == "all":
        return list(images_mod.iter_images(manifest))

    # Bare category name.
    cat_alias = _IMAGE_TARGET_CATEGORY_ALIASES.get(t)
    if cat_alias is not None and "/" not in t:
        return [
            (cat_alias, k, images_mod.fully_qualified(cat_alias, k, manifest))
            for k in images_mod.keys(cat_alias, manifest)
        ]

    # <cat>/<key> form.
    if "/" in t:
        cat_raw, key = t.split("/", 1)
        cat = _IMAGE_TARGET_CATEGORY_ALIASES.get(cat_raw.strip(), cat_raw.strip())
        return [(cat, key, images_mod.fully_qualified(cat, key, manifest))]

    # Bare key: search categories in preference order.
    for cat in ("simulators", "agents", "sim_agents", "challenges"):
        if t in images_mod.keys(cat, manifest):
            return [(cat, t, images_mod.fully_qualified(cat, t, manifest))]
    raise KeyError(
        f"unknown image target {target!r}: not 'all', a category "
        f"({', '.join(images_mod.CATEGORIES)}), or a declared image key. "
        f"Use <category>/<key> to disambiguate."
    )


def _load_manifest_or_die():
    """Load the manifest, logging + returning None on failure (the caller then
    returns rc=1). Shared by every verb so the error is uniform."""
    try:
        return images_mod.load_manifest()
    except Exception as e:  # noqa: BLE001 — surface any manifest problem
        log.error("could not load images.yaml: %s", e)
        return None


def cmd_images_build(args) -> int:
    """`archbench images build <target> [--dry-run]` — build from the Dockerfile.

    For a SIMULATOR build, stages site.workloads_dir into the build context
    first (so the relative `COPY workload_pools/<sim>/` resolves on a fresh
    clone). For a SIM-AGENT (combined) image, delegates to the recipe script
    (scripts/build_l2agent_image.sh) — NOT generalized. `--dry-run` prints the
    plan (image, dockerfile, context, staged workloads) and builds nothing.
    """
    from archbench.core.container import build_image_from_manifest

    manifest = _load_manifest_or_die()
    if manifest is None:
        return 1
    try:
        targets = _resolve_image_targets(args.target, manifest)
    except (KeyError, ValueError) as e:
        log.error("%s", e)
        return 1

    print()
    print("=" * 72)
    print(f"archbench images build  {args.target}"
          f"{'   (DRY-RUN — building nothing)' if args.dry_run else ''}")
    print("=" * 72)

    rc = 0
    for category, key, image in targets:
        if category == "sim_agents":
            recipe = images_mod.recipe_for(image, manifest) or {}
            script = recipe.get("recipe")
            base = recipe.get("base") or key
            if not script:
                log.error("sim_agent %s has no `recipe:` in images.yaml", image)
                rc = 1
                continue
            script_path = REPO_ROOT / script
            argv = ["bash", str(script_path), base]
            print()
            print(f"[{category}/{key}] {image}")
            print(f"  recipe:  {script_path}")
            print(f"  invoke:  {' '.join(argv)}   (combined: sim={base} + agent loop)")
            if args.dry_run:
                continue
            if not script_path.is_file():
                log.error("recipe script not found: %s", script_path)
                rc = 1
                continue
            import subprocess
            res = subprocess.run(argv)
            if res.returncode != 0:
                log.error("build of %s failed (rc=%d)", image, res.returncode)
                rc = 1
            continue

        # simulators / agents: build from the manifest Dockerfile.
        ctx = images_mod.build_context_for(image, manifest)
        print()
        print(f"[{category}/{key}] {image}")
        if ctx is None:
            print("  (no `build:` Dockerfile in the manifest — cannot build)")
            log.error("%s has no buildable `build:` Dockerfile", image)
            rc = 1
            continue
        dockerfile, context = ctx
        staged = "-"
        if category == "simulators":
            # Show what WOULD be staged (don't create it on a dry run).
            pool = REPO_ROOT / "workload_pools" / key
            if pool.exists():
                staged = f"{pool} (present)"
            else:
                from archbench.image_management.site import load_site
                src = load_site().workloads_dir / key
                if src.exists():
                    staged = f"{src} -> {pool}  (will symlink)"
                else:
                    staged = (f"{src} (ABSENT — build will fail if the "
                              "Dockerfile COPYs workload_pools/; set workloads_dir)")
        print(f"  dockerfile: {dockerfile}")
        print(f"  context:    {context}")
        print(f"  workloads:  {staged}")
        from archbench.image_management.engine import container_engine
        print(f"  argv:       {container_engine()} build -t {image} "
              f"-f {dockerfile} {context}")
        if args.dry_run:
            continue
        try:
            build_image_from_manifest(image, repo_root=REPO_ROOT)
        except Exception as e:  # noqa: BLE001 — report + continue to next target
            log.error("build of %s failed: %s", image, e)
            rc = 1
    print()
    return rc


def cmd_images_load(args) -> int:
    """`archbench images load <target>` — load from the tar pool (ensure_image's
    tar path / _load_from_tar)."""
    from archbench.core.container import ensure_image, ImageNotFoundError

    manifest = _load_manifest_or_die()
    if manifest is None:
        return 1
    try:
        targets = _resolve_image_targets(args.target, manifest)
    except (KeyError, ValueError) as e:
        log.error("%s", e)
        return 1

    tar_dirs = _default_tar_search()
    rc = 0
    for category, key, image in targets:
        try:
            digest = ensure_image(image, tar_dirs)
            print(f"  [OK]      {image:<40} {digest.split(':', 1)[-1][:12]}")
        except ImageNotFoundError as e:
            print(f"  [MISSING] {image:<40} no tar in pool")
            log.error("load %s: %s", image, e)
            rc = 1
        except Exception as e:  # noqa: BLE001
            print(f"  [ERROR]   {image:<40} {e}")
            rc = 1
    return rc


def cmd_images_save(args) -> int:
    """`archbench images save <target>` — save a local image to the tar pool.

    Writes to ``site.tar_dir/<slug>/<repo>-<tag>.tar`` (the slug-subdir form
    ensure_image reads first). Skips images that aren't local (nothing to save).
    """
    import subprocess
    from archbench.image_management.engine import container_engine
    from archbench.core.container import get_image_digest
    from archbench.image_management.site import load_site

    manifest = _load_manifest_or_die()
    if manifest is None:
        return 1
    try:
        targets = _resolve_image_targets(args.target, manifest)
    except (KeyError, ValueError) as e:
        log.error("%s", e)
        return 1

    tar_dir = load_site().tar_dir
    rc = 0
    for category, key, image in targets:
        if get_image_digest(image) is None:
            print(f"  [SKIP]    {image:<40} not local (nothing to save)")
            continue
        out = _save_tar_path(image, tar_dir)
        out.parent.mkdir(parents=True, exist_ok=True)
        argv = [container_engine(), "save", "-o", str(out), image]
        log.info("saving %s -> %s", image, out)
        res = subprocess.run(argv, capture_output=True, text=True, timeout=1800)
        if res.returncode != 0:
            print(f"  [ERROR]   {image:<40} {res.stderr.strip()}")
            rc = 1
            continue
        size = out.stat().st_size / 1e9 if out.exists() else 0.0
        print(f"  [SAVED]   {image:<40} {out}  ({size:.1f} GB)")
    return rc


def _save_tar_path(image: str, tar_dir: Path) -> Path:
    """The pool tar path `archbench images save` writes, == the FIRST candidate
    ensure_image() reads (slug subdir form): <tar_dir>/<slug>/<repo>-<tag>.tar.
    Mirrors container.py's derivation EXACTLY."""
    bare = image.split("/")[-1]            # archbench-champsim:v6
    name, _, tag = bare.partition(":")     # archbench-champsim, v6
    tag = tag or "latest"
    slug = name[len("archbench-"):] if name.startswith("archbench-") else name
    return tar_dir / slug / f"{name}-{tag}.tar"


def cmd_images_pull(args) -> int:
    """`archbench images pull <target>` — pull from the configured registry, then
    tag local. Exits non-zero if no registry is configured in site.yaml."""
    import subprocess
    from archbench.image_management.engine import container_engine
    from archbench.image_management.site import load_site

    site = load_site()
    if not site.registry:
        log.error("no registry configured in site.yaml (set `registry:` or "
                  "ARCHBENCH_REGISTRY); nothing to pull from.")
        return 1

    manifest = _load_manifest_or_die()
    if manifest is None:
        return 1
    try:
        targets = _resolve_image_targets(args.target, manifest)
    except (KeyError, ValueError) as e:
        log.error("%s", e)
        return 1

    reg = site.registry.rstrip("/")
    rc = 0
    for category, key, image in targets:
        remote = f"{reg}/{image.split('/')[-1]}"
        log.info("pulling %s", remote)
        res = subprocess.run([container_engine(), "pull", remote],
                             capture_output=True, text=True, timeout=1800)
        if res.returncode != 0:
            print(f"  [ERROR]   {remote:<48} {res.stderr.strip()}")
            rc = 1
            continue
        subprocess.run([container_engine(), "tag", remote, image],
                       capture_output=True, timeout=60)
        print(f"  [PULLED]  {remote}  ->  {image}")
    return rc


def cmd_images_rm(args) -> int:
    """`archbench images rm <target>` — `<engine> rmi -f` the resolved image(s)."""
    import subprocess
    from archbench.image_management.engine import container_engine

    manifest = _load_manifest_or_die()
    if manifest is None:
        return 1
    try:
        targets = _resolve_image_targets(args.target, manifest)
    except (KeyError, ValueError) as e:
        log.error("%s", e)
        return 1

    rc = 0
    for category, key, image in targets:
        res = subprocess.run([container_engine(), "rmi", "-f", image],
                             capture_output=True, text=True, timeout=120)
        if res.returncode != 0:
            # rmi of an absent image is not an error worth failing the verb.
            print(f"  [skip]    {image:<40} {res.stderr.strip() or 'not present'}")
        else:
            print(f"  [removed] {image}")
    return rc


def cmd_images_gc(args) -> int:
    """`archbench images gc [--dry-run]` — reap orphan archbench_* containers + dangling
    images. Conservative: ONLY archbench_*-named containers and dangling layers
    (`image prune -f`). Ties to lessons §21 (disk-full). Prints what it reaps.
    """
    import subprocess
    from archbench.image_management.engine import container_engine

    engine = container_engine()
    dry = getattr(args, "dry_run", False)

    print()
    print("=" * 72)
    print(f"archbench images gc{'   (DRY-RUN — reaping nothing)' if dry else ''}")
    print("  scope: archbench_*-named containers + dangling (untagged) image layers")
    print("=" * 72)

    # 1. Orphan archbench_* containers.
    ps = subprocess.run(
        [engine, "ps", "-aq", "--filter", "name=archbench_"],
        capture_output=True, text=True, timeout=60,
    )
    ctr_ids = [c for c in ps.stdout.split() if c.strip()] if ps.returncode == 0 else []
    print()
    if ps.returncode != 0:
        print("CONTAINERS  (could not query the engine — skipped)")
    elif not ctr_ids:
        print("CONTAINERS  (none — no archbench_* containers to reap)")
    else:
        print(f"CONTAINERS  ({len(ctr_ids)} archbench_* to remove)")
        for cid in ctr_ids:
            print(f"  {cid}")
        if not dry:
            subprocess.run([engine, "rm", "-f", *ctr_ids],
                           capture_output=True, text=True, timeout=300)
            print(f"  -> removed {len(ctr_ids)} container(s)")

    # 2. Dangling image layers.
    print()
    if dry:
        dangling = subprocess.run(
            [engine, "images", "-qf", "dangling=true"],
            capture_output=True, text=True, timeout=60,
        )
        ids = [d for d in dangling.stdout.split() if d.strip()] \
            if dangling.returncode == 0 else []
        if not ids:
            print("DANGLING    (none — no untagged layers to prune)")
        else:
            print(f"DANGLING    ({len(ids)} untagged layer(s) would be pruned)")
            for did in ids:
                print(f"  {did}")
    else:
        pr = subprocess.run([engine, "image", "prune", "-f"],
                            capture_output=True, text=True, timeout=300)
        out = (pr.stdout + pr.stderr).strip()
        print("DANGLING    (image prune -f)")
        for line in out.splitlines():
            print(f"  {line}")
    print()
    return 0


def cmd_images_digest(args) -> int:
    """`archbench images digest <name>` — print the local digest (get_image_digest)
    for the resolved image. Exits non-zero if the image isn't local."""
    from archbench.core.container import get_image_digest

    manifest = _load_manifest_or_die()
    if manifest is None:
        return 1
    try:
        targets = _resolve_image_targets(args.name, manifest)
    except (KeyError, ValueError) as e:
        log.error("%s", e)
        return 1

    rc = 0
    for category, key, image in targets:
        digest = get_image_digest(image)
        if digest is None:
            print(f"{image}\t(not local)")
            rc = 1
        else:
            print(f"{image}\t{digest}")
    return rc


def cmd_images(args) -> int:
    """Dispatch `archbench images <sub>`."""
    sub = getattr(args, "images_cmd", None)
    dispatch = {
        "status": cmd_images_status,
        "build": cmd_images_build,
        "load": cmd_images_load,
        "save": cmd_images_save,
        "pull": cmd_images_pull,
        "rm": cmd_images_rm,
        "gc": cmd_images_gc,
        "digest": cmd_images_digest,
    }
    fn = dispatch.get(sub)
    if fn is not None:
        return fn(args)
    # No sub-subcommand given: print the images help and exit 0.
    parser = getattr(args, "_images_parser", None)
    if parser is not None:
        parser.print_help()
    return 0


def register_images_subcommand(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Build the ``archbench images`` subparser (+ its sub-subcommands) on ``sub``.

    ``archbench/cli.py::main`` calls this where it builds the top-level subparsers, so
    ``archbench images ...`` is wired identically while the verb implementations live
    in this package. Returns the ``images`` parser (the caller may keep it).
    """
    # images — inventory + lifecycle over the images.yaml manifest (K0 + K5).
    # Sub-subcommands: status (read-only) + build/load/save/pull/rm/gc/digest.
    p_img = sub.add_parser(
        "images",
        help="Inspect + manage images.yaml-managed images "
             "(status/build/load/save/pull/rm/gc/digest)",
    )
    img_sub = p_img.add_subparsers(dest="images_cmd")

    p_img_status = img_sub.add_parser(
        "status",
        help="Show LOCAL/POOL/STATE for every manifest image (read-only)",
    )
    p_img_status.add_argument(
        "category", nargs="?", default=None,
        help="Optional category filter: simulators | agents | sim_agents | challenges",
    )
    p_img_status.set_defaults(func=cmd_images_status)

    # A shared positional TARGET (name | category | all) for the lifecycle verbs.
    _target_help = (
        "Target: an image name (e.g. champsim or sim/champsim), a category "
        "(simulators | agents | sim-agents), or 'all'."
    )

    p_img_build = img_sub.add_parser(
        "build", help="Build image(s) from the manifest Dockerfile/recipe")
    p_img_build.add_argument("target", help=_target_help)
    p_img_build.add_argument(
        "--dry-run", action="store_true",
        help="Print the build plan (image, dockerfile, context, staged "
             "workloads) and build nothing.")
    p_img_build.set_defaults(func=cmd_images_build)

    p_img_load = img_sub.add_parser(
        "load", help="Load image(s) from the tar pool (docker load)")
    p_img_load.add_argument("target", help=_target_help)
    p_img_load.set_defaults(func=cmd_images_load)

    p_img_save = img_sub.add_parser(
        "save", help="Save local image(s) to the tar pool (docker save)")
    p_img_save.add_argument("target", help=_target_help)
    p_img_save.set_defaults(func=cmd_images_save)

    p_img_pull = img_sub.add_parser(
        "pull", help="Pull image(s) from the configured registry, then tag local")
    p_img_pull.add_argument("target", help=_target_help)
    p_img_pull.set_defaults(func=cmd_images_pull)

    p_img_rm = img_sub.add_parser(
        "rm", help="Remove local image(s) (docker rmi -f)")
    p_img_rm.add_argument("target", help=_target_help)
    p_img_rm.set_defaults(func=cmd_images_rm)

    p_img_gc = img_sub.add_parser(
        "gc", help="Reap orphan archbench_* containers + dangling image layers")
    p_img_gc.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be reaped and remove nothing.")
    p_img_gc.set_defaults(func=cmd_images_gc)

    p_img_digest = img_sub.add_parser(
        "digest", help="Print the local digest of an image")
    p_img_digest.add_argument(
        "name", help="Image name (e.g. champsim, sim/champsim, or 'all').")
    p_img_digest.set_defaults(func=cmd_images_digest)

    # `archbench images` with no sub-subcommand -> print images help via cmd_images.
    p_img.set_defaults(func=cmd_images, _images_parser=p_img)
    return p_img
