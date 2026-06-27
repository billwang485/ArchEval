"""Challenge YAML loader + new unified runtimes schema."""

import textwrap

import pytest

from archbench.core.challenge import (
    Challenge,
    RuntimeSpec,
    list_challenges,
    load_challenge,
)


def _write_challenge(tmp_path, yaml_text, starter_files=None):
    """Helper: drop a challenge.yaml + starter/ in tmp_path."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "challenge.yaml").write_text(textwrap.dedent(yaml_text).lstrip())
    starter = tmp_path / "starter"
    starter.mkdir(exist_ok=True)
    for fname, content in (starter_files or {"main.cc": "int main(){}"}).items():
        (starter / fname).write_text(content)
    return tmp_path


def test_minimal_challenge_loads(tmp_path):
    _write_challenge(tmp_path, """
        id: test_ch
        name: "Test challenge"
        simulator: champsim
        prompt: "Implement an X"
        eval:
          metric: ipc
          direction: higher_is_better
          max_submissions: 3
        input: {starter_files: [main.cc]}
        output: {files: [main.cc]}
        simulator_config: {script: simulate.sh}
    """)
    ch = load_challenge(tmp_path)
    assert ch.id == "test_ch"
    assert ch.name == "Test challenge"
    assert ch.simulator == "champsim"
    assert ch.eval.metric == "ipc"
    assert ch.eval.max_submissions == 3
    assert "main.cc" in ch.starter_code
    assert ch.starter_code["main.cc"] == "int main(){}"
    assert ch.runtimes == {}  # none declared


def test_legacy_agent_blocks_unify_into_runtimes(tmp_path):
    """The 4-way *_agent schema fork (CLAUDE/CODEX/GEMINI/ARCHHARNESS)
    collapses into one runtimes: dict. Verifies the legacy fix."""
    _write_challenge(tmp_path, """
        id: legacy
        name: legacy
        simulator: champsim
        prompt: ""
        input: {starter_files: [main.cc]}
        output: {files: [main.cc]}
        simulator_config: {script: simulate.sh}
        agent:
          runtime_version: "2.1.85"
          model: "claude-opus-4-6"
        codex_agent:
          runtime_version: "0.121.0"
          model: "gpt-5"
        gemini_agent:
          runtime_version: "0.38.1"
        archharness_agent:
          runtime_version: "v6"
          model: "google/gemma-4-31B-it"
          round_timeout: 18000
        mini_agent:
          runtime_version: "v6"
    """)
    ch = load_challenge(tmp_path)
    assert set(ch.runtimes.keys()) == {
        "claude_code", "codex", "gemini", "archharness", "mini",
    }
    assert ch.runtimes["claude_code"].expected_version == "2.1.85"
    assert ch.runtimes["claude_code"].model == "claude-opus-4-6"
    assert ch.runtimes["archharness"].round_timeout == 18000
    assert ch.runtimes["archharness"].model == "google/gemma-4-31B-it"


def test_legacy_runtimes_block_still_loads_with_warning(tmp_path, caplog):
    """A yaml with the legacy `runtimes:` block (carrying image/model/version/
    timeouts) still loads but emits a deprecation warning. Per-runtime config
    should migrate to runtimes/<rt>/info.yaml."""
    import logging
    _write_challenge(tmp_path, """
        id: legacy_runtimes
        name: legacy_runtimes
        simulator: champsim
        prompt: ""
        input: {starter_files: [main.cc]}
        output: {files: [main.cc]}
        simulator_config: {script: simulate.sh}
        runtimes:
          claude_code:
            runtime_version: "2.1.85"
            model: "claude-opus-4-7"
            round_timeout: 7200
            max_turns: 200
    """)
    with caplog.at_level(logging.WARNING, logger="archbench.challenge"):
        ch = load_challenge(tmp_path)
    # Still loads correctly
    assert "claude_code" in ch.runtimes
    assert ch.runtimes["claude_code"].model == "claude-opus-4-7"
    assert ch.runtimes["claude_code"].round_timeout == 7200
    # And we emitted the deprecation warning
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        "legacy 'runtimes:' block" in r.getMessage() for r in warnings
    ), f"expected deprecation warning, got: {[r.getMessage() for r in warnings]}"


def test_lifecycle_default_is_standby(tmp_path):
    """If `simulator_config.lifecycle` is absent, default to 'standby'."""
    _write_challenge(tmp_path, """
        id: lc_default
        name: lc_default
        simulator: champsim
        prompt: ""
        input: {starter_files: [main.cc]}
        output: {files: [main.cc]}
        simulator_config: {script: simulate.sh}
    """)
    ch = load_challenge(tmp_path)
    assert ch.lifecycle == "standby"


def test_lifecycle_standby_explicit(tmp_path):
    """Explicit lifecycle: standby is parsed verbatim."""
    _write_challenge(tmp_path, """
        id: lc_standby
        name: lc_standby
        simulator: champsim
        prompt: ""
        input: {starter_files: [main.cc]}
        output: {files: [main.cc]}
        simulator_config:
          script: simulate.sh
          lifecycle: standby
    """)
    ch = load_challenge(tmp_path)
    assert ch.lifecycle == "standby"


def test_lifecycle_lazy_explicit(tmp_path):
    """Explicit lifecycle: lazy is parsed verbatim (single-shot variant)."""
    _write_challenge(tmp_path, """
        id: lc_lazy
        name: lc_lazy
        simulator: champsim
        prompt: ""
        input: {starter_files: [main.cc]}
        output: {files: [main.cc]}
        simulator_config:
          script: simulate.sh
          lifecycle: lazy
    """)
    ch = load_challenge(tmp_path)
    assert ch.lifecycle == "lazy"


def test_slim_schema_runtime_spec_fields_are_none(tmp_path):
    """With the slim schema, RuntimeSpec timeouts/turns are None until
    the runtimes/<rt>/info.yaml loader fills them in. Test that we
    don't accidentally default them to legacy ints."""
    # Slim block: empty runtime entry — no image/model/version/timeout.
    _write_challenge(tmp_path, """
        id: slim2
        name: slim2
        simulator: champsim
        prompt: ""
        input: {starter_files: [main.cc]}
        output: {files: [main.cc]}
        simulator_config: {script: simulate.sh}
        runtimes:
          claude_code: {}
    """)
    ch = load_challenge(tmp_path)
    spec = ch.runtimes["claude_code"]
    # Slim shape leaves these unset.
    assert spec.round_timeout is None
    assert spec.max_turns is None
    assert spec.image is None
    assert spec.model is None
    assert spec.expected_version is None


def test_explicit_runtimes_block_wins_over_legacy(tmp_path):
    """If both new `runtimes:` and old `agent:` are present, new wins."""
    _write_challenge(tmp_path, """
        id: dup
        name: dup
        simulator: champsim
        prompt: ""
        input: {starter_files: [main.cc]}
        output: {files: [main.cc]}
        simulator_config: {script: simulate.sh}
        runtimes:
          claude_code:
            model: "claude-opus-4-7"
            runtime_version: "2.2.0"
        agent:
          model: "claude-opus-4-6"
          runtime_version: "2.1.85"
    """)
    ch = load_challenge(tmp_path)
    # Legacy block did NOT overwrite the new one
    assert ch.runtimes["claude_code"].model == "claude-opus-4-7"
    assert ch.runtimes["claude_code"].expected_version == "2.2.0"


def test_runtime_for_unknown_raises(tmp_path):
    _write_challenge(tmp_path, """
        id: x
        name: x
        simulator: champsim
        prompt: ""
        input: {starter_files: [main.cc]}
        output: {files: [main.cc]}
        simulator_config: {script: simulate.sh}
        runtimes:
          claude_code: {model: claude-opus-4-7}
    """)
    ch = load_challenge(tmp_path)
    with pytest.raises(KeyError, match="does not declare runtime"):
        ch.runtime_for("nonexistent_runtime")


def test_missing_starter_file_is_loud(tmp_path):
    """If challenge.yaml declares starter_files but the file isn't
    in starter/, fail loudly. Past bug: empty starter/ was silently
    accepted, agent saw a missing reference."""
    _write_challenge(
        tmp_path,
        """
            id: m
            name: m
            simulator: champsim
            prompt: ""
            input: {starter_files: [present.cc, missing.cc]}
            output: {files: [present.cc]}
            simulator_config: {script: simulate.sh}
        """,
        starter_files={"present.cc": "// here"},
    )
    with pytest.raises(FileNotFoundError, match="missing.cc"):
        load_challenge(tmp_path)


def test_no_yaml_at_all(tmp_path):
    with pytest.raises(FileNotFoundError, match="No challenge.yaml"):
        load_challenge(tmp_path)


def test_legacy_storage_keys_are_not_core_fields(tmp_path):
    _write_challenge(tmp_path, """
        id: s
        name: s
        simulator: champsim
        prompt: ""
        storage_limit_bytes: 4096
        storage_check_script: check_storage.py
        input: {starter_files: [main.cc]}
        output: {files: [main.cc]}
        simulator_config: {script: simulate.sh}
    """)
    ch = load_challenge(tmp_path)
    assert not hasattr(ch, "storage_limit_bytes")
    assert not hasattr(ch, "storage_check_script")


def test_list_challenges_skips_dirs_without_yaml(tmp_path):
    # Two child dirs, one with yaml, one without
    a = tmp_path / "a"
    _write_challenge(a, """
        id: a
        name: a
        simulator: champsim
        prompt: ""
        input: {starter_files: [main.cc]}
        output: {files: [main.cc]}
        simulator_config: {script: simulate.sh}
    """)
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "not_a_challenge.txt").write_text("nope")

    chs = list_challenges(tmp_path)
    assert [c.id for c in chs] == ["a"]


# ---------------------------------------------------------------------------
# bundled vs. byo_model schema (runtimes/<rt>/info.yaml)
# ---------------------------------------------------------------------------


def _write_info_yaml(repo_root, rt_name, body):
    """Helper: scaffold a fake repo with runtimes/<rt>/info.yaml so the
    challenge loader's info.yaml merge has something to read."""
    rt_dir = repo_root / "runtimes" / rt_name
    rt_dir.mkdir(parents=True, exist_ok=True)
    (rt_dir / "info.yaml").write_text(textwrap.dedent(body).lstrip())


def _write_challenge_in_repo(repo_root, ch_id, yaml_text, starter_files=None):
    """Helper: place a challenge at <repo_root>/challenges/<ch_id>/.

    Required because the loader looks for runtimes/<rt>/info.yaml relative
    to the repo root (challenge_dir.parents[1]).
    """
    ch_dir = repo_root / "challenges" / ch_id
    return _write_challenge(ch_dir, yaml_text, starter_files)


def test_bundled_info_yaml_populates_type_vendor_auth_allowed_models(tmp_path):
    """A bundled info.yaml fills runtime_type, vendor, auth, allowed_models."""
    _write_info_yaml(tmp_path, "claude_code", """
        name: claude_code
        image: localhost/archbench-agent:v6
        runtime_version: "2.1.85"
        default_model: claude-opus-4-7
        type: bundled
        vendor: anthropic
        auth:
          method: oauth_token
          host_path: ~/.config/archbench/oauth_token
          container_path: /home/agent/.claude/credentials.json
        allowed_models:
          - claude-opus-4-7
          - claude-sonnet-4-6
    """)
    _write_challenge_in_repo(tmp_path, "bundled_ch", """
        id: bundled_ch
        name: bundled_ch
        simulator: champsim
        prompt: ""
        input: {starter_files: [main.cc]}
        output: {files: [main.cc]}
        simulator_config: {script: simulate.sh}
    """)
    ch = load_challenge(tmp_path / "challenges" / "bundled_ch")
    spec = ch.runtimes["claude_code"]
    assert spec.runtime_type == "bundled"
    assert spec.vendor == "anthropic"
    assert spec.auth is not None
    assert spec.auth["method"] == "oauth_token"
    assert spec.auth["host_path"] == "~/.config/archbench/oauth_token"
    assert spec.auth["container_path"] == "/home/agent/.claude/credentials.json"
    assert spec.allowed_models == ["claude-opus-4-7", "claude-sonnet-4-6"]
    assert spec.model == "claude-opus-4-7"


def test_byo_model_info_yaml_defaults_empty_auth_allowed(tmp_path):
    """A byo_model info.yaml: type=byo_model, auth=None, allowed_models=[]."""
    _write_info_yaml(tmp_path, "mini", """
        name: mini
        image: localhost/archbench-agent-mini:v6
        runtime_version: "v6"
        default_model: gemma4
        type: byo_model
    """)
    _write_challenge_in_repo(tmp_path, "byo_ch", """
        id: byo_ch
        name: byo_ch
        simulator: champsim
        prompt: ""
        input: {starter_files: [main.cc]}
        output: {files: [main.cc]}
        simulator_config: {script: simulate.sh}
    """)
    ch = load_challenge(tmp_path / "challenges" / "byo_ch")
    spec = ch.runtimes["mini"]
    assert spec.runtime_type == "byo_model"
    assert spec.vendor is None
    assert spec.auth is None
    assert spec.allowed_models == []
    assert spec.model == "gemma4"


def test_missing_type_defaults_to_byo_model_with_warning(tmp_path, caplog):
    """If info.yaml omits `type`, default to byo_model (safer fallback).

    No warning is emitted for the missing key (the default kicks in
    silently); a warning IS emitted if `type` is set to something unknown.
    """
    _write_info_yaml(tmp_path, "mini", """
        name: mini
        image: localhost/archbench-agent-mini:v6
        runtime_version: "v6"
        default_model: gemma4
    """)
    _write_challenge_in_repo(tmp_path, "notype", """
        id: notype
        name: notype
        simulator: champsim
        prompt: ""
        input: {starter_files: [main.cc]}
        output: {files: [main.cc]}
        simulator_config: {script: simulate.sh}
    """)
    ch = load_challenge(tmp_path / "challenges" / "notype")
    assert ch.runtimes["mini"].runtime_type == "byo_model"


def test_bundled_without_auth_raises(tmp_path):
    """A bundled runtime missing `auth:` is a hard schema error.

    Loud-raise (not graceful-degrade) because a missing auth on a
    vendor-bound runtime would silently fall through to whatever default
    the runner picks up — usually wrong credentials.
    """
    _write_info_yaml(tmp_path, "claude_code", """
        name: claude_code
        image: localhost/archbench-agent:v6
        runtime_version: "2.1.85"
        default_model: claude-opus-4-7
        type: bundled
        vendor: anthropic
        # no auth: ...
        allowed_models:
          - claude-opus-4-7
    """)
    _write_challenge_in_repo(tmp_path, "bad_bundled", """
        id: bad_bundled
        name: bad_bundled
        simulator: champsim
        prompt: ""
        input: {starter_files: [main.cc]}
        output: {files: [main.cc]}
        simulator_config: {script: simulate.sh}
    """)
    with pytest.raises(ValueError, match="type=bundled requires an `auth:`"):
        load_challenge(tmp_path / "challenges" / "bad_bundled")


def test_bundled_empty_allowed_models_raises(tmp_path):
    """A bundled runtime with empty allowed_models is a hard schema error."""
    _write_info_yaml(tmp_path, "claude_code", """
        name: claude_code
        image: localhost/archbench-agent:v6
        runtime_version: "2.1.85"
        default_model: claude-opus-4-7
        type: bundled
        vendor: anthropic
        auth:
          method: oauth_token
          host_path: ~/.config/archbench/oauth_token
          container_path: /home/agent/.claude/credentials.json
        allowed_models: []
    """)
    _write_challenge_in_repo(tmp_path, "empty_allowed", """
        id: empty_allowed
        name: empty_allowed
        simulator: champsim
        prompt: ""
        input: {starter_files: [main.cc]}
        output: {files: [main.cc]}
        simulator_config: {script: simulate.sh}
    """)
    with pytest.raises(ValueError, match="non-empty `allowed_models:`"):
        load_challenge(tmp_path / "challenges" / "empty_allowed")


def test_bundled_default_model_not_in_allowed_models_raises(tmp_path):
    """default_model MUST be in allowed_models for bundled runtimes."""
    _write_info_yaml(tmp_path, "claude_code", """
        name: claude_code
        image: localhost/archbench-agent:v6
        runtime_version: "2.1.85"
        default_model: claude-opus-4-7
        type: bundled
        vendor: anthropic
        auth:
          method: oauth_token
          host_path: ~/.config/archbench/oauth_token
          container_path: /home/agent/.claude/credentials.json
        allowed_models:
          - claude-sonnet-4-6      # default_model not listed here
    """)
    _write_challenge_in_repo(tmp_path, "default_mismatch", """
        id: default_mismatch
        name: default_mismatch
        simulator: champsim
        prompt: ""
        input: {starter_files: [main.cc]}
        output: {files: [main.cc]}
        simulator_config: {script: simulate.sh}
    """)
    with pytest.raises(ValueError, match="not in allowed_models"):
        load_challenge(tmp_path / "challenges" / "default_mismatch")


def test_byo_model_with_auth_warns_and_drops(tmp_path, caplog):
    """If a byo_model info.yaml carries `auth:` / `allowed_models:`, those
    are dropped (proxy is source of truth) and a warning is emitted."""
    import logging
    _write_info_yaml(tmp_path, "mini", """
        name: mini
        image: localhost/archbench-agent-mini:v6
        runtime_version: "v6"
        default_model: gemma4
        type: byo_model
        auth:
          method: api_key
          host_path: ~/.somewhere
          container_path: /home/agent/.somewhere
        allowed_models:
          - should-be-ignored
    """)
    _write_challenge_in_repo(tmp_path, "byo_with_auth", """
        id: byo_with_auth
        name: byo_with_auth
        simulator: champsim
        prompt: ""
        input: {starter_files: [main.cc]}
        output: {files: [main.cc]}
        simulator_config: {script: simulate.sh}
    """)
    with caplog.at_level(logging.WARNING, logger="archbench.challenge"):
        ch = load_challenge(tmp_path / "challenges" / "byo_with_auth")
    spec = ch.runtimes["mini"]
    assert spec.runtime_type == "byo_model"
    assert spec.auth is None
    assert spec.allowed_models == []
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno >= logging.WARNING]
    assert any("byo_model" in m and "auth" in m for m in warnings), warnings
    assert any("byo_model" in m and "allowed_models" in m for m in warnings), warnings


def test_unknown_type_degrades_to_byo_model(tmp_path, caplog):
    """An unknown `type:` value degrades to byo_model with a warning."""
    import logging
    _write_info_yaml(tmp_path, "mini", """
        name: mini
        image: localhost/archbench-agent-mini:v6
        runtime_version: "v6"
        default_model: gemma4
        type: nonsense
    """)
    _write_challenge_in_repo(tmp_path, "unknown_type", """
        id: unknown_type
        name: unknown_type
        simulator: champsim
        prompt: ""
        input: {starter_files: [main.cc]}
        output: {files: [main.cc]}
        simulator_config: {script: simulate.sh}
    """)
    with caplog.at_level(logging.WARNING, logger="archbench.challenge"):
        ch = load_challenge(tmp_path / "challenges" / "unknown_type")
    assert ch.runtimes["mini"].runtime_type == "byo_model"
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("unknown type" in m for m in msgs), msgs


def test_list_challenges_simulator_filter(tmp_path):
    a = tmp_path / "a"; b = tmp_path / "b"
    _write_challenge(a, """
        id: a
        name: a
        simulator: champsim
        prompt: ""
        input: {starter_files: [main.cc]}
        output: {files: [main.cc]}
        simulator_config: {script: simulate.sh}
    """)
    _write_challenge(b, """
        id: b
        name: b
        simulator: gem5
        prompt: ""
        input: {starter_files: [main.cc]}
        output: {files: [main.cc]}
        simulator_config: {script: simulate.sh}
    """)
    chs = list_challenges(tmp_path, simulator="champsim")
    assert [c.id for c in chs] == ["a"]
