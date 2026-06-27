"""RunSpec — the YAML front door. Load + tier resolution + overrides + the
loud validation that makes a malformed spec fail at parse time (not mid-run)."""
import pytest

from archbench.core.run_spec import REPO_ROOT, RunSpec


def _write(tmp_path, body):
    p = tmp_path / "run.yaml"
    p.write_text(body)
    return p


def test_family_plus_tier_resolves_to_assisted(tmp_path):
    spec = RunSpec.from_yaml(_write(tmp_path, "challenge: branch_predictor\ntier: L1\nagent: mini\n"))
    assert spec.challenge_dir == (REPO_ROOT / "challenges/branch_predictor/assisted/L1").resolve()
    assert spec.tier == "L1"
    assert spec.agent == "mini"


def test_tier_defaults_to_L3_family_root(tmp_path):
    spec = RunSpec.from_yaml(_write(tmp_path, "challenge: branch_predictor\nagent: mini\n"))
    assert spec.challenge_dir == (REPO_ROOT / "challenges/branch_predictor").resolve()
    assert spec.tier == "L3"


def test_explicit_path_used_verbatim(tmp_path):
    spec = RunSpec.from_yaml(
        _write(tmp_path, "challenge: challenges/branch_predictor/assisted/L2\nagent: mini\n"))
    assert spec.challenge_dir == (REPO_ROOT / "challenges/branch_predictor/assisted/L2").resolve()


def test_agent_override_wins(tmp_path):
    spec = RunSpec.from_yaml(
        _write(tmp_path, "challenge: branch_predictor\nagent: mini\n"),
        agent_override="claude_code")
    assert spec.agent == "claude_code"


def test_defaults(tmp_path):
    spec = RunSpec.from_yaml(_write(tmp_path, "challenge: branch_predictor\nagent: mini\n"))
    assert spec.anonymize is True
    assert spec.model is None
    assert spec.dev is False
    assert spec.thinking is False


def test_full_spec_parses(tmp_path):
    spec = RunSpec.from_yaml(_write(tmp_path,
        "challenge: branch_predictor\ntier: L1\nagent: mini\nmodel: gemma4\n"
        "anonymize: false\nrun_name: r1\ndev: true\nthinking: true\n"))
    assert spec.model == "gemma4"
    assert spec.anonymize is False
    assert spec.run_name == "r1"
    assert spec.dev is True and spec.thinking is True


def test_missing_challenge_raises(tmp_path):
    with pytest.raises(ValueError, match="challenge"):
        RunSpec.from_yaml(_write(tmp_path, "agent: mini\n"))


def test_bad_tier_raises(tmp_path):
    with pytest.raises(ValueError, match="tier"):
        RunSpec.from_yaml(_write(tmp_path, "challenge: branch_predictor\ntier: L9\nagent: mini\n"))


def test_missing_agent_raises(tmp_path):
    with pytest.raises(ValueError, match="agent"):
        RunSpec.from_yaml(_write(tmp_path, "challenge: branch_predictor\n"))
