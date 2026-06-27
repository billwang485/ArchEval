"""Shared pytest config — markers for tests that need real docker.

Integration tests (those that touch docker / actually start a sim
container) are marked `@pytest.mark.requires_docker` and auto-skip
unless docker is available AND the user opts in by passing
`-m requires_docker` or `--run-docker`.

This keeps `pytest` fast and CI-friendly by default; ops can run the
heavier checks before pushing.
"""

import shutil
import subprocess

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-docker", action="store_true", default=False,
        help="Run integration tests that require docker",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "requires_docker: integration test that needs docker + a built image",
    )


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-docker"):
        return  # user opted in; run them
    skip_marker = pytest.mark.skip(
        reason="requires docker — pass --run-docker to enable",
    )
    for item in items:
        if "requires_docker" in item.keywords:
            item.add_marker(skip_marker)


@pytest.fixture(scope="session")
def docker_available() -> bool:
    return _docker_available()
