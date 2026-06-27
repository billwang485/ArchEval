"""Provenance round-trip and drift-detection tests."""

import pytest

from archbench.core.provenance import (
    Provenance,
    sha256_of_bytes,
    sha256_of_json,
)


def _sample(image="aa" * 32, config="bb" * 32, starter="cc" * 32,
            trace="dd" * 32, commit="ee" * 32) -> Provenance:
    return Provenance(
        image_digest=image,
        config_sha256=config,
        starter_sha256=starter,
        trace_sha256=trace,
        harness_commit=commit,
    )


def test_round_trip():
    p = _sample()
    d = p.to_dict()
    assert Provenance.from_dict(d) == p


def test_missing_field_rejected():
    d = _sample().to_dict()
    del d["config_sha256"]
    with pytest.raises(ValueError, match="missing required"):
        Provenance.from_dict(d)


def test_matching_provenances_have_no_drift():
    p1 = _sample()
    p2 = _sample()
    assert p1.verify_against(p2) == []


def test_drift_detected_per_field():
    baseline = _sample(image="aa" * 32)
    current = _sample(image="ff" * 32)
    drifts = current.verify_against(baseline)
    assert len(drifts) == 1
    assert "image_digest" in drifts[0]


def test_commit_field_not_treated_as_drift():
    """harness_commit changes routinely; only the 4 measurement-relevant
    fields constitute drift."""
    baseline = _sample(commit="aaaaaaaa")
    current = _sample(commit="bbbbbbbb")
    assert current.verify_against(baseline) == []


def test_canonical_json_hash_stable():
    a = {"x": 1, "y": [2, 3]}
    b = {"y": [2, 3], "x": 1}  # different insertion order
    assert sha256_of_json(a) == sha256_of_json(b)


def test_sha256_of_bytes_deterministic():
    assert sha256_of_bytes(b"hello") == sha256_of_bytes(b"hello")
    assert sha256_of_bytes(b"hello") != sha256_of_bytes(b"world")
