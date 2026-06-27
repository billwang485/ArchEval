#!/usr/bin/env python3
"""Anonymize trace file names in a directory using trace_mapping.json.

Usage (called during Docker build):
    python3 rename_at_build.py /work/workload_pools/champsim

Also anonymizes decoded trace text files in /decoded/ subdirectory.

Layout: this script and `trace_mapping.json` are siblings under
`simulators/champsim/anonymization/` — the script discovers the mapping
via `Path(__file__).parent`. Renamed from the legacy `anonymize_traces.py`
when the anonymization helpers were grouped together.
"""
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
MAPPING_FILE = SCRIPT_DIR / "trace_mapping.json"


def main():
    trace_dir = Path(sys.argv[1])
    mapping = json.loads(MAPPING_FILE.read_text())

    renamed = 0
    for orig_name, anon_name in mapping.items():
        # Rename binary trace
        orig_path = trace_dir / orig_name
        anon_path = trace_dir / anon_name
        if orig_path.exists():
            orig_path.rename(anon_path)
            renamed += 1

        # Rename decoded trace (if exists)
        orig_decoded = trace_dir / "decoded" / orig_name.replace(".champsimtrace.xz", ".trace.txt")
        anon_decoded = trace_dir / "decoded" / anon_name.replace(".champsimtrace.xz", ".trace.txt")
        if orig_decoded.exists():
            orig_decoded.rename(anon_decoded)

    print(f"Anonymized {renamed} traces")


if __name__ == "__main__":
    main()
