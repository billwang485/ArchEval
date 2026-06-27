"""PromptBuilder — variable substitution, anonymization integration."""

import pytest

from archbench.core.anonymizer import Anonymizer
from archbench.core.challenge import Challenge, EvalConfig
from archbench.core.prompt_builder import PromptBuilder


@pytest.fixture
def sample_challenge():
    return Challenge(
        id="cache_replacement_L3",
        name="LLC replacement, IPC under 4KB",
        simulator="champsim",
        prompt=(
            "Design an LLC replacement policy. Budget: 4096 bytes (4 KB)."
        ),
        starter_files=["candidate.h", "candidate.cc"],
        output_files=["candidate.h", "candidate.cc"],
        eval=EvalConfig(metric="ipc", max_submissions=5, threshold=0.0),
        simulator_config={"script": "simulate.sh"},
        starter_code={
            "candidate.h": "// header",
            "candidate.cc": "// impl",
        },
    )


def test_prompt_md_preserves_literal_challenge_constraints(sample_challenge):
    md = PromptBuilder.build_prompt_md(sample_challenge)
    assert "Budget: 4096 bytes (4 KB)" in md


def test_prompt_md_renders_yaml_prompt_verbatim(sample_challenge):
    """P6: prompt content comes from challenge.yaml; builder only renders
    variable substitutions (no auto-appended sections)."""
    sample_challenge.prompt = (
        "Design X. Storage: 4096 bytes. Submits: {max_submissions}."
    )
    md = PromptBuilder.build_prompt_md(sample_challenge)
    assert "Storage: 4096 bytes" in md
    assert "Submits: 5" in md


def test_prompt_md_does_not_add_extras(sample_challenge):
    """Post-P6 the builder no longer appends Starter/Evaluation/Workflow
    sections — the yaml prompt is the single source of truth."""
    sample_challenge.prompt = "Just the task, nothing else."
    md = PromptBuilder.build_prompt_md(sample_challenge)
    assert md.strip() == "Just the task, nothing else."


def test_system_prompt_includes_tool_inventory(sample_challenge):
    sys_msg = PromptBuilder.build_system_prompt(sample_challenge)
    assert "submit()" in sys_msg
    assert "browse_simulator" in sys_msg
    assert "read_simulator_file" in sys_msg
    assert "/workspace/" in sys_msg


def test_prompt_md_anonymizes_via_injected_instance(sample_challenge):
    """Past bug: a global singleton made it easy to anonymize in one
    place and forget another. Now the Anonymizer is an explicit
    parameter — pass disabled for no-op, pass real to scrub."""
    sample_challenge.prompt = "Test trace 482.sphinx3-1100B works."
    anon = Anonymizer(forward={
        "482.sphinx3-1100B": "W003",
    })
    md = PromptBuilder.build_prompt_md(sample_challenge, anonymizer=anon)
    assert "482.sphinx3-1100B" not in md
    assert "W003" in md


def test_prompt_md_no_anon_by_default(sample_challenge):
    """Anonymizer is opt-in; default does not scrub."""
    sample_challenge.prompt = "Trace 482.sphinx3-1100B."
    md = PromptBuilder.build_prompt_md(sample_challenge)  # no anonymizer
    assert "482.sphinx3-1100B" in md


def test_extra_vars_override(sample_challenge):
    sample_challenge.prompt = "Custom: {my_var}"
    md = PromptBuilder.build_prompt_md(
        sample_challenge,
        extra_vars={"my_var": "hello"},
    )
    assert "Custom: hello" in md
