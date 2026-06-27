"""container_engine() — the engine-shim resolver (docs/docker_management.md §7).

These tests never call a real container engine: ``shutil.which`` is
monkeypatched so the behavior is deterministic on any box (docker-only,
podman-only, both, or neither). The lru_cache is cleared before each
assertion so each case re-probes a fresh environment.

Covers:
- ``ARCHBENCH_CONTAINER_CLI`` override is honored when the named binary is on PATH.
- ``ARCHBENCH_CONTAINER_CLI`` set but not on PATH raises (no silent fall-through).
- ``site.yaml`` ``container_cli`` is honored (K2) below the env override and
  above auto-detect, with the same not-on-PATH-raises rule.
- a ``None`` site value (zero-config default) falls through to auto-detect.
- auto-detect returns "docker" when docker is present (docker-FIRST =
  behavior-preserving).
- auto-detect returns "podman" when only podman is present.
- raises when neither is present (no silent fallback).
"""

import pytest

from archbench.image_management import engine
from archbench.image_management.engine import container_engine
from archbench.image_management.site import SiteConfig, load_site


def _which_factory(present):
    """Return a fake shutil.which that only finds binaries in ``present``."""
    present = set(present)

    def _which(cmd, *args, **kwargs):
        return f"/usr/bin/{cmd}" if cmd in present else None

    return _which


def _site_with_cli(cli):
    """A SiteConfig whose only relevant field is container_cli=`cli`. Other
    fields are placeholders the engine resolver never reads."""
    from pathlib import Path

    return SiteConfig(
        tar_dir=Path("/x/docker"),
        container_cli=cli,
        registry="",
        scratch_dir=Path("/tmp"),
        workloads_dir=Path("/x/workload_pools"),
        lock_dir=Path("/tmp"),
    )


@pytest.fixture(autouse=True)
def _clear_engine_cache(monkeypatch):
    """Force a re-probe for every test (the resolver is lru_cache'd), and
    default the site to zero-config (container_cli=None) so an unrelated
    real site.yaml on the box cannot perturb the auto-detect cases. Tests
    that exercise the site hop override this with their own monkeypatch."""
    container_engine.cache_clear()
    load_site.cache_clear()
    monkeypatch.setattr(engine, "load_site", lambda: _site_with_cli(None))
    yield
    container_engine.cache_clear()
    load_site.cache_clear()


def test_env_override_honored_when_on_path(monkeypatch):
    monkeypatch.setenv("ARCHBENCH_CONTAINER_CLI", "podman")
    # Both are on PATH; the override must win over the docker-first probe.
    monkeypatch.setattr(engine.shutil, "which", _which_factory({"docker", "podman"}))
    assert container_engine() == "podman"


def test_env_override_can_force_docker(monkeypatch):
    monkeypatch.setenv("ARCHBENCH_CONTAINER_CLI", "docker")
    monkeypatch.setattr(engine.shutil, "which", _which_factory({"docker", "podman"}))
    assert container_engine() == "docker"


def test_env_override_not_on_path_raises(monkeypatch):
    monkeypatch.setenv("ARCHBENCH_CONTAINER_CLI", "podman")
    # Override names podman, but only docker is actually installed.
    monkeypatch.setattr(engine.shutil, "which", _which_factory({"docker"}))
    with pytest.raises(RuntimeError, match="not on PATH"):
        container_engine()


def test_autodetect_prefers_docker_when_present(monkeypatch):
    monkeypatch.delenv("ARCHBENCH_CONTAINER_CLI", raising=False)
    # Both present -> docker FIRST (behavior preservation on the current box).
    monkeypatch.setattr(engine.shutil, "which", _which_factory({"docker", "podman"}))
    assert container_engine() == "docker"


def test_autodetect_returns_docker_when_only_docker(monkeypatch):
    monkeypatch.delenv("ARCHBENCH_CONTAINER_CLI", raising=False)
    monkeypatch.setattr(engine.shutil, "which", _which_factory({"docker"}))
    assert container_engine() == "docker"


def test_autodetect_returns_podman_when_only_podman(monkeypatch):
    monkeypatch.delenv("ARCHBENCH_CONTAINER_CLI", raising=False)
    monkeypatch.setattr(engine.shutil, "which", _which_factory({"podman"}))
    assert container_engine() == "podman"


def test_raises_when_neither_present(monkeypatch):
    monkeypatch.delenv("ARCHBENCH_CONTAINER_CLI", raising=False)
    monkeypatch.setattr(engine.shutil, "which", _which_factory(set()))
    with pytest.raises(RuntimeError, match="No container engine found"):
        container_engine()


def test_empty_env_override_falls_through_to_autodetect(monkeypatch):
    # An empty ARCHBENCH_CONTAINER_CLI must not be treated as a forced engine;
    # it falls through to the docker-first probe.
    monkeypatch.setenv("ARCHBENCH_CONTAINER_CLI", "")
    monkeypatch.setattr(engine.shutil, "which", _which_factory({"podman"}))
    assert container_engine() == "podman"


# --- site.yaml container_cli hop (K2) --------------------------------------


def test_site_container_cli_honored_when_on_path(monkeypatch):
    # No env override; site.yaml forces podman even though docker is present.
    monkeypatch.delenv("ARCHBENCH_CONTAINER_CLI", raising=False)
    monkeypatch.setattr(engine, "load_site", lambda: _site_with_cli("podman"))
    monkeypatch.setattr(engine.shutil, "which", _which_factory({"docker", "podman"}))
    assert container_engine() == "podman"


def test_env_override_beats_site(monkeypatch):
    # Env override wins over the site value (precedence hop 1 > hop 2).
    monkeypatch.setenv("ARCHBENCH_CONTAINER_CLI", "docker")
    monkeypatch.setattr(engine, "load_site", lambda: _site_with_cli("podman"))
    monkeypatch.setattr(engine.shutil, "which", _which_factory({"docker", "podman"}))
    assert container_engine() == "docker"


def test_site_container_cli_not_on_path_raises(monkeypatch):
    monkeypatch.delenv("ARCHBENCH_CONTAINER_CLI", raising=False)
    monkeypatch.setattr(engine, "load_site", lambda: _site_with_cli("podman"))
    # site says podman, but only docker is installed -> raise (no fall-through).
    monkeypatch.setattr(engine.shutil, "which", _which_factory({"docker"}))
    with pytest.raises(RuntimeError, match="not on PATH"):
        container_engine()


def test_none_site_falls_through_to_autodetect(monkeypatch):
    # The zero-config default (container_cli=None) must reach the docker-first
    # probe -> behavior-preserving.
    monkeypatch.delenv("ARCHBENCH_CONTAINER_CLI", raising=False)
    monkeypatch.setattr(engine, "load_site", lambda: _site_with_cli(None))
    monkeypatch.setattr(engine.shutil, "which", _which_factory({"docker", "podman"}))
    assert container_engine() == "docker"
