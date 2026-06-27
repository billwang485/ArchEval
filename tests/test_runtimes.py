"""Per-runtime unit tests: identity, from_runtime_spec, auth contracts.

Container-bound tests (verify_in_container, start_session) live in
test_runtime_integration.py and require --run-docker.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from archbench.core.challenge import Challenge, EvalConfig, RuntimeSpec
from archbench.runtimes import (
    _REGISTRY,
    get_runtime,
    runtime_from_challenge,
)
from runtimes.archharness import ArchharnessRuntime
from runtimes.claude_code import ClaudeCodeRuntime
from runtimes.codex import CodexRuntime
from runtimes.gemini import GeminiRuntime
from runtimes.mini import MiniRuntime


REPO_ROOT = Path(__file__).resolve().parents[1]
CACHE_REPLACEMENT = REPO_ROOT / "challenges" / "cache_replacement"
_NO_BUNDLED_CHALLENGES = "public framework release does not bundle challenge corpus"


# ---------------------------------------------------------------------------
# Registry / identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,cls,image", [
    ("claude_code", ClaudeCodeRuntime, "localhost/archbench-agent:v6"),
    ("codex",       CodexRuntime,       "localhost/archbench-agent-codex:v6"),
    ("gemini",      GeminiRuntime,      "localhost/archbench-agent-gemini:v6"),
    ("archharness", ArchharnessRuntime, "localhost/archbench-agent-archharness:v6"),
    ("mini",        MiniRuntime,        "localhost/archbench-agent-mini:v6"),
])
def test_runtime_identity(name, cls, image):
    rt = get_runtime(name)
    assert isinstance(rt, cls)
    assert rt.name == name
    assert rt.docker_image == image


def test_unknown_runtime_raises():
    with pytest.raises(KeyError, match="No agent runtime"):
        get_runtime("does_not_exist")


# ---------------------------------------------------------------------------
# from_runtime_spec: challenge.yaml runtimes block → runtime instance
# ---------------------------------------------------------------------------


def _spec(name: str, **data) -> RuntimeSpec:
    return RuntimeSpec(
        name=name,
        image=data.pop("image", None),
        expected_version=data.pop("runtime_version", None),
        model=data.pop("model", None),
        round_timeout=data.pop("round_timeout", 14400),
        max_turns=data.pop("max_turns", 400),
        data=data,
    )


def test_claude_code_from_spec_picks_up_yaml_version():
    spec = _spec(
        "claude_code", runtime_version="2.2.0", model="claude-opus-4-7",
        oauth_token_file="~/.config/archbench/oauth_token",
    )
    rt = ClaudeCodeRuntime.from_runtime_spec(spec)
    assert rt.expected_version == "2.2.0"
    assert rt.model == "claude-opus-4-7"


def test_codex_from_spec_pins_version():
    rt = CodexRuntime.from_runtime_spec(_spec(
        "codex", runtime_version="0.121.0", model="gpt-5",
    ))
    assert rt.expected_version == "0.121.0"
    assert rt.model == "gpt-5"


def test_gemini_from_spec_resolves_auth_files():
    rt = GeminiRuntime.from_runtime_spec(_spec(
        "gemini", runtime_version="0.38.1",
        auth_files=["~/.gemini/oauth_creds.json", "~/.gemini/google_accounts.json"],
    ))
    assert rt.expected_version == "0.38.1"
    assert rt.oauth_path.endswith("oauth_creds.json")
    assert rt.accounts_path.endswith("google_accounts.json")


def test_archharness_from_spec_uses_model():
    rt = ArchharnessRuntime.from_runtime_spec(_spec(
        "archharness", runtime_version="v6", model="google/gemma-4-31B-it",
    ))
    assert rt.model == "google/gemma-4-31B-it"
    assert rt.expected_version == "v6"


def test_mini_from_spec_uses_model():
    rt = MiniRuntime.from_runtime_spec(_spec(
        "mini", runtime_version="v6", model="google/gemma-4-31B-it",
    ))
    assert rt.model == "google/gemma-4-31B-it"


# ---------------------------------------------------------------------------
# auth contracts
# ---------------------------------------------------------------------------


def test_claude_code_auth_fails_on_missing_token(monkeypatch):
    """Past bug: silent OAuth fallback. Now: missing token → loud FNF."""
    monkeypatch.setattr(
        "runtimes.claude_code.runner.DEFAULT_OAUTH_TOKEN_PATH",
        "/tmp/definitely_does_not_exist_archbench_oauth",
    )
    rt = ClaudeCodeRuntime(oauth_token_path="/tmp/definitely_does_not_exist_archbench_oauth")
    with pytest.raises(FileNotFoundError, match="OAuth token missing"):
        rt.auth()


def test_archharness_auth_fails_on_missing_endpoint(monkeypatch):
    """LLM_BASE_URL must be set explicitly; no silent fallback."""
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    rt = ArchharnessRuntime()
    with pytest.raises(RuntimeError, match="LLM_BASE_URL"):
        rt.auth()


def test_archharness_auth_uses_env(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://fake-vllm:8000")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    rt = ArchharnessRuntime()
    auth = rt.auth()
    assert auth.env_vars["LLM_BASE_URL"] == "http://fake-vllm:8000"
    assert auth.env_vars["LLM_API_KEY"] == "test-key"
    assert auth.endpoint_url == "http://fake-vllm:8000"


def test_mini_auth_fails_on_missing_endpoint(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    rt = MiniRuntime()
    with pytest.raises(RuntimeError, match="LLM_BASE_URL"):
        rt.auth()


# ---------------------------------------------------------------------------
# verify_runtime — host-side preflight
# ---------------------------------------------------------------------------


def test_archharness_verify_runtime_no_url(monkeypatch):
    """verify_runtime is a HOST-side pre-flight that runs BEFORE
    session.run_session has had a chance to start the proxy. Since the
    proxy URL is supplied dynamically (not from env or info.yaml), the
    pre-flight no longer treats missing LLM_BASE_URL as a hard error —
    that's verify_in_container's job, post-proxy-startup."""
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    rt = ArchharnessRuntime()
    errors = rt.verify_runtime(Path("/tmp"))
    assert errors == []


def test_claude_code_verify_runtime_missing_token(monkeypatch):
    rt = ClaudeCodeRuntime(oauth_token_path="/tmp/definitely_missing_oauth_xxx")
    errors = rt.verify_runtime(Path("/tmp"))
    assert any("oauth token missing" in e for e in errors)


# ---------------------------------------------------------------------------
# runtime_from_challenge integration: load challenge + spec → runtime
# ---------------------------------------------------------------------------


def test_runtime_from_challenge_for_archharness():
    """Loading cache_replacement and asking for archharness should
    return an ArchharnessRuntime initialized from yaml model/version.

    For BYO runtimes, the model id is a routes.yaml key (resolved by the
    host-side proxy, not a vendor model name).
    """
    if not CACHE_REPLACEMENT.exists():
        pytest.skip(_NO_BUNDLED_CHALLENGES)
    from archbench.core.challenge import load_challenge
    ch = load_challenge(CACHE_REPLACEMENT)  # root = L3
    rt = runtime_from_challenge("archharness", ch)
    assert isinstance(rt, ArchharnessRuntime)
    assert rt.expected_version == "v6"
    assert rt.model == "gemma4"


def test_runtime_from_challenge_unknown_runtime():
    """Asking for a runtime the challenge.yaml doesn't list → KeyError."""
    if not CACHE_REPLACEMENT.exists():
        pytest.skip(_NO_BUNDLED_CHALLENGES)
    from archbench.core.challenge import load_challenge
    ch = load_challenge(CACHE_REPLACEMENT)  # root = L3
    with pytest.raises(KeyError):
        runtime_from_challenge("nonexistent", ch)


def test_stage_workspace_keeps_advisory_check_storage_for_agent(tmp_path):
    """The submit oracle no longer runs this helper, but agents still can."""
    sim_dir = tmp_path / "simulator"
    eval_dir = tmp_path / "evaluation"
    sim_dir.mkdir()
    eval_dir.mkdir()
    checker = "#!/usr/bin/env python3\nprint('agent self-check')\n"
    (sim_dir / "check_storage.py").write_text(checker)

    challenge = Challenge(
        id="self_check", name="self_check", simulator="champsim", prompt="",
        starter_files=[], output_files=[], eval=EvalConfig(metric="ipc"),
        simulator_config={}, challenge_dir=tmp_path,
        simulator_dir=sim_dir, evaluation_dir=eval_dir,
    )

    class FakeAgent:
        def __init__(self):
            self.files = {}

        def exec(self, command, timeout=0):
            return "", 0

        def write_file(self, name, content, base_dir="/workspace"):
            self.files[f"{base_dir}/{name}"] = content

    agent = FakeAgent()
    MiniRuntime().stage_workspace(agent, challenge, starter_visibility="none")

    assert agent.files["/workspace/check_storage.py"] == checker


# ---------------------------------------------------------------------------
# bundled vs. byo_model: all 5 real info.yaml files parse with correct types
# ---------------------------------------------------------------------------


def test_all_five_runtimes_parse_with_correct_types():
    """End-to-end: load a real challenge and verify each runtime's
    info.yaml is parsed into the correct runtime_type bucket.

    Bundled (claude_code / codex / gemini) MUST carry vendor + auth +
    non-empty allowed_models. BYO (mini / archharness) MUST default
    those to None / empty.
    """
    if not CACHE_REPLACEMENT.exists():
        pytest.skip(_NO_BUNDLED_CHALLENGES)
    from archbench.core.challenge import load_challenge
    ch = load_challenge(CACHE_REPLACEMENT)  # root = L3
    assert set(ch.runtimes.keys()) >= {
        "claude_code", "codex", "gemini", "archharness", "mini",
    }
    bundled_expected = {
        "claude_code": "anthropic",
        "codex":       "openai",
        "gemini":      "google",
    }
    for name, vendor in bundled_expected.items():
        spec = ch.runtimes[name]
        assert spec.runtime_type == "bundled", f"{name} runtime_type"
        assert spec.vendor == vendor, f"{name} vendor"
        assert isinstance(spec.auth, dict) and spec.auth, f"{name} auth"
        assert "method" in spec.auth, f"{name} auth.method"
        assert "host_path" in spec.auth, f"{name} auth.host_path"
        assert "container_path" in spec.auth, f"{name} auth.container_path"
        assert spec.allowed_models, f"{name} allowed_models non-empty"
        assert spec.model in spec.allowed_models, (
            f"{name} default_model={spec.model} must be in "
            f"allowed_models={spec.allowed_models}"
        )

    for name in ("mini", "archharness"):
        spec = ch.runtimes[name]
        assert spec.runtime_type == "byo_model", f"{name} runtime_type"
        assert spec.auth is None, f"{name} auth"
        assert spec.allowed_models == [], f"{name} allowed_models"
