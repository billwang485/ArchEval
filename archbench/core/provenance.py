"""[concept: VERIFY — see ARCHITECTURE.md]

Provenance — proof a baseline number is comparable to the agent's number.

PLAIN LANGUAGE (what "provenance" means here): a small fingerprint —
image_digest + config_sha256 + starter_sha256 + trace_sha256 + harness_commit —
stamped onto every ``baseline.json`` (and every per-submit result). Before a run
the harness re-computes it from the LIVE image / config / starter / trace and
refuses to start if it drifted from what the baseline was measured with. Why: an
"agent beat the baseline by X%" number only means something if the baseline and
the agent ran on the SAME image + config — otherwise the speedup is fake. The
word "provenance" is just the standard reproducibility term for "where a number
came from / its lineage"; read it as "measurement fingerprint".

Historical incident (2026-04-19, see legacy ARCHEVAL
`cache_replacement_baseline_fix/EVIDENCE.md`): the LRU baseline IPC =
0.6968 was measured against `localhost/archbench-champsim:latest`, then
copy-pasted into a new challenge directory. The `:latest` image was
overwritten; the only ChampSim image still loadable was `:v6`, which
actually produces IPC ≈ 0.5113 for the same starter code. Every
"agent beat LRU by X%" number reported for ~6 weeks was wrong-by-
construction (baseline was too high, so reported speedup was too low).

The structural fix: every `baseline.json` and every per-submit result
carries a `Provenance` record. The runner verifies the bundle on startup
and refuses to start if anything drifted.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Provenance:
    """Reproducibility metadata for a single measurement.

    All four sha fields are hex digests (64 chars). All are required —
    a measurement with a missing field is rejected by the runner.

    - `image_digest`: docker image sha (output of `docker image inspect`),
      not the human-readable tag. Tags can be reassigned silently; digests
      cannot.
    - `config_sha256`: sha256 of the simulator config.json that produced
      the measurement.
    - `starter_sha256`: sha256 of the agent's submitted code (or, for
      baseline, of the LRU starter that produced the baseline).
    - `trace_sha256`: sha256 of the workload trace file.
    - `harness_commit`: git rev-parse HEAD of this repo at measurement time.
    """

    image_digest: str
    config_sha256: str
    starter_sha256: str
    trace_sha256: str
    harness_commit: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Provenance":
        missing = [
            k for k in (
                "image_digest", "config_sha256",
                "starter_sha256", "trace_sha256", "harness_commit",
            ) if k not in d
        ]
        if missing:
            raise ValueError(f"Provenance missing required fields: {missing}")
        return cls(
            image_digest=d["image_digest"],
            config_sha256=d["config_sha256"],
            starter_sha256=d["starter_sha256"],
            trace_sha256=d["trace_sha256"],
            harness_commit=d["harness_commit"],
        )

    def verify_against(
        self, other: "Provenance", *, skip_starter: bool = False,
    ) -> list[str]:
        """Return drift descriptions; empty list = match.

        Used by the runner: if `baseline.json`'s provenance differs from
        what the runner observes now, the runner aborts rather than
        report comparisons against a stale baseline.

        Phase B (tiered challenges, 2026-05-31): pass
        ``skip_starter=True`` when the challenge's
        ``starter_visibility == 'none'`` — there is no scaffold shipped
        to the agent, so the starter sha is degenerate. A one-line
        warning is logged for audit. The other three sha fields still
        run, preserving §1.7.
        """
        if skip_starter:
            import warnings
            warnings.warn(
                "Provenance.verify_against: skipping starter_sha256 "
                "(starter_visibility='none'); other three sha fields "
                "still verified.",
                stacklevel=2,
            )
            fields = ("image_digest", "config_sha256", "trace_sha256")
        else:
            fields = (
                "image_digest", "config_sha256", "starter_sha256", "trace_sha256",
            )
        drifts = []
        for field_name in fields:
            mine = getattr(self, field_name)
            theirs = getattr(other, field_name)
            if mine != theirs:
                drifts.append(
                    f"{field_name}: baseline={theirs[:12]}… "
                    f"current={mine[:12]}…"
                )
        return drifts


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_of_json(obj) -> str:
    """sha256 of a JSON-serializable object, using canonical key ordering."""
    return sha256_of_bytes(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def docker_image_digest(image_tag: str) -> Optional[str]:
    """Return image's sha256 digest, or None if image is not local.

    Tries `docker image inspect` first, then `podman image inspect` as a
    fallback. On most cluster nodes `docker` is a podman alias, but that is
    not guaranteed — if the alias is absent (or `docker` is missing),
    inspecting via the real `podman` binary keeps this from silently
    returning ``None`` and breaking provenance stamping (design §5b.2).

    The runner uses this at start time to compare against
    `Provenance.image_digest`.
    """
    for binary in ("docker", "podman"):
        try:
            out = subprocess.check_output(
                [binary, "image", "inspect", "--format", "{{.Id}}", image_tag],
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).decode().strip()
            if out:
                return out.removeprefix("sha256:")
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            FileNotFoundError,
        ):
            continue  # try the next binary
    return None


def starter_dir_sha256(starter_dir: Path) -> str:
    """Combined sha over all files in a starter dir, in canonical order.

    CRITICAL: this formula is the single source of truth shared between
    baseline stamping (`stamp_baseline`) and the session-start drift
    guard (`archbench.runtimes.session._check_baseline_provenance`, L989-992).
    The two MUST byte-match or every freshly-stamped baseline immediately
    reads RED (lessons §1).

    The canonical form, fixed since the per-challenge stamp_provenance.py
    scripts, is::

        sha256( join over sorted(starter_dir.iterdir(), files only) of
                 f.name + ":" + sha256_of_file(f) + "\\n" )

    Do not "improve" the separator, ordering, or filter without updating
    `_check_baseline_provenance` in the SAME change.
    """
    return sha256_of_bytes(
        b"".join(
            f.name.encode() + b":" + sha256_of_file(f).encode() + b"\n"
            for f in sorted(starter_dir.iterdir()) if f.is_file()
        )
    )


def trace_files_sha256(trace_files: list[Path]) -> str:
    """Combined sha over an ordered list of resolved trace files.

    Mirrors the trace step of `_check_baseline_provenance` (session.py
    L1035-1038): the key is the file *basename* (so the digest is
    independent of which search dir the file resolved from — subtraces/
    vs workload_pools/), joined as ``name + ":" + sha256 + "\\n"``.

    Callers are responsible for passing the files in the same order the
    drift guard rebuilds them (per_trace order). Empty list → all-zero
    sentinel, signalling "no per-trace workload to hash" (the workload is
    baked into the image or is itself a starter file).
    """
    if not trace_files:
        return "0" * 64
    return sha256_of_bytes(
        b"".join(
            p.name.encode() + b":" + sha256_of_file(p).encode() + b"\n"
            for p in trace_files
        )
    )


def stamp_baseline(
    baseline_path: Path,
    *,
    image_tag: str,
    config_path: Optional[Path],
    starter_dir: Path,
    trace_files: Optional[list[Path]],
    repo_root: Path,
) -> Provenance:
    """Compute + write the Provenance 4-tuple into ``baseline.json``.

    The single sim-agnostic stamper (design §3.4). Replaces the
    near-duplicate per-challenge ``stamp_provenance.py`` scripts. The
    produced shas are computed by the EXACT formulas the session-start
    drift guard re-derives, so a freshly stamped baseline reads GREEN:

    - ``image_digest``  — `docker_image_digest(image_tag)` (podman fallback).
                          Raises if the image is not loadable; we never
                          stamp a baseline against an absent image (§1.7).
    - ``config_sha256`` — `sha256_of_file(config_path)` if config_path is
                          given and exists, else the all-zero sentinel.
                          `_check_baseline_provenance` only compares config
                          when the live ``simulator/config.json`` exists, so
                          zeroing is safe for config-baked sims.
    - ``starter_sha256``— `starter_dir_sha256(starter_dir)` (byte-matches
                          session.py L989-992).
    - ``trace_sha256``  — `trace_files_sha256(trace_files)`; ``"0"*64`` when
                          no trace list is supplied (records a reason).
    - ``harness_commit``— `git_head_commit(repo_root)` or "uncommitted".

    Writes the ``provenance`` block + ``provenance_image_tag`` hint back
    into baseline.json (preserving the rest), and records
    ``trace_sha256_reason`` when the trace digest is zeroed.
    """
    baseline_path = baseline_path.resolve()

    image_digest = docker_image_digest(image_tag)
    if image_digest is None:
        raise RuntimeError(
            f"Image {image_tag!r} not loaded locally (tried docker + podman "
            "image inspect); cannot stamp provenance. Run `ensure_image` or "
            "`podman load -i …tar` on this node first."
        )

    if config_path is not None and Path(config_path).exists():
        config_sha256 = sha256_of_file(Path(config_path))
        config_reason = None
    else:
        config_sha256 = "0" * 64
        config_reason = (
            f"no config.json at {config_path}" if config_path is not None
            else "no config_path supplied (config baked into image)"
        )

    starter_sha256 = starter_dir_sha256(Path(starter_dir))

    trace_sha256 = trace_files_sha256(list(trace_files or []))
    trace_reason = None if trace_files else (
        "no per-trace workload to hash (baked into image or trace is a "
        "starter file)"
    )

    harness_commit = git_head_commit(Path(repo_root)) or "uncommitted"

    prov = Provenance(
        image_digest=image_digest,
        config_sha256=config_sha256,
        starter_sha256=starter_sha256,
        trace_sha256=trace_sha256,
        harness_commit=harness_commit,
    )

    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {}
    baseline["provenance"] = prov.to_dict()
    baseline["provenance_image_tag"] = image_tag  # human-readable hint
    if trace_reason is not None:
        baseline["trace_sha256_reason"] = trace_reason
    if config_reason is not None:
        baseline["config_sha256_reason"] = config_reason
    baseline_path.write_text(json.dumps(baseline, indent=2) + "\n")
    return prov


def git_head_commit(repo_root: Path) -> str:
    """Return the git HEAD sha. Empty string if not in a git repo.

    Used as the `harness_commit` field. Allows after-the-fact debugging
    of "which code produced this number".
    """
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode().strip()
        return out
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def repo_root_from_challenge_dir(challenge_dir: Path) -> Path:
    """Repo root for any challenge layout: walk up to the `challenges/`
    component and return its parent. Works for family roots
    (challenges/<fam>) AND assisted tiers (challenges/<fam>/assisted/<L>) —
    a naive ``parents[1]`` is wrong for tiers (it lands on the family dir),
    which made the provenance drift-guard fail to find workload_pools/ and
    falsely refuse every assisted-tier run (wave-1 incident 2026-06-09).
    Falls back to ``parents[1]`` if no `challenges` component exists
    (out-of-tree challenge dirs in tests/tmp)."""
    p = Path(challenge_dir).resolve()
    for anc in p.parents:
        if anc.name == "challenges":
            return anc.parent
    return p.parents[1] if len(p.parents) > 1 else p.parent


def resolve_trace_path(trace_name: str, search_dirs: list[Path]) -> Path:
    """Find a trace file by name across a list of candidate directories.

    Returns the first existing file. Raises FileNotFoundError listing all
    candidates if none match. Used by stamp_provenance.py scripts in
    every challenge to avoid the per-challenge hardcoded-path bug that
    surfaced in Phase H e2e (5/5 challenges had stamp_provenance.py
    silently fail because traces lived in workload_pools/champsim/ but
    the script looked only in eval/subtraces/).

    Pass search_dirs in priority order — typically:
      [challenge_dir / "simulator" / "subtraces",  # new layout
       challenge_dir / "eval" / "subtraces",       # legacy layout
       challenge_dir / "subtraces",                # flat layout
       repo_root / "workload_pools" / "champsim"]  # shared pool
    """
    tried = []
    for d in search_dirs:
        candidate = Path(d) / trace_name
        tried.append(str(candidate))
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"trace {trace_name!r} not found; tried: {tried}"
    )
