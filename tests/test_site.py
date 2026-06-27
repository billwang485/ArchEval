"""load_site() — the per-machine "where" resolver (docs/docker_management.md §6, K2).

These tests never touch a real cluster path. They assert:
- zero-config (no site.yaml) -> portable defaults, == today's behavior;
- env var overrides win over site.yaml (precedence hop 1 > hop 2);
- a site.yaml value overrides the built-in default (hop 2 > hop 3);
- read-on-demand: load_site() does NOT mutate os.environ (CLAUDE.md §1.14/§1.15).

``load_site`` is ``lru_cache``'d on its ``path`` arg, so each test passes an
explicit path (a temp file, or a guaranteed-absent path) AND clears the
cache, keeping cases independent and independent of any real repo-root
``site.yaml``.
"""

import os
from pathlib import Path

import pytest

from archbench.image_management import site as site_mod
from archbench.image_management.site import REPO_ROOT, SiteConfig, load_site

# Every ARCHBENCH_* override site.py consults — cleared before each test so the
# host environment can't perturb a "defaults" assertion.
_SITE_ENV_VARS = (
    "ARCHBENCH_TAR_DIR",
    "ARCHBENCH_CONTAINER_CLI",
    "ARCHBENCH_REGISTRY",
    "ARCHBENCH_SCRATCH_DIR",
    "ARCHBENCH_WORKLOADS_DIR",
    "ARCHBENCH_LOCK_DIR",
    "ARCHBENCH_LEGACY_TAR_DIR",
)


@pytest.fixture(autouse=True)
def _clean_site_env(monkeypatch):
    """Clear the cache and every ARCHBENCH_* var so each case starts from a known,
    override-free environment. Individual tests re-set the vars they study."""
    load_site.cache_clear()
    for var in _SITE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield
    load_site.cache_clear()


def _absent_path(tmp_path: Path) -> Path:
    """A path guaranteed not to exist -> exercises the zero-config branch."""
    return tmp_path / "does_not_exist_site.yaml"


def test_defaults_with_no_site_file(tmp_path, monkeypatch):
    # No TMPDIR -> scratch default is /tmp (the "identical to today" case).
    monkeypatch.delenv("TMPDIR", raising=False)
    cfg = load_site(_absent_path(tmp_path))
    assert isinstance(cfg, SiteConfig)
    # tar_dir -> <repo>/docker (byte-identical to the pre-K2 default).
    assert cfg.tar_dir == REPO_ROOT / "docker"
    assert str(cfg.tar_dir).endswith("/docker")
    # container_cli -> None (engine auto-detects), NOT the string "auto".
    assert cfg.container_cli is None
    # registry -> "" (build, store only; not used until K5).
    assert cfg.registry == ""
    # scratch_dir -> /tmp when TMPDIR is unset.
    assert cfg.scratch_dir == Path("/tmp")
    # workloads_dir -> <repo>/workload_pools.
    assert cfg.workloads_dir == REPO_ROOT / "workload_pools"
    # lock_dir defaults to scratch_dir.
    assert cfg.lock_dir == cfg.scratch_dir == Path("/tmp")
    assert cfg.legacy_tar_dirs == ()


def test_scratch_default_uses_tmpdir_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", "/scratch/tmpdir")
    cfg = load_site(_absent_path(tmp_path))
    assert cfg.scratch_dir == Path("/scratch/tmpdir")
    # lock_dir tracks scratch_dir by default.
    assert cfg.lock_dir == Path("/scratch/tmpdir")


def test_env_overrides_win_over_site_yaml(tmp_path, monkeypatch):
    # site.yaml sets one value, the matching ARCHBENCH_* env var sets another;
    # the env var must win for every field.
    site_yaml = tmp_path / "site.yaml"
    site_yaml.write_text(
        "tar_dir: /from/site/tars\n"
        "container_cli: docker\n"
        "registry: from-site\n"
        "scratch_dir: /from/site/scratch\n"
        "workloads_dir: /from/site/wl\n"
        "lock_dir: /from/site/locks\n"
    )
    monkeypatch.setenv("ARCHBENCH_TAR_DIR", "/from/env/tars")
    monkeypatch.setenv("ARCHBENCH_CONTAINER_CLI", "podman")
    monkeypatch.setenv("ARCHBENCH_REGISTRY", "from-env")
    monkeypatch.setenv("ARCHBENCH_SCRATCH_DIR", "/from/env/scratch")
    monkeypatch.setenv("ARCHBENCH_WORKLOADS_DIR", "/from/env/wl")
    monkeypatch.setenv("ARCHBENCH_LOCK_DIR", "/from/env/locks")

    cfg = load_site(site_yaml)
    assert cfg.tar_dir == Path("/from/env/tars")
    assert cfg.container_cli == "podman"
    assert cfg.registry == "from-env"
    assert cfg.scratch_dir == Path("/from/env/scratch")
    assert cfg.workloads_dir == Path("/from/env/wl")
    assert cfg.lock_dir == Path("/from/env/locks")


def test_site_yaml_overrides_defaults(tmp_path, monkeypatch):
    # No env overrides; a written site.yaml must beat the built-in defaults.
    monkeypatch.delenv("TMPDIR", raising=False)
    site_yaml = tmp_path / "site.yaml"
    site_yaml.write_text(
        "schema_version: 1\n"
        "tar_dir: /pool/archbench-images\n"
        "container_cli: podman\n"
        "registry: ghcr.io/example\n"
        "scratch_dir: /big/scratch\n"
        "workloads_dir: /data/traces\n"
        "lock_dir: /big/locks\n"
    )
    cfg = load_site(site_yaml)
    assert cfg.tar_dir == Path("/pool/archbench-images")
    assert cfg.container_cli == "podman"
    assert cfg.registry == "ghcr.io/example"
    assert cfg.scratch_dir == Path("/big/scratch")
    assert cfg.workloads_dir == Path("/data/traces")
    assert cfg.lock_dir == Path("/big/locks")


def test_partial_site_yaml_fills_rest_with_defaults(tmp_path, monkeypatch):
    # Only one key set; everything else falls through to the portable default.
    monkeypatch.delenv("TMPDIR", raising=False)
    site_yaml = tmp_path / "site.yaml"
    site_yaml.write_text("workloads_dir: /data/traces\n")
    cfg = load_site(site_yaml)
    assert cfg.workloads_dir == Path("/data/traces")
    assert cfg.tar_dir == REPO_ROOT / "docker"
    assert cfg.container_cli is None
    assert cfg.registry == ""
    assert cfg.scratch_dir == Path("/tmp")


def test_lock_dir_defaults_to_resolved_scratch(tmp_path, monkeypatch):
    # lock_dir absent -> tracks the *resolved* scratch_dir, including when
    # scratch_dir itself came from site.yaml.
    monkeypatch.delenv("TMPDIR", raising=False)
    site_yaml = tmp_path / "site.yaml"
    site_yaml.write_text("scratch_dir: /custom/scratch\n")
    cfg = load_site(site_yaml)
    assert cfg.scratch_dir == Path("/custom/scratch")
    assert cfg.lock_dir == Path("/custom/scratch")


def test_legacy_tar_dir_appended(tmp_path, monkeypatch):
    # ARCHBENCH_LEGACY_TAR_DIR is colon-split and captured as extra tar dirs.
    monkeypatch.setenv("ARCHBENCH_LEGACY_TAR_DIR", "/pool/a:/pool/b")
    cfg = load_site(_absent_path(tmp_path))
    assert cfg.legacy_tar_dirs == (Path("/pool/a"), Path("/pool/b"))


def test_read_on_demand_does_not_mutate_environ(tmp_path, monkeypatch):
    # The §1.14/§1.15 discipline: resolving site config must not export any
    # key into os.environ (paths/secrets must not leak into child containers).
    monkeypatch.delenv("TMPDIR", raising=False)
    site_yaml = tmp_path / "site.yaml"
    site_yaml.write_text(
        "tar_dir: /pool/archbench-images\n"
        "container_cli: podman\n"
        "registry: ghcr.io/example\n"
        "workloads_dir: /data/traces\n"
    )
    before = dict(os.environ)
    load_site(site_yaml)
    assert dict(os.environ) == before
    # None of the ARCHBENCH_* names appeared as a side effect.
    for var in _SITE_ENV_VARS:
        assert var not in os.environ


def test_non_mapping_site_yaml_raises(tmp_path):
    # A typo'd top-level list would silently disable every override; reject it.
    bad = tmp_path / "site.yaml"
    bad.write_text("- not\n- a\n- mapping\n")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_site(bad)


def test_empty_site_yaml_is_zero_config(tmp_path, monkeypatch):
    # A present-but-empty file parses to {} -> portable defaults.
    monkeypatch.delenv("TMPDIR", raising=False)
    empty = tmp_path / "site.yaml"
    empty.write_text("")
    cfg = load_site(empty)
    assert cfg.tar_dir == REPO_ROOT / "docker"
    assert cfg.container_cli is None
    assert cfg.registry == ""
    assert cfg.scratch_dir == Path("/tmp")


def test_default_path_points_at_repo_root():
    # Guards the "site.yaml lives at repo root" contract the gitignore + the
    # migrate guide both assume.
    assert site_mod.DEFAULT_SITE_PATH == REPO_ROOT / "site.yaml"
