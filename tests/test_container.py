"""ContainerManager + ensure_image — tests that don't need real docker.

Covers:
- Cleanup-hook registration is idempotent.
- ContainerConfig.with_run_id gives unique names.
- ensure_image's no-fallback policy fires the right exception.
"""

import threading
from pathlib import Path

import pytest

from archbench.core.container import (
    ContainerConfig,
    ContainerManager,
    ImageDigestMismatch,
    ImageNotFoundError,
    _CLEANUP_INSTALLED,
    _LIVE_CONTAINERS,
    _install_cleanup_handlers,
    ensure_image,
)


def test_with_run_id_names_are_unique():
    a = ContainerConfig.with_run_id("img", "arch_agent")
    b = ContainerConfig.with_run_id("img", "arch_agent")
    assert a.container_name != b.container_name
    assert a.container_name.startswith("arch_agent_")
    assert b.container_name.startswith("arch_agent_")


def test_cleanup_handlers_install_idempotent():
    # _install is idempotent — call twice, no exception
    _install_cleanup_handlers()
    _install_cleanup_handlers()
    # And the module-level flag is set
    from archbench.core import container as ct
    assert ct._CLEANUP_INSTALLED is True


def test_ensure_image_no_image_no_tar_raises_typed_error(tmp_path):
    """The structural rule: never silently `docker pull` or assume."""
    # tmp_path has no tarball; assume docker doesn't have the image
    # (extremely unlikely tag collision, but let's also make the name
    # deterministically unique)
    fake_image = "localhost/archbench-nonexistent-test:v0"
    with pytest.raises(ImageNotFoundError, match="not loaded and no tarball"):
        ensure_image(fake_image, [tmp_path])


def test_ensure_image_searches_multiple_dirs(tmp_path):
    """When given multiple tar_search_dirs, the error message should
    list all candidates so the user knows where to put the tar."""
    d1 = tmp_path / "dir1"; d1.mkdir()
    d2 = tmp_path / "dir2"; d2.mkdir()
    fake_image = "localhost/archbench-nonexistent-test2:v0"
    with pytest.raises(ImageNotFoundError) as e:
        ensure_image(fake_image, [d1, d2])
    # The message should include both candidate paths
    msg = str(e.value)
    assert "dir1" in msg
    assert "dir2" in msg


def test_container_manager_can_be_constructed_without_starting():
    """ContainerManager init must not perform any docker calls — just
    config validation. Lets tests construct it freely."""
    cfg = ContainerConfig.with_run_id("img", "test")
    mgr = ContainerManager(cfg)
    assert mgr.name == cfg.container_name
    assert mgr.running is False


def test_start_refuses_missing_mount_source(tmp_path):
    """Mount source must exist on host — no silent broken mounts.

    Past confusion: docker run would accept a missing host path and
    create it implicitly as an empty dir, masking trace-not-mounted bugs.
    """
    cfg = ContainerConfig(
        image="any:tag",
        container_name="test_missing_mount",
        mounts=[(tmp_path / "does_not_exist", "/in/container", "ro")],
    )
    mgr = ContainerManager(cfg)
    with pytest.raises(FileNotFoundError, match="Mount source missing"):
        mgr.start()


def test_config_has_volumes_field_default_empty():
    """volumes is a dict[Path, str] default-empty — bake-only runtimes
    leave it untouched, dev-capable runtimes populate it."""
    cfg = ContainerConfig(image="img", container_name="t")
    assert hasattr(cfg, "volumes")
    assert cfg.volumes == {}


def test_start_refuses_missing_volume_source(tmp_path):
    """Volumes get the same fail-fast treatment as mounts: missing host
    path → FileNotFoundError, never silently created as an empty dir."""
    cfg = ContainerConfig(
        image="any:tag",
        container_name="test_missing_volume",
        volumes={tmp_path / "does_not_exist": "/in/container"},
    )
    mgr = ContainerManager(cfg)
    with pytest.raises(FileNotFoundError, match="Volume source missing"):
        mgr.start()


def test_start_volumes_appear_in_docker_args(tmp_path, monkeypatch):
    """The volumes dict must turn into `-v host:container` args. Capture
    the cmd via a subprocess.run patch and assert the bind appears."""
    import subprocess as _sp
    host_dir = tmp_path / "src"
    host_dir.mkdir()

    captured: dict[str, list[str]] = {}

    real_run = _sp.run

    def fake_run(cmd, *args, **kwargs):
        # The first call inside start() is the pre-clean `docker rm -f`;
        # the second is the real `docker run -d ...`. We capture the run
        # cmd then short-circuit with a fake successful result so we
        # don't actually hit docker.
        if isinstance(cmd, list) and len(cmd) >= 2 and cmd[:2] == ["docker", "run"]:
            captured["cmd"] = list(cmd)

            class _R:
                returncode = 0
                stdout = "ctrid\n"
                stderr = ""
            return _R()
        # pre-clean rm -f: also return a fake success without doing anything
        if isinstance(cmd, list) and len(cmd) >= 3 and cmd[:3] == ["docker", "rm", "-f"]:
            class _R:
                returncode = 0
                stdout = ""
                stderr = ""
            return _R()
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr("archbench.core.container.subprocess.run", fake_run)

    cfg = ContainerConfig(
        image="any:tag",
        container_name="test_vol_args",
        volumes={host_dir: "/baked/path"},
    )
    mgr = ContainerManager(cfg)
    mgr.start()
    cmd = captured["cmd"]
    # Sanity: -v <host>:<container> appears as adjacent argv entries
    assert "-v" in cmd, f"-v not in cmd: {cmd}"
    v_idx = cmd.index("-v")
    assert cmd[v_idx + 1] == f"{host_dir}:/baked/path", (
        f"expected `-v {host_dir}:/baked/path`, got `-v {cmd[v_idx + 1]}`"
    )


def test_live_containers_registry_is_thread_safe():
    """We use a lock on _LIVE_CONTAINERS; sanity-check multi-threaded
    inserts don't lose entries."""
    from archbench.core.container import _LIVE_CONTAINERS, _LIVE_LOCK
    _LIVE_CONTAINERS.clear()

    def adder(name):
        with _LIVE_LOCK:
            _LIVE_CONTAINERS.add(name)

    threads = [
        threading.Thread(target=adder, args=(f"t{i}",))
        for i in range(50)
    ]
    for t in threads: t.start()
    for t in threads: t.join()
    assert _LIVE_CONTAINERS == {f"t{i}" for i in range(50)}
    _LIVE_CONTAINERS.clear()
