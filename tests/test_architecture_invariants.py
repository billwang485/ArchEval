"""Mechanical enforcement of the architecture invariants — belt-and-suspenders (belt + suspenders).

The rules live in CLAUDE.md for humans. But this repo is meant to be edited by
Agents (Claude Code and friends), which do NOT reliably follow documented-only
conventions — they refactor without re-reading the rulebook. So every
load-bearing invariant is ALSO gated here: a violation fails the test suite
loudly, so a weak agent can't silently mangle the structure.

Each test below mirrors a documented rule:
  1. Layering        — archbench/core is the base; must not import the top-level
                       simulators/ or challenges/ packages (CLAUDE.md §1.1).
  2. Tier config     — no illegal agent_image_mode × starter_visibility combo
                       (CLAUDE.md §1.17; also enforced at load in challenge.py).
  3. Rename          — the abandoned project shorthand must not reappear in
                       tracked source (only results/ may contain it).
  4. Challenges load — every challenge.yaml loads + assembles a prompt.
"""
import ast
import glob
import os
import re
import subprocess
from pathlib import Path

from archbench.core.challenge import load_challenge

REPO = Path(__file__).resolve().parents[1]


# --- 1. layering: core is the base, must not depend UP on sims/challenges -----

def test_core_does_not_import_sims_or_challenges():
    offenders = []
    for f in glob.glob(str(REPO / "archbench" / "core" / "**" / "*.py"), recursive=True):
        tree = ast.parse(Path(f).read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                if n.split(".")[0] in ("simulators", "challenges"):
                    offenders.append(
                        f"{os.path.relpath(f, REPO)}:{getattr(node, 'lineno', '?')} -> {n}")
    assert not offenders, (
        "archbench/core/ must not import top-level simulators/ or challenges/ "
        "(layering — core is the base; plugins are passed in, never imported):\n  "
        + "\n  ".join(offenders)
    )


# --- 2. tier config: simulator_centric REQUIRES starter_visibility:none -------

def test_no_illegal_tier_config():
    offenders = []
    for y in glob.glob(str(REPO / "challenges" / "**" / "challenge.yaml"), recursive=True):
        d = os.path.dirname(y)
        ch = load_challenge(d)  # challenge.py raises on illegal combos; this gates the corpus
        mode = getattr(ch, "agent_image_mode", "agent_centric")
        vis = getattr(ch, "starter_visibility", "full")
        if mode == "simulator_centric" and vis != "none":
            offenders.append(f"{os.path.relpath(d, REPO)}: {mode} + {vis}")
    assert not offenders, (
        "illegal tier configs (simulator_centric must be starter_visibility:none):\n  "
        + "\n  ".join(offenders)
    )


# --- 3. the abandoned shorthand must not creep back into tracked source -------

def test_abandoned_shorthand_absent_from_tracked_source():
    # Assemble the token so THIS enforcer file is not its own false positive.
    token = "m" "a" "b"
    rx = re.compile(r"(?<![a-z])" + token + r"(?![a-z])", re.IGNORECASE)
    tracked = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True
    ).stdout.splitlines()
    offenders = []
    for rel in tracked:
        if rel.startswith("results/"):
            continue  # results/ is the documented exception (historical numbers)
        if rel.startswith("third_party/"):
            continue  # vendored upstream (ShinkaEvolve) — not ours to rename
        try:
            text = (REPO / rel).read_text()
        except Exception:
            continue  # binary / unreadable
        for i, line in enumerate(text.splitlines(), 1):
            if "streamable" in line.lower():
                continue  # FastMCP word, not the shorthand
            if rx.search(line):
                offenders.append(f"{rel}:{i}: {line.strip()[:80]}")
    assert not offenders, (
        "the abandoned project shorthand reappeared in tracked source — the rename "
        "onto 'archbench' must stay complete (only results/ may keep it):\n  "
        + "\n  ".join(offenders[:25])
    )


# --- 4. every challenge.yaml loads + assembles a prompt ----------------------

def test_all_challenges_load_and_assemble_prompt():
    failures = []
    dirs = sorted({os.path.dirname(y) for y in glob.glob(
        str(REPO / "challenges" / "**" / "challenge.yaml"), recursive=True)})
    for d in dirs:
        try:
            ch = load_challenge(d)
            assert ch.prompt, "empty prompt"
        except Exception as e:  # noqa: BLE001 — we want to report ALL failures
            failures.append(f"{os.path.relpath(d, REPO)}: {type(e).__name__}: {e}")
    assert not failures, "challenges that fail to load:\n  " + "\n  ".join(failures)


def test_assisted_tiers_are_self_contained():
    """Every assisted tier yaml (L1/L2) MUST be SELF-CONTAINED (CLAUDE.md §1.3):
    no `extends:` cross-yaml dependency, and it carries — on its own — the fields
    it used to inherit (simulator + the task_prompt). L1/L2/L3 read top-to-bottom
    independently; only ARTIFACTS (one simulator/, one evaluation/baseline.json)
    are shared, by the assisted/<L>/ convention (comparability invariant §1.7).

    This is the mechanical guard against regressing to the old `extends:` overlay
    form (which was unreadable standalone — you had to mentally merge with L3)."""
    import yaml
    bad = []
    for y in sorted(glob.glob(str(REPO / "challenges" / "*" / "assisted" / "*" / "challenge.yaml"))):
        rel = os.path.relpath(y, REPO)
        data = yaml.safe_load(open(y)) or {}
        if "extends" in data:
            bad.append(f"{rel}: has `extends:` — tiers must be self-contained, not overlays")
        if not (data.get("task_prompt") or data.get("task") or data.get("prompt")):
            bad.append(f"{rel}: no task_prompt — not self-contained (was it inheriting from L3?)")
        if not data.get("simulator"):
            bad.append(f"{rel}: no simulator — not self-contained")
    assert not bad, "assisted tiers must be self-contained (no extends):\n  " + "\n  ".join(bad)


# --- 5. the 4-concept model is visible IN the skeleton (ARCHITECTURE.md) ------

def test_core_modules_declare_their_concept():
    """Each core module's docstring must start with a [concept: ...] tag, so the
    4-concept model (ORCHESTRATION / VERIFY / MONITOR / EVALUATE) is readable from
    the code itself, not just ARCHITECTURE.md. An untagged core module fails here
    — the structural reflection can't silently rot."""
    expected = {
        "archbench/runtimes/session.py": "ORCHESTRATION",
        "archbench/core/run_spec.py": "ORCHESTRATION",
        "archbench/core/provenance.py": "VERIFY",
        "archbench/core/container_card.py": "VERIFY",
        "archbench/core/doctor.py": "VERIFY",
        "archbench/core/trajectory.py": "MONITOR",
        "archbench/evaluators/base.py": "EVALUATE",
    }
    bad = []
    for rel, concept in expected.items():
        head = (REPO / rel).read_text()[:200]
        if f"[concept: {concept}" not in head:
            bad.append(f"{rel}: missing [concept: {concept}] tag")
    assert not bad, ("core modules must declare their concept (see ARCHITECTURE.md):\n  "
                     + "\n  ".join(bad))


def test_repo_root_resolution_for_assisted_tiers():
    """parents[1] bug regression (wave-1 2026-06-09): the provenance drift
    guard must find workload_pools/ from BOTH a family root and an assisted
    tier dir. challenges/<fam>/assisted/<L> has the repo root 3 levels up,
    not 2 — the naive parents[1] falsely refused every assisted champsim run."""
    from archbench.core.provenance import repo_root_from_challenge_dir
    root = repo_root_from_challenge_dir(REPO / "challenges" / "branch_predictor")
    tier = repo_root_from_challenge_dir(
        REPO / "challenges" / "branch_predictor" / "assisted" / "L1")
    assert root == REPO and tier == REPO, (root, tier)


def test_no_timestamp_keyed_tmp_paths():
    """Host-side per-run artifacts MUST derive from run_id/uuid, never from a
    bare timestamp (lessons §26.1: second-resolution /tmp trajectory names
    collided across concurrently launched cells and corrupted 13 cells'
    trajectory-derived artifacts). Flag any tracked .py building a /tmp path
    from time.time() without a uuid/run_id/mkdtemp in the same expression."""
    import subprocess
    tracked = subprocess.run(["git", "-C", str(REPO), "ls-files", "*.py"],
                             capture_output=True, text=True).stdout.splitlines()
    offenders = []
    for rel in tracked:
        if rel.startswith(("tests/", "results/")):
            continue
        try:
            text = (REPO / rel).read_text()
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if "/tmp/" in line and "time.time()" in line \
                    and not any(k in line for k in ("uuid", "run_id", "mkdtemp", "getpid")):
                offenders.append(f"{rel}:{i}: {line.strip()[:90]}")
    assert not offenders, (
        "timestamp-keyed /tmp paths (collision class, lessons §26.1) — derive "
        "from run_id/uuid instead:\n  " + "\n  ".join(offenders))


def test_no_bind_mount_shadows_image_root():
    """Sim images bake their tree at /work (components/, workloads/, verify.sh,
    ...). Bind-mounting a host dir AT /work shadows the whole baked tree
    (rename-class regression: timeloop_dosa + cnn_accelerator kept mounting
    `-v $WORK:/work` after the image root moved to /work, so timeloop-model's
    accelergy step crashed on the now-empty /work/components glob and both
    baselines regenerated null). Mounts must target a SUB-path of /work
    (house convention: /work/submission — see mnsim/gibbon/scalesim)."""
    pat = re.compile(r"-v\s+\S+:/work(?![/\w])")
    offenders = []
    for pattern in ("challenges/**/*.sh", "simulators/**/*.sh"):
        for f in glob.glob(str(REPO / pattern), recursive=True):
            try:
                lines = Path(f).read_text(errors="replace").splitlines()
            except Exception:
                continue
            for i, line in enumerate(lines, 1):
                if pat.search(line):
                    offenders.append(
                        f"{os.path.relpath(f, REPO)}:{i}: {line.strip()[:100]}")
    assert not offenders, (
        "bind mount targets the image root /work — this shadows the baked "
        "/work tree (components/, workloads/). Mount a sub-path "
        "(e.g. :/work/submission) instead:\n  " + "\n  ".join(offenders))


def test_runner_container_env_contract():
    """Every ARCHBENCH_* env the mini runner injects into the container must be
    READ by /opt/mini/main.py (rename-class regression: the runner passed
    ARCHBENCH_TEMPERATURE but main.py read the pre-rename ARCHEVAL_TEMPERATURE,
    silently forcing greedy decoding on every run for 3 days)."""
    runner = (REPO / "runtimes/mini/runner.py").read_text()
    main = (REPO / "runtimes/mini/src/main.py").read_text()
    injected = set(re.findall(r'-e", f?"(ARCHBENCH_[A-Z_]+)=', runner))
    missing = [v for v in injected if v not in main]
    assert not missing, (
        f"runner injects {missing} but /opt/mini/main.py never reads them "
        "(cross-container env contract; see the temperature incident)")
