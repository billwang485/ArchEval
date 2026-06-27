#!/bin/bash
# Download DPC3 official traces (SPEC CPU 2017 memory-intensive subset)

BASE_URL="https://dpc3.compas.cs.stonybrook.edu/champsim-traces/speccpu"
OUT_DIR="$(dirname "$0")/dpc3_traces"
mkdir -p "$OUT_DIR"

# DPC3 used SPEC CPU 2017 traces with LLC MPKI >= 1.0
# These are the 46 memory-intensive traces from the competition
TRACES=(
    # 600.perlbench_s
    "600.perlbench_s-210B.champsimtrace.xz"
    "600.perlbench_s-570B.champsimtrace.xz"
    "600.perlbench_s-1273B.champsimtrace.xz"
    # 602.gcc_s
    "602.gcc_s-734B.champsimtrace.xz"
    "602.gcc_s-1850B.champsimtrace.xz"
    "602.gcc_s-2226B.champsimtrace.xz"
    # 603.bwaves_s (streaming)
    "603.bwaves_s-891B.champsimtrace.xz"
    "603.bwaves_s-1740B.champsimtrace.xz"
    "603.bwaves_s-2609B.champsimtrace.xz"
    "603.bwaves_s-2931B.champsimtrace.xz"
    # 605.mcf_s (pointer-chasing, memory-intensive)
    "605.mcf_s-472B.champsimtrace.xz"
    "605.mcf_s-484B.champsimtrace.xz"
    "605.mcf_s-665B.champsimtrace.xz"
    "605.mcf_s-782B.champsimtrace.xz"
    "605.mcf_s-994B.champsimtrace.xz"
    "605.mcf_s-1152B.champsimtrace.xz"
    "605.mcf_s-1536B.champsimtrace.xz"
    "605.mcf_s-1554B.champsimtrace.xz"
    "605.mcf_s-1644B.champsimtrace.xz"
    # 607.cactuBSSN_s
    "607.cactuBSSN_s-2421B.champsimtrace.xz"
    "607.cactuBSSN_s-3477B.champsimtrace.xz"
    "607.cactuBSSN_s-4004B.champsimtrace.xz"
    # 619.lbm_s (streaming)
    "619.lbm_s-2676B.champsimtrace.xz"
    "619.lbm_s-2677B.champsimtrace.xz"
    "619.lbm_s-3766B.champsimtrace.xz"
    "619.lbm_s-4268B.champsimtrace.xz"
    # 620.omnetpp_s
    "620.omnetpp_s-141B.champsimtrace.xz"
    "620.omnetpp_s-874B.champsimtrace.xz"
    # 621.wrf_s
    "621.wrf_s-6673B.champsimtrace.xz"
    "621.wrf_s-8065B.champsimtrace.xz"
    # 623.xalancbmk_s (XML parsing, pointer-intensive)
    "623.xalancbmk_s-10B.champsimtrace.xz"
    "623.xalancbmk_s-165B.champsimtrace.xz"
    "623.xalancbmk_s-325B.champsimtrace.xz"
    "623.xalancbmk_s-700B.champsimtrace.xz"
    # 625.x264_s
    "625.x264_s-18B.champsimtrace.xz"
    "625.x264_s-33B.champsimtrace.xz"
    # 627.cam4_s
    "627.cam4_s-490B.champsimtrace.xz"
    # 628.pop2_s
    "628.pop2_s-17B.champsimtrace.xz"
    # 631.deepsjeng_s
    "631.deepsjeng_s-928B.champsimtrace.xz"
    # 638.imagick_s
    "638.imagick_s-10316B.champsimtrace.xz"
    "638.imagick_s-824B.champsimtrace.xz"
    # 641.leela_s
    "641.leela_s-800B.champsimtrace.xz"
    "641.leela_s-862B.champsimtrace.xz"
    # 644.nab_s
    "644.nab_s-5853B.champsimtrace.xz"
    # 649.fotonik3d_s
    "649.fotonik3d_s-1176B.champsimtrace.xz"
    "649.fotonik3d_s-7084B.champsimtrace.xz"
    "649.fotonik3d_s-8225B.champsimtrace.xz"
    # 654.roms_s
    "654.roms_s-1007B.champsimtrace.xz"
    "654.roms_s-1070B.champsimtrace.xz"
    "654.roms_s-1390B.champsimtrace.xz"
)

echo "Downloading ${#TRACES[@]} DPC3 traces..."
for trace in "${TRACES[@]}"; do
    if [ -f "$OUT_DIR/$trace" ]; then
        echo "  [skip] $trace (exists)"
    else
        echo "  [download] $trace"
        wget -q "$BASE_URL/$trace" -O "$OUT_DIR/$trace"
    fi
done

echo "Done. $(ls "$OUT_DIR"/*.xz 2>/dev/null | wc -l) traces in $OUT_DIR"
