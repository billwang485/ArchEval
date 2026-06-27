"""Tests for archbench.core.env_file.read_env.

Verifies the per-call .env reader does what we want: returns a value
when the key is present, returns None on every other path, never
injects into os.environ.
"""
from __future__ import annotations

import os
from pathlib import Path

from archbench.core.env_file import read_env


def test_read_env_key_present(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=bar\nBAZ=qux\n")
    assert read_env("FOO", env_file) == "bar"
    assert read_env("BAZ", env_file) == "qux"


def test_read_env_key_absent(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=bar\n")
    assert read_env("NOT_THERE", env_file) is None


def test_read_env_missing_file(tmp_path: Path):
    env_file = tmp_path / "no_such_file.env"
    assert read_env("ANY_KEY", env_file) is None


def test_read_env_quoted_value(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'DOUBLE="hello world"\n'
        "SINGLE='secret-key'\n"
        "BARE=plainvalue\n"
    )
    assert read_env("DOUBLE", env_file) == "hello world"
    assert read_env("SINGLE", env_file) == "secret-key"
    assert read_env("BARE", env_file) == "plainvalue"


def test_read_env_skips_comments_and_blanks(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# this is a comment\n"
        "\n"
        "FOO=bar\n"
        "# FOO=should_be_ignored\n"
    )
    assert read_env("FOO", env_file) == "bar"
    # A commented key must NOT be returned.
    assert read_env("# FOO", env_file) is None


def test_read_env_empty_file(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    assert read_env("ANYTHING", env_file) is None


def test_read_env_does_not_pollute_os_environ(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("ARCHBENCH_TEST_NO_LEAK_KEY=please_do_not_leak\n")
    # Sanity: must not already be in env
    assert "ARCHBENCH_TEST_NO_LEAK_KEY" not in os.environ
    val = read_env("ARCHBENCH_TEST_NO_LEAK_KEY", env_file)
    assert val == "please_do_not_leak"
    # Critical invariant: reading must NOT inject into os.environ
    assert "ARCHBENCH_TEST_NO_LEAK_KEY" not in os.environ
