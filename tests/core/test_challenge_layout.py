"""Layout detection + path resolution + starter_visibility for load_challenge.

Covers:
  1. Family-root (mode 2) path resolution — shared ``common/simulator/`` +
     ``common/evaluation/``, tolerant of a missing starter dir.
  2. ``starter_visibility`` parsing (default 'full' + the 3 valid values
     + warning-on-unknown).
  3. Legacy 3-subdir backwards-compat invariants.

The interim ``tiers/<L>/`` layout + its ``_detect_tier_layout`` detector were
removed (no challenge used it; CLAUDE.md §1.3). The live layouts are the
assisted overlay (mode 1), the family root (mode 2 — tested here via the
``common/`` back-compat form), and the legacy 3-subdir form (mode 3).
"""

import textwrap

import pytest

from archbench.core.challenge import load_challenge


# --- helpers -----------------------------------------------------------------

_MIN_YAML = """\
id: {id}
name: "{name}"
simulator: champsim
prompt: "do a thing"
input: {{starter_files: [main.cc]}}
output: {{files: [main.cc]}}
simulator_config: {{script: simulate.sh}}
"""


def _write_legacy(tmp_path, **overrides):
    """Drop a minimal legacy 3-subdir challenge tree at tmp_path."""
    yaml_text = _MIN_YAML.format(id="legacy", name="legacy")
    for k, v in overrides.items():
        yaml_text += f"{k}: {v}\n"
    (tmp_path / "challenge.yaml").write_text(yaml_text)
    starter = tmp_path / "challenge" / "starter"
    starter.mkdir(parents=True)
    (starter / "main.cc").write_text("int main(){}")
    (tmp_path / "simulator").mkdir()
    (tmp_path / "evaluation").mkdir()
    return tmp_path


def _write_family(tmp_path, *, starter_files=None, yaml_extra="", create_starter=True):
    """Drop a minimal FAMILY-ROOT tree (the live mode-2 layout) at tmp_path.

    The family root IS the L3 challenge; the sibling ``assisted/`` dir is the
    detection tell-tale, and shared dirs live under ``common/`` (the back-compat
    form ``_family_shared_dirs`` falls back to when there is no flat
    ``simulator/`` at the root):

      tmp_path/
      ├── challenge.yaml      ← L3
      ├── assisted/           ← tell-tale of a family root
      ├── common/{{simulator,evaluation}}/
      └── starter/            (optional)
    """
    (tmp_path / "common" / "simulator").mkdir(parents=True)
    (tmp_path / "common" / "evaluation").mkdir(parents=True)
    (tmp_path / "assisted").mkdir()
    yaml_text = _MIN_YAML.format(id="fam_l3", name="fam L3")
    yaml_text += textwrap.dedent(yaml_extra or "")
    (tmp_path / "challenge.yaml").write_text(yaml_text)
    if create_starter:
        starter = tmp_path / "starter"
        starter.mkdir()
        for fname in (starter_files or ["main.cc"]):
            (starter / fname).write_text("int main(){}")
    return tmp_path


# --- family-root (mode 2) path resolution -----------------------------------


def test_family_root_resolves_shared_dirs(tmp_path):
    root = _write_family(tmp_path)
    ch = load_challenge(root)
    assert ch.is_tier_layout is True
    assert ch.family_root == tmp_path
    assert ch.tier_name == "L3"
    assert ch.simulator_dir == tmp_path / "common" / "simulator"
    assert ch.evaluation_dir == tmp_path / "common" / "evaluation"
    assert ch.starter_dir == tmp_path / "starter"


def test_family_root_accepts_missing_starter_dir(tmp_path):
    # api_stub / none regimes may ship no starter/.
    root = _write_family(
        tmp_path, create_starter=False,
        yaml_extra='starter_visibility: "none"\n',
    )
    text = (root / "challenge.yaml").read_text()
    text = text.replace("starter_files: [main.cc]", "starter_files: []")
    (root / "challenge.yaml").write_text(text)
    ch = load_challenge(root)
    assert ch.starter_visibility == "none"
    assert ch.starter_dir == tmp_path / "starter"  # path set even if absent
    assert not ch.starter_dir.exists()
    assert ch.starter_code == {}


def test_legacy_load_preserves_old_paths(tmp_path):
    legacy = _write_legacy(tmp_path)
    ch = load_challenge(legacy)
    assert ch.is_tier_layout is False
    assert ch.family_root is None
    assert ch.tier_name is None
    assert ch.simulator_dir == legacy / "simulator"
    assert ch.evaluation_dir == legacy / "evaluation"
    assert ch.starter_dir == legacy / "challenge" / "starter"


# --- starter_visibility ------------------------------------------------------


def test_starter_visibility_defaults_to_full(tmp_path):
    legacy = _write_legacy(tmp_path)
    ch = load_challenge(legacy)
    assert ch.starter_visibility == "full"


@pytest.mark.parametrize("value", ["full", "none", "api_stub"])
def test_starter_visibility_accepts_all_valid_values(tmp_path, value):
    legacy = _write_legacy(tmp_path, starter_visibility=f'"{value}"')
    ch = load_challenge(legacy)
    assert ch.starter_visibility == value


def test_starter_visibility_unknown_value_degrades_to_full(tmp_path, caplog):
    legacy = _write_legacy(tmp_path, starter_visibility='"bogus"')
    with caplog.at_level("WARNING", logger="archbench.challenge"):
        ch = load_challenge(legacy)
    assert ch.starter_visibility == "full"
    assert any("starter_visibility" in rec.message for rec in caplog.records)


# --- new-layout interaction with the `simulator:` requirement ---------------


def test_family_root_requires_simulator_field(tmp_path):
    """A new-layout (is_tier) challenge.yaml MUST declare simulator: — the
    legacy parent-dir fallback does not apply.
    """
    root = _write_family(tmp_path)
    yaml_text = (root / "challenge.yaml").read_text()
    yaml_text = "\n".join(
        line for line in yaml_text.splitlines()
        if not line.startswith("simulator:")
    )
    (root / "challenge.yaml").write_text(yaml_text)
    with pytest.raises(ValueError, match="simulator"):
        load_challenge(root)
