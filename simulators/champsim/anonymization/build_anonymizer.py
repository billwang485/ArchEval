"""Helper: build an Anonymizer forward map from ChampSim's trace_mapping.json.

Used by the CLI / runner when `--anonymize` is on. Keeps the SPEC name
fan-out logic (base name, .champsimtrace.xz, .trace.txt) in one place
so the connector never sees the raw map.

Layout (post-reorg):
    simulators/champsim/anonymization/build_anonymizer.py   ← this file
    simulators/champsim/anonymization/trace_mapping.json    ← sibling data
    simulators/champsim/anonymization/rename_at_build.py    ← Dockerfile-time helper
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from archbench.core.anonymizer import Anonymizer


def load_champsim_anonymizer(
    mapping_path: Optional[Path] = None,
) -> Anonymizer:
    """Load the ChampSim trace_mapping.json into an Anonymizer.

    The mapping file lives under
    `simulators/champsim/anonymization/trace_mapping.json` (next to this
    module — per-image colocation); pass an explicit path to override.

    Expands each entry into three pairs so all three filename shapes
    (binary trace, decoded trace, base name) get scrubbed:

        "482.sphinx3-1100B.champsimtrace.xz" → "trace_xxxxxx.champsimtrace.xz"
        "482.sphinx3-1100B.trace.txt"        → "trace_xxxxxx.trace.txt"
        "482.sphinx3-1100B"                  → "trace_xxxxxx"
    """
    if mapping_path is None:
        mapping_path = Path(__file__).resolve().parent / "trace_mapping.json"
    if not mapping_path.exists():
        raise FileNotFoundError(
            f"ChampSim trace_mapping.json not found at {mapping_path}. "
            "Anonymizer cannot start with --anonymize."
        )
    with open(mapping_path) as f:
        raw: dict[str, str] = json.load(f)

    forward: dict[str, str] = {}
    for orig, anon in raw.items():
        # Full binary trace name
        forward[orig] = anon
        # Base name (no extension)
        orig_base = orig.replace(".champsimtrace.xz", "")
        anon_base = anon.replace(".champsimtrace.xz", "")
        forward[orig_base] = anon_base
        # Decoded trace name
        forward[orig.replace(".champsimtrace.xz", ".trace.txt")] = (
            anon.replace(".champsimtrace.xz", ".trace.txt")
        )

    return Anonymizer(forward=forward)
