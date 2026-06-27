"""Container card — pure logic (no docker): check-script build, output parse,
role defaults, and the yaml round-trip. The container-touching stamp/verify are
exercised live by scripts/, not here."""
from archbench.core import container_card as cc


def test_build_check_script_covers_all_clauses():
    expect = {
        "paths_present": ["/work/runtimes/champsim"],
        "paths_absent": ["/archeval", "/opt/legacy"],
        "file_sha256": {"/work/build_and_run.sh": "abc123"},
        "env_absent_tokens": ["archbench", "archeval"],
    }
    s = cc._build_check_script(expect)
    assert "present /work/runtimes/champsim" in s
    assert "absent /archeval" in s and "absent /opt/legacy" in s
    assert "abc123" in s and "/work/build_and_run.sh" in s
    assert "archbench|archeval" in s  # env grep pattern


def test_parse_check_output_extracts_only_fails():
    out = "\n".join([
        "OK present /work/runtimes/champsim",
        "FAIL absent /archeval present",
        "OK env",
        "FAIL sha /work/build_and_run.sh got=deadbeef",
    ])
    fails = cc._parse_check_output(out)
    assert fails == ["absent /archeval present", "sha /work/build_and_run.sh got=deadbeef"]


def test_parse_clean_output_is_empty():
    out = "OK present /x\nOK absent /archeval\nOK env\n"
    assert cc._parse_check_output(out) == []


def test_role_defaults_simulator_has_sim_source_and_neutrality():
    d = cc.role_defaults("simulator", "champsim")
    assert "/work/runtimes/champsim" in d["paths_present"]
    assert "/archeval" in d["paths_absent"]
    assert "archbench" in d["env_absent_tokens"]


def test_role_defaults_agent_has_no_sim_source():
    d = cc.role_defaults("agent", None)
    assert all("runtimes" not in p for p in d["paths_present"])
    assert "/archeval" in d["paths_absent"]


def test_card_write_load_roundtrip(tmp_path):
    card = {"image": "x:v6", "role": "simulator",
            "expect": {"paths_present": ["/work"], "paths_absent": ["/archeval"],
                       "file_sha256": {}, "env_absent_tokens": ["archbench"]}}
    p = tmp_path / "x-v6.card.yaml"
    cc.write_card(card, p)
    assert cc.load_card(p) == card
    assert cc.load_card(tmp_path / "nope.card.yaml") is None


def test_empty_expect_still_captures_top_level_no_hard_checks():
    s = cc._build_check_script({})
    assert "TOPLEVEL::" in s                 # always snapshots the top level
    assert "present" not in s and "FAIL" not in s  # but no hard path/sha/env checks
    assert cc._parse_check_output("") == []


def test_parse_top_level_extracts_listing():
    out = 'TOPLEVEL::bin work workspace usr\nOK present /work\n'
    assert cc._parse_top_level(out) == ["bin", "usr", "work", "workspace"]


def test_toplevel_allowlist_flags_extra_and_missing():
    declared = ["bin", "work", "workspace"]
    actual = ["bin", "work", "archeval"]  # extra /archeval, missing /workspace
    v = cc._toplevel_violations(declared, actual)
    assert any("UNEXPECTED" in x and "archeval" in x for x in v)
    assert any("MISSING" in x and "workspace" in x for x in v)
    assert cc._toplevel_violations(declared, declared) == []  # exact match -> clean


def test_render_pretty_is_human_readable():
    card = {"image": "localhost/archbench-champsim:v6", "role": "simulator",
            "stamped_at_commit": "9f1daf4",
            "expect": {"paths_present": ["/work/runtimes/champsim"],
                       "paths_absent": ["/archeval", "/opt/legacy"],
                       "file_sha256": {"/work/build_and_run.sh": "dceec3bd7348aa"},
                       "env_absent_tokens": ["archbench", "archeval"]}}
    out = cc.render_pretty(card)
    assert "MUST have" in out and "MUST NOT have" in out
    assert "/work/runtimes/champsim" in out and "simulator's source" in out
    assert "byte-for-byte" in out and "dceec3bd7348" in out
    assert "env must NOT contain: archbench, archeval" in out
    assert "verify localhost/archbench-champsim:v6" in out


def test_card_lives_in_repo_not_in_gitignored_docker():
    """A card is a version-controlled contract -> image_cards/ in the repo, NOT
    docker/ (next to the gitignored tars). Guards the cross-session reproducibility
    fix (a card that isn't committed is useless on another machine)."""
    p = cc.card_path_for("localhost/archbench-champsim:v6")
    assert p == cc.REPO_ROOT / "image_cards" / "archbench-champsim-v6.card.yaml"
    assert "/docker/" not in str(p)
