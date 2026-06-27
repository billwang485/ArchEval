"""Anonymizer three-layer leak detection.

Plants known SPEC benchmark names and asserts no path bypasses the
scrub/translate pair. This is the regression test for the legacy
incident where rollout to the connector's outbound metric path was
forgotten.
"""

import pytest

from archbench.core.anonymizer import (
    Anonymizer,
    AnonymizerConfig,
    KNOWN_LEAK_TOKENS,
)


@pytest.fixture
def anon():
    """Realistic mapping mirroring ChampSim's binary vs decoded trace pair."""
    return Anonymizer(forward={
        "482.sphinx3-1100B.champsimtrace.xz": "W003.champsimtrace.xz",
        "482.sphinx3-1100B.trace.txt":        "W003.trace.txt",
        "482.sphinx3-1100B":                  "W003",
        "403.gcc-16B.champsimtrace.xz":       "W007.champsimtrace.xz",
        "403.gcc-16B.trace.txt":              "W007.trace.txt",
        "403.gcc-16B":                        "W007",
        "605.mcf_s-1152B.champsimtrace.xz":   "W011.champsimtrace.xz",
        "605.mcf_s-1152B.trace.txt":          "W011.trace.txt",
        "605.mcf_s-1152B":                    "W011",
    })


def test_disabled_anonymizer_is_passthrough():
    a = Anonymizer.disabled()
    assert a.scrub_outbound("482.sphinx3-1100B") == "482.sphinx3-1100B"
    assert a.translate_inbound("W003") == "W003"
    assert not a.enabled


def test_scrub_outbound_replaces_known_names(anon):
    raw = "Trace 482.sphinx3-1100B completed; IPC 0.5113"
    assert anon.scrub_outbound(raw) == "Trace W003 completed; IPC 0.5113"


def test_translate_inbound_resolves_anon_path(anon):
    assert anon.translate_inbound("/traces/W003.trace.txt") == \
        "/traces/482.sphinx3-1100B.trace.txt"
    assert anon.translate_inbound("/traces/W003.champsimtrace.xz") == \
        "/traces/482.sphinx3-1100B.champsimtrace.xz"


def test_round_trip_through_both_layers(anon):
    """Agent emits anon name → translate_inbound → simulator returns raw
    output containing original name → scrub_outbound → agent sees anon."""
    agent_emitted = "W003.trace.txt"
    sim_received = anon.translate_inbound(agent_emitted)
    assert sim_received == "482.sphinx3-1100B.trace.txt"

    sim_output = f"Loaded {sim_received}\nDone."
    agent_view = anon.scrub_outbound(sim_output)
    assert "482.sphinx3-1100B" not in agent_view
    assert "W003.trace.txt" in agent_view


def test_no_leak_through_outbound_for_any_known_token(anon):
    """If a future trace name overlaps with a known SPEC stem and is in
    the mapping, scrub MUST replace it. This is the structural test that
    catches the legacy 'forgot to scrub the connector reply' bug."""
    for orig, _ in anon._forward.items():
        # Embed inside surrounding text — most realistic
        msg = f"Working on trace {orig} now; please wait."
        scrubbed = anon.scrub_outbound(msg)
        assert orig not in scrubbed, (
            f"Anonymizer leaked {orig!r} via scrub_outbound. "
            "This is exactly the bug class the three-layer contract exists to prevent."
        )


def test_longest_prefix_wins():
    """If two originals share a prefix (482.sphinx3-1100B vs
    482.sphinx3-1100B.champsimtrace.xz), the longer match must win or
    we'd mangle the file extension."""
    a = Anonymizer(forward={
        "482.sphinx3-1100B":                 "W003",
        "482.sphinx3-1100B.champsimtrace.xz": "W003.trace.xz",
    })
    out = a.scrub_outbound("Loading 482.sphinx3-1100B.champsimtrace.xz now")
    assert "W003.trace.xz" in out
    assert ".champsimtrace.xz" not in out


def test_enabled_requires_mapping_file():
    """Refuse to silently no-op when --anonymize is on but mapping is missing."""
    cfg = AnonymizerConfig(enabled=True, mapping_file=None)
    with pytest.raises(ValueError, match="no mapping_file"):
        Anonymizer.load(cfg)


def test_empty_mapping_file_rejected(tmp_path):
    """A blank mapping file would silently pass everything through."""
    mapping = tmp_path / "empty.json"
    mapping.write_text("{}")
    cfg = AnonymizerConfig(enabled=True, mapping_file=str(mapping))
    with pytest.raises(ValueError, match="is empty"):
        Anonymizer.load(cfg)


def test_known_leak_tokens_constant_has_seed_set():
    """Sanity: the CI canary list isn't empty and includes the names
    that have been observed leaking in past incidents."""
    assert "sphinx3" in KNOWN_LEAK_TOKENS
    assert "gcc" in KNOWN_LEAK_TOKENS
    assert "mcf" in KNOWN_LEAK_TOKENS
    assert "perlbench" in KNOWN_LEAK_TOKENS
    assert len(KNOWN_LEAK_TOKENS) >= 10
