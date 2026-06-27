"""[concept: ORCHESTRATION — see ARCHITECTURE.md]

RunSpec — the single declarative description of ONE benchmark run.

The front door. A run is fully described by a YAML file (the "run spec") plus,
at most, an agent override. Everything the harness needs is read from here —
there is NO sprawl of command-line flags that can drift or contradict each
other. One file → one run, reproducible and reviewable.

    # run.yaml — the whole contract, at a glance:
    challenge: branch_predictor   # family name  (or an explicit challenges/... path)
    tier: L1                      # L1 | L2 | L3  (default: L3 — the headline tier)
    agent: mini                   # the agent runtime
    model: gemma4                 # optional — runtime's default_model if omitted
    anonymize: true               # optional — default true (head-to-head fairness)
    run_name: my_run              # optional — auto-generated if omitted
    results_dir: results          # optional — default <repo>/results
    dev: false                    # optional — bind-mount runtime src (dev_capable only)
    thinking: false               # optional — reasoning mode

Tier resolution (the one rule): a FAMILY NAME resolves with the tier —
``L3 → challenges/<family>`` (the family root), ``L1|L2 →
challenges/<family>/assisted/<tier>``. A value containing ``/`` is taken as an
explicit challenge path and used verbatim.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
VALID_TIERS = ("L1", "L2", "L3")


@dataclass
class RunSpec:
    """A fully-resolved run description. Build it with ``from_yaml`` (the front
    door) or directly (the legacy CLI maps onto the same shape)."""

    challenge_dir: Path          # resolved challenge directory (has challenge.yaml)
    agent: str                   # agent runtime name
    model: Optional[str] = None
    anonymize: bool = True
    run_name: Optional[str] = None
    results_dir: Optional[Path] = None
    dev: bool = False
    thinking: bool = False
    # provenance (for logging / debugging only)
    tier: Optional[str] = None
    spec_path: Optional[Path] = None

    @classmethod
    def from_yaml(cls, path, agent_override: Optional[str] = None) -> "RunSpec":
        """Load + fully validate a run spec from a YAML file."""
        path = Path(path)
        data = yaml.safe_load(path.read_text()) or {}
        if not isinstance(data, dict):
            raise ValueError(f"run spec {path}: must be a YAML mapping")

        challenge = data.get("challenge")
        if not challenge:
            raise ValueError(f"run spec {path}: missing required key `challenge:`")

        tier = str(data.get("tier", "L3")).upper()
        if tier not in VALID_TIERS:
            raise ValueError(
                f"run spec {path}: `tier:` must be one of {VALID_TIERS}, got {tier!r}"
            )

        agent = agent_override or data.get("agent")
        if not agent:
            raise ValueError(
                f"run spec {path}: no agent — set `agent:` in the spec or pass one "
                f"on the CLI (archbench run {path.name} <agent>)"
            )

        challenge_dir = cls.resolve_challenge_dir(challenge, tier)
        results_dir = data.get("results_dir")
        return cls(
            challenge_dir=challenge_dir,
            agent=agent,
            model=data.get("model"),
            anonymize=bool(data.get("anonymize", True)),
            run_name=data.get("run_name"),
            results_dir=Path(results_dir) if results_dir else None,
            dev=bool(data.get("dev", False)),
            thinking=bool(data.get("thinking", False)),
            tier=tier,
            spec_path=path,
        )

    @staticmethod
    def resolve_challenge_dir(challenge: str, tier: str) -> Path:
        """(challenge, tier) → challenge directory. The one rule (see module doc):
        a value with ``/`` is an explicit path; otherwise it's a family name
        resolved with the tier (L3 → root, L1/L2 → assisted/<tier>)."""
        c = str(challenge)
        if "/" in c:  # explicit path
            p = Path(c)
            return (p if p.is_absolute() else REPO_ROOT / p).resolve()
        base = REPO_ROOT / "challenges" / c  # family name
        d = base if tier == "L3" else base / "assisted" / tier
        return d.resolve()
