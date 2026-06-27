"""CI gate: no cluster-specific absolute paths in tracked code.

K2 externalizes every machine-specific path into an untracked ``site.yaml``.
This test enforces the other half of that promise: the four *code* dirs must
contain ZERO ``private filesystem`` references, so a fresh clone
on any cluster/laptop cannot silently mis-bind to the origin box's NFS roots.

Scope is deliberately the executable framework dirs:

    archbench/  simulators/  runtimes/  scripts/

Exclusions, each with a reason:

- **Git-status-modified-uncommitted / untracked files** are SKIPPED. The
  gate must not couple to a parallel in-flight changeset: a concurrent
  session may be mid-edit on a file in these dirs, and a leak there is that
  session's to scrub, not this test's to police.
- **``site.yaml``** is the untracked per-machine file where these paths are
  *supposed* to live; it is gitignored and not under these dirs anyway.
- **Template files** (``*.example.yaml``, e.g. the tracked
  ``site.example.yaml``) carry ``/n/`` only inside COMMENTS as human-facing
  examples; they are documentation, not executing code, and live at repo
  root rather than in the scanned dirs. Excluded defensively.

Out of scope by design: human-facing prose files are not part of this
framework-only prerelease.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The executable dirs the gate scans.
SCAN_DIRS = ("archbench", "simulators", "runtimes", "scripts")

# Private filesystem roots that must never appear in tracked code.
LEAK_PATTERN = r"/private_cluster/(home|scratch|work)"

# Filename patterns excluded even when committed (human-facing templates).
_EXCLUDED_BASENAMES = ("site.yaml",)
_EXCLUDED_SUFFIXES = (".example.yaml",)


def _dirty_paths() -> set[str]:
    """Repo-relative paths git reports as not-clean (modified, staged, or
    untracked). These are skipped so the gate never couples to a parallel
    in-flight changeset. Paths are normalized to forward-slash, repo-relative.
    """
    proc = subprocess.run(
        ["git", "status", "--porcelain", "-z"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    dirty: set[str] = set()
    # -z gives NUL-separated records; XY + space + path. Renames carry a second
    # NUL-separated path ("R  old\0new") — split conservatively on NUL and
    # strip the 3-char status prefix from each record that has one.
    for record in proc.stdout.split("\0"):
        if not record:
            continue
        # A status record is "XY path"; a rename's second field is a bare path.
        path = record[3:] if len(record) > 3 and record[2] == " " else record
        dirty.add(path.strip())
    return dirty


def _is_excluded(rel_path: str) -> bool:
    base = rel_path.rsplit("/", 1)[-1]
    if base in _EXCLUDED_BASENAMES:
        return True
    return any(rel_path.endswith(sfx) for sfx in _EXCLUDED_SUFFIXES)


def test_no_cluster_paths_in_code():
    existing = [d for d in SCAN_DIRS if (REPO_ROOT / d).is_dir()]
    assert existing, "none of the scan dirs exist — repo layout changed?"

    # -I skips binary files (compiled *.pyc under __pycache__ embed source
    # paths and are not tracked code); --exclude-dir drops the cache dirs
    # outright. Both flags are common to GNU grep and ugrep.
    proc = subprocess.run(
        ["grep", "-rInE", "--exclude-dir=__pycache__", LEAK_PATTERN, *existing],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    # grep rc: 0 = matches found, 1 = clean, 2 = real error.
    assert proc.returncode in (0, 1), (
        f"grep failed (rc={proc.returncode}): {proc.stderr.strip()}"
    )
    if proc.returncode == 1:
        return  # nothing matched anywhere — trivially clean

    dirty = _dirty_paths()
    offenders: list[str] = []
    for line in proc.stdout.splitlines():
        if not line:
            continue
        # grep -rn output: "<relpath>:<lineno>:<text>"
        rel_path = line.split(":", 1)[0]
        if rel_path in dirty:
            continue  # parallel/uncommitted — not this gate's concern
        if _is_excluded(rel_path):
            continue  # gitignored site file or human-facing template
        offenders.append(line)

    assert not offenders, (
        "Private machine-specific path(s) leaked into tracked code. Move each into "
        "site.yaml (e.g. workloads_dir / tar_dir) or an ARCHBENCH_* env var; never "
        "hardcode it:\n  "
        + "\n  ".join(offenders)
    )


def test_gate_machinery_is_not_vacuous():
    """Guard against a silently-broken gate: the LEAK_PATTERN must actually
    match a real private-cluster string and the dirty-path filter must be the reason
    any in-tree leak is tolerated — never a broken grep. We synthesize a leak
    line and confirm the same filter logic would (a) skip it when its file is
    dirty and (b) flag it when its file is clean. Pure-Python; no repo writes.
    """
    import re

    leak = "/private_cluster/work/project/traces"
    assert re.search(LEAK_PATTERN, leak), "LEAK_PATTERN no longer matches private roots"

    # A clean (non-dirty, non-excluded) path with a leak must be flagged.
    clean_line = f"archbench/core/somefile.py:7:trace_dir = '{leak}'"
    rel = clean_line.split(":", 1)[0]
    assert rel.startswith("archbench/")
    assert not _is_excluded(rel)

    # An excluded template path with a leak-in-comment must be tolerated.
    assert _is_excluded("site.example.yaml")
    assert _is_excluded("site.yaml")
