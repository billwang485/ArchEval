"""[concept: VERIFY — see ARCHITECTURE.md]

Container card — a per-image contract of what's inside, for humans + machines.

A card is a plain YAML file, VERSION-CONTROLLED in the repo at
``image_cards/<image-slug>.card.yaml`` (a contract, like a challenge — NOT next
to the gitignored tars in docker/, and never baked into the image). A human
reads it to SEE what the image looks like; the harness verifies the LIVE image
against it on every load, so the declaration can't drift (a stale/wrong image
fails AT LOAD, naming what's wrong) and it travels across sessions/machines.

What the card captures
----------------------
HARD checks (verify FAILS on mismatch):
  expect.top_level          the ``ls /`` listing — an ALLOWLIST: nothing else may
                            exist at / (catches surprise dirs like /archeval).
  expect.paths_present      key paths that MUST exist.
  expect.paths_absent       paths that MUST NOT exist.
  expect.file_sha256        load-bearing files pinned to an exact content hash.
  expect.env_absent_tokens  tokens that must not appear in the env.
Identity:
  fingerprint               the image's content fingerprint (sha256, from
                            ``docker image inspect``) at stamp time — ``card
                            verify`` checks the live image's fingerprint against
                            it ("is this the exact image I built?").
Informational (shown to humans, not enforced — contents legitimately vary):
  snapshot.<dir>            an ``ls`` of a few key dirs so you SEE the layout.

Pure helpers (_build_check_script / _parse_check_output / _parse_top_level) are
separated from the container call so the logic is unit-testable without docker.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def card_path_for(image: str) -> Path:
    """A card is a CONTRACT, version-controlled in the repo (like challenges/) —
    NOT next to the gitignored image tars in docker/. So it travels with the
    code across sessions/machines, acts as a lock that catches a drifted rebuild,
    and is never baked into the image (the agent can't see it)."""
    slug = image.split("/")[-1].replace(":", "-")
    return REPO_ROOT / "image_cards" / f"{slug}.card.yaml"


def load_card(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text()) or {}
    return data if isinstance(data, dict) else None


def write_card(card: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(card, sort_keys=False))


# --- pure logic (unit-testable without a container) --------------------------

_TOPLEVEL_MARK = "TOPLEVEL::"


def _build_check_script(expect: dict) -> str:
    """One /bin/sh script that emits the actual top-level + one 'OK/FAIL' line
    per hard check."""
    lines = ["set -u", f'echo "{_TOPLEVEL_MARK}$(ls -1 / 2>/dev/null | tr "\\n" " ")"']
    for p in expect.get("paths_present", []) or []:
        lines.append(f'[ -e "{p}" ] && echo "OK present {p}" || echo "FAIL present {p}"')
    for p in expect.get("paths_absent", []) or []:
        lines.append(f'[ -e "{p}" ] && echo "FAIL absent {p}" || echo "OK absent {p}"')
    for path, sha in (expect.get("file_sha256", {}) or {}).items():
        lines.append(
            f'g=$(sha256sum "{path}" 2>/dev/null | cut -d" " -f1); '
            f'[ "$g" = "{sha}" ] && echo "OK sha {path}" || echo "FAIL sha {path} got=$g"'
        )
    toks = expect.get("env_absent_tokens", []) or []
    if toks:
        pat = "|".join(re.escape(t) for t in toks)
        lines.append(f'env | grep -iE "{pat}" >/dev/null && echo "FAIL env leak" || echo "OK env"')
    return "\n".join(lines)


def _parse_check_output(output: str) -> list[str]:
    return [ln.strip()[len("FAIL "):] for ln in output.splitlines()
            if ln.strip().startswith("FAIL ")]


def _parse_top_level(output: str) -> Optional[list[str]]:
    for ln in output.splitlines():
        if ln.strip().startswith(_TOPLEVEL_MARK):
            rest = ln.strip()[len(_TOPLEVEL_MARK):].strip()
            return sorted(x for x in rest.split() if x)
    return None


def _toplevel_violations(declared: list[str], actual: Optional[list[str]]) -> list[str]:
    """ALLOWLIST: actual must equal declared (no extra at /, none missing)."""
    if not declared or actual is None:
        return []
    d, a = set(declared), set(actual)
    out = [f"top-level UNEXPECTED at / : {x}" for x in sorted(a - d)]
    out += [f"top-level MISSING at / : {x}" for x in sorted(d - a)]
    return out


# --- container-touching ops --------------------------------------------------

def verify_against_image(card: dict, image: str, engine: str) -> list[str]:
    """Run the card's hard checks inside `image`. Return violations (empty = match).
    Raises only on infra failure to run the check at all."""
    import subprocess
    expect = card.get("expect", {}) or {}
    script = _build_check_script(expect)
    res = subprocess.run([engine, "run", "--rm", image, "sh", "-c", script],
                         capture_output=True, text=True, timeout=120)
    out = res.stdout
    if not out.strip() and res.returncode != 0:
        raise RuntimeError(f"card verify could not run in {image}: {res.stderr.strip()[:300]}")
    fails = _parse_check_output(out)
    fails += _toplevel_violations(expect.get("top_level", []) or [], _parse_top_level(out))
    return fails


def verify_identity(card: dict, image: str) -> Optional[tuple[bool, str, str]]:
    """Is the live image the EXACT one stamped? Returns (match, live, stamped) or
    None if the card has no stamped digest. (live digest via container.py — lazy
    import to avoid a cycle.)"""
    stamped = card.get("fingerprint")
    if not stamped:
        return None
    from archbench.core.container import get_image_digest
    live = get_image_digest(image) or ""
    return (live == stamped, live, stamped)


def stamp_from_image(image: str, role: str, engine: str, *,
                     paths_present: list[str], paths_absent: list[str],
                     hash_files: list[str], env_absent_tokens: list[str],
                     snapshot_dirs: Optional[list[str]] = None,
                     commit: str = "", digest: str = "") -> dict:
    """Introspect a KNOWN-GOOD image and emit its card: the top-level allowlist,
    theload-bearing hashes, and an `ls` snapshot of key dirs (so a human sees the layout).
    Stamp once on a verified image; thereafter verify-on-load catches drift."""
    import subprocess

    def _run(cmd: str) -> str:
        return subprocess.run([engine, "run", "--rm", image, "sh", "-c", cmd],
                              capture_output=True, text=True, timeout=120).stdout

    # STAMP-TIME VERIFICATION (lessons §26): a card must never ASSERT a path
    # without checking it — gem5/timeloop cards were stamped with fictional
    # paths_present and self-blocked at the first verify-on-load. Refuse to
    # stamp if any expected path is absent from the live image.
    if paths_present:
        probe = "".join(f'test -e "{p}" || echo "ABSENT {p}"\n' for p in paths_present)
        absent = [ln.split(" ", 1)[1] for ln in _run(probe).splitlines() if ln.startswith("ABSENT ")]
        if absent:
            raise ValueError(
                f"refusing to stamp {image}: declared paths_present missing from the "
                f"live image: {absent} (fix the image or the sim's layout declaration)")

    top_level = sorted(x for x in _run("ls -1 / 2>/dev/null").split() if x)

    file_sha256: dict[str, str] = {}
    if hash_files:
        sha_cmd = "".join(
            f'echo -n "{f} "; sha256sum "{f}" 2>/dev/null | cut -d" " -f1 || echo MISSING\n'
            for f in hash_files)
        for ln in _run(sha_cmd).splitlines():
            parts = ln.split()
            if len(parts) == 2 and parts[1] and parts[1] != "MISSING":
                file_sha256[parts[0]] = parts[1]

    snapshot: dict[str, list[str]] = {}
    for d in (snapshot_dirs or []):
        entries = sorted(x for x in _run(f'ls -1 "{d}" 2>/dev/null | head -40').split() if x)
        if entries:
            snapshot[d] = entries

    return {
        "image": image,
        "role": role,
        "stamped_at_commit": commit,
        "fingerprint": digest,
        "expect": {
            "top_level": top_level,
            "paths_present": paths_present,
            "paths_absent": paths_absent,
            "file_sha256": file_sha256,
            "env_absent_tokens": env_absent_tokens,
        },
        "snapshot": snapshot,
    }


# --- human-readable rendering ------------------------------------------------

def _gloss(path: str) -> str:
    if "/runtimes/" in path and path.rstrip("/").endswith("bin"):
        return "   <- compiled simulator binary"
    if "/runtimes/" in path:
        return "   <- the simulator's source lives here"
    if path == "/opt/mini":
        return "   <- the baked agent loop"
    if path == "/workspace":
        return "   <- where the agent works"
    return ""


_ROLE_HUMAN = {
    "simulator": "a simulator image",
    "agent": "an agent sandbox image (no sim source)",
    "agent_sim": "an L2 image (simulator + baked agent loop)",
}


def render_pretty(card: dict) -> str:
    e = card.get("expect", {}) or {}
    out = [f"{card.get('image', '?')}",
           f"  type   : {card.get('role', '?')}  ({_ROLE_HUMAN.get(card.get('role'), '')})"]
    if card.get("stamped_at_commit") or card.get("fingerprint"):
        out.append(f"  stamped: commit {card.get('stamped_at_commit', '?')}, "
                   f"fingerprint {str(card.get('fingerprint', ''))[:12]}…")
    out.append("")
    tl = e.get("top_level", []) or []
    if tl:
        out.append(f"  top level (/)  — ONLY these may exist here:")
        out.append("    " + "  ".join(tl))
    pp = e.get("paths_present", []) or []
    if pp:
        out.append("  MUST have:")
        out += [f"    {p}{_gloss(p)}" for p in pp]
    pa = e.get("paths_absent", []) or []
    if pa:
        out.append("  MUST NOT have:")
        out += [f"    {p}" for p in pa]
    fs = e.get("file_sha256", {}) or {}
    if fs:
        out.append("  these files pinned byte-for-byte:")
        out += [f"    {p}  = {s[:12]}…" for p, s in fs.items()]
    et = e.get("env_absent_tokens", []) or []
    if et:
        out.append(f"  env must NOT contain: {', '.join(et)}")
    snap = card.get("snapshot", {}) or {}
    if snap:
        out.append("")
        out.append("  what's inside (snapshot at stamp time):")
        for d, entries in snap.items():
            shown = "  ".join(entries[:14])
            more = f"  …(+{len(entries) - 14})" if len(entries) > 14 else ""
            out.append(f"    {d}/")
            out.append(f"        {shown}{more}")
    out.append("")
    out.append("  -> verified against the LIVE image on every load.")
    out.append(f"     check now (incl. 'is this my build?'):  archbench card verify {card.get('image', '<image>')}")
    return "\n".join(out)


# Per-sim source-root EXCEPTIONS to the /work/runtimes/<sim> convention.
# gem5's image is binary-only (the 1.3GB gem5 binary + /work/workloads/gem5;
# no source tree was ever baked in) — discovered when the first verify-on-load
# of the base image tripped on the conventional path (wave-1 2026-06-10).
# When wave-2 rebuilds gem5 WITH its source tree, delete this entry and
# restamp (task #32).
def sim_source_root(sim: str) -> str:
    """The sim's container source-root, read from the sim's OWN declaration
    (simulators/<sim>/info.yaml::layout.source_root). One source of truth —
    the /work/runtimes/<sim> convention template was fiction for gem5 and
    timeloop and self-blocked their cards (lessons §26). Reads a DATA file,
    not the plugin package (layering §1.1 intact). Falls back to the
    convention for sims that have not declared a layout."""
    import yaml
    info = REPO_ROOT / "simulators" / sim / "info.yaml"
    try:
        d = yaml.safe_load(info.read_text()) or {}
        root = ((d.get("layout") or {}).get("source_root") or "").strip()
        if root:
            return root
    except FileNotFoundError:
        pass
    return f"/work/runtimes/{sim}"


# Sensible role defaults so `archbench card stamp` needs minimal flags.
def role_defaults(role: str, sim: Optional[str]) -> dict:
    neutral_absent = ["/archeval"]
    neutral_env = ["archbench", "archeval"]
    if role == "simulator" and sim:
        d = sim_source_root(sim)
        return {"paths_present": ["/work", d, "/workspace"], "paths_absent": neutral_absent,
                "hash_files": ["/work/build_and_run.sh", "/work/verify.sh"],
                "env_absent_tokens": neutral_env, "snapshot_dirs": ["/work", d]}
    if role == "agent_sim" and sim:  # l2agent
        d = sim_source_root(sim)
        return {"paths_present": ["/work", d, "/opt/mini", "/workspace"], "paths_absent": neutral_absent,
                "hash_files": [], "env_absent_tokens": neutral_env, "snapshot_dirs": ["/work", d, "/opt"]}
    if role == "agent":  # agent-mini (no sim source)
        return {"paths_present": ["/opt/mini", "/workspace"], "paths_absent": neutral_absent,
                "hash_files": [], "env_absent_tokens": neutral_env, "snapshot_dirs": ["/opt", "/workspace"]}
    return {"paths_present": [], "paths_absent": neutral_absent,
            "hash_files": [], "env_absent_tokens": neutral_env, "snapshot_dirs": []}
