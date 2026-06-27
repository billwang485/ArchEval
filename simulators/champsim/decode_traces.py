#!/usr/bin/env python3
"""Decode ChampSim binary trace files into human-readable text.

ChampSim traces are compressed binary files containing per-instruction records.
This script decompresses and decodes them into a text format that an LLM agent
can read and analyze to understand workload characteristics.

Trace record format (ChampSim input_instr, 64 bytes little-endian):
    uint64_t ip                              instruction pointer
    uint8_t  is_branch                       1 if branch instruction
    uint8_t  branch_taken                    1 if branch was taken
    uint8_t  destination_registers[2]        destination register IDs
    uint8_t  source_registers[4]             source register IDs
    uint64_t destination_memory[2]           destination memory addresses
    uint64_t source_memory[4]               source memory addresses

Usage (standalone):
    python3 decode_traces.py /path/to/traces/ /path/to/output/ [--max-records N]

Usage (Docker build):
    COPY simulators/champsim/decode_traces.py /tmp/decode_traces.py
    RUN python3 /tmp/decode_traces.py /work/workload_pools/champsim /work/workload_pools/champsim/decoded
"""

from __future__ import annotations

import argparse
import lzma
import os
import struct
import sys
from pathlib import Path

# ChampSim input_instr: ip(Q) is_branch(B) branch_taken(B) dest_reg(2B) src_reg(4B) dest_mem(2Q) src_mem(4Q)
RECORD_FORMAT = struct.Struct("<QBB2B4B2Q4Q")
RECORD_SIZE = RECORD_FORMAT.size  # 64 bytes

# Default: decode first 200K records (~warmup + simulation for most challenges)
DEFAULT_MAX_RECORDS = 200_000


def decode_trace(
    input_path: Path,
    output_path: Path,
    max_records: int = DEFAULT_MAX_RECORDS,
) -> int:
    """Decode a ChampSim binary trace into a text file.

    Args:
        input_path: Path to .champsimtrace.xz file.
        output_path: Path for decoded .txt output.
        max_records: Maximum number of instruction records to decode.

    Returns:
        Number of records decoded.
    """
    # Open compressed trace
    opener = lzma.open if input_path.suffix == ".xz" else open
    open_kwargs = {"mode": "rb"}

    count = 0
    with opener(input_path, **open_kwargs) as fin, open(output_path, "w") as fout:
        # Write header
        fout.write(
            "# ChampSim instruction trace (decoded from binary)\n"
            f"# Source: {input_path.name}\n"
            f"# Max records: {max_records}\n"
            "#\n"
            "# Fields:\n"
            "#   idx          - instruction index (0-based)\n"
            "#   ip           - instruction pointer (hex)\n"
            "#   is_branch    - 1 if branch instruction, 0 otherwise\n"
            "#   branch_taken - 1 if branch taken, 0 if not taken (only meaningful when is_branch=1)\n"
            "#   dst_regs     - destination register IDs (comma-separated)\n"
            "#   src_regs     - source register IDs (comma-separated)\n"
            "#   dst_mem      - destination memory addresses (hex, comma-separated, 0x0 = unused)\n"
            "#   src_mem      - source memory addresses (hex, comma-separated, 0x0 = unused)\n"
            "#\n"
            "# idx\tip\tis_branch\tbranch_taken\tdst_regs\tsrc_regs\tdst_mem\tsrc_mem\n"
        )

        buf = fin.read(RECORD_SIZE * min(max_records, 10000))
        offset = 0

        while buf and count < max_records:
            # Process buffered data
            while offset + RECORD_SIZE <= len(buf) and count < max_records:
                fields = RECORD_FORMAT.unpack_from(buf, offset)
                offset += RECORD_SIZE

                ip = fields[0]
                is_branch = fields[1]
                branch_taken = fields[2]
                dst_regs = fields[3:5]
                src_regs = fields[5:9]
                dst_mem = fields[9:11]
                src_mem = fields[11:15]

                fout.write(
                    f"{count}\t"
                    f"0x{ip:x}\t"
                    f"{is_branch}\t"
                    f"{branch_taken}\t"
                    f"{dst_regs[0]},{dst_regs[1]}\t"
                    f"{src_regs[0]},{src_regs[1]},{src_regs[2]},{src_regs[3]}\t"
                    f"0x{dst_mem[0]:x},0x{dst_mem[1]:x}\t"
                    f"0x{src_mem[0]:x},0x{src_mem[1]:x},0x{src_mem[2]:x},0x{src_mem[3]:x}\n"
                )
                count += 1

            # Read next chunk
            remaining = buf[offset:]
            next_read = RECORD_SIZE * min(max_records - count, 10000)
            if next_read <= 0:
                break
            buf = remaining + fin.read(next_read)
            offset = 0

    return count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Decode ChampSim binary traces into human-readable text."
    )
    parser.add_argument(
        "input_dir",
        type=Path,
        help="Directory containing .champsimtrace.xz files",
    )
    parser.add_argument(
        "output_dir",
        type=Path,
        help="Output directory for decoded .txt files",
    )
    parser.add_argument(
        "--max-records",
        type=int,
        default=DEFAULT_MAX_RECORDS,
        help=f"Max records per trace (default: {DEFAULT_MAX_RECORDS})",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    traces = sorted(args.input_dir.glob("*.champsimtrace.xz"))
    if not traces:
        print(f"No .champsimtrace.xz files found in {args.input_dir}")
        sys.exit(1)

    for trace_path in traces:
        # Output name: 600.perlbench_s-210B.champsimtrace.xz -> 600.perlbench_s-210B.trace.txt
        stem = trace_path.name.replace(".champsimtrace.xz", "")
        output_path = args.output_dir / f"{stem}.trace.txt"

        print(f"Decoding {trace_path.name} -> {output_path.name} ...", end=" ", flush=True)
        n = decode_trace(trace_path, output_path, args.max_records)
        size_mb = output_path.stat().st_size / (1024 * 1024)
        print(f"{n:,} records, {size_mb:.1f} MB")


if __name__ == "__main__":
    main()
