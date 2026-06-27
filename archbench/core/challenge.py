"""Challenge — load challenge.yaml + starter files into a typed dataclass.

Schema evolution (most recent first):

  1. Slim challenge.yaml. Per-runtime config (image, model, version,
     timeouts) lives OUT of challenge.yaml in per-runtime
     `runtimes/<rt>/info.yaml` files. A challenge supplies only the TASK
     (the prompt). It does NOT touch the agent's system prompt — the
     agent (runtimes/<rt>/, baked into its image) owns its system prompt
     and role. Also adds `simulator_config.lifecycle` (`standby` | `lazy`).

  2. Unified runtimes block: the four `agent:` / `codex_agent:` /
     `gemini_agent:` / `archharness_agent:` blocks were unified into
     one `runtimes:` block keyed by runtime name.

Legacy challenge.yaml files are still accepted; the loader merges the
old shapes into the new. See docs/lessons_learned.md §7 for context.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

import yaml

log = logging.getLogger("archbench.challenge")


# Permitted values for ``Challenge.starter_visibility`` (spec #2). The
# meaning is enforced by ``archbench.core.runtime_base.stage_workspace`` (Agent
# A2's scope) — this loader only validates the string is one of these.
StarterVisibility = Literal["full", "none", "api_stub"]
_VALID_STARTER_VISIBILITY = ("full", "none", "api_stub")


# How the session prepares the agent environment.
SessionProfile = Literal["managed_mcp", "sim_dev_env"]
_VALID_SESSION_PROFILES = ("managed_mcp", "sim_dev_env")


# Where the agent runs — the NEW canonical knob (docs/docker_management.md §1.2).
# A closed enum. ``challenge_centric`` is recognized-but-not-runnable (§1.3).
AgentImageMode = Literal["agent_centric", "simulator_centric", "challenge_centric"]
_VALID_AGENT_IMAGE_MODES = ("agent_centric", "simulator_centric", "challenge_centric")

# Bidirectional mapping between the legacy ``session_profile`` surface and the
# new ``agent_image_mode``. The two are derived alongside each other and kept
# consistent (managed_mcp<->agent_centric, sim_dev_env<->simulator_centric).
# ``challenge_centric`` has no session_profile counterpart, so it back-fills to
# the closest safe profile (managed_mcp) for the legacy field only.
_SESSION_PROFILE_TO_AGENT_MODE = {
    "managed_mcp": "agent_centric",
    "sim_dev_env": "simulator_centric",
}
_AGENT_MODE_TO_SESSION_PROFILE = {
    "agent_centric": "managed_mcp",
    "simulator_centric": "sim_dev_env",
    "challenge_centric": "managed_mcp",
}

# Evaluation RUBRIC — how DEEP a finding is (grouped in eval_summary). This is
# NOT the difficulty tier (L1/L2/L3 = the run target / agent_image_mode). Words,
# not numbers, for readability. The legacy int form (1/2/3) is still accepted on
# load and mapped through here.
RUBRIC_LEVELS = ("basic", "process", "outcome")
_RUBRIC_FROM_INT = {1: "basic", 2: "process", 3: "outcome"}


def _normalize_rubric(val) -> list[str]:
    """A rubric value (a word, a legacy int, or a list of either) -> list of
    rubric words (basic/process/outcome)."""
    items = val if isinstance(val, list) else [val]
    out: list[str] = []
    for v in items:
        if isinstance(v, int):
            v = _RUBRIC_FROM_INT.get(v)
        if isinstance(v, str) and v in RUBRIC_LEVELS and v not in out:
            out.append(v)
    return out


# Mapping from legacy YAML keys to canonical runtime names.
# Legacy: top-level `agent:` (Claude), `codex_agent:`, `gemini_agent:`,
# `archharness_agent:`, `mini_agent:` — five forked schemas.
# New: one `runtimes:` block with these names as keys.
_LEGACY_RUNTIME_KEYS = {
    "agent":             "claude_code",
    "codex_agent":       "codex",
    "gemini_agent":      "gemini",
    "archharness_agent": "archharness",
    "mini_agent":        "mini",
}


@dataclass
class EvalConfig:
    metric: str = "ipc"
    direction: str = "higher_is_better"  # or "lower_is_better"
    threshold: float = 0.0
    max_submissions: int = 5
    max_code_lines: int = 1000
    baseline_file: str = "baseline.json"
    # Free-form extras: classical baselines table, reference impls, etc.
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeSpec:
    """One runtime's configuration for this challenge.

    NOTE: in the slim challenge.yaml schema, NOTHING in this spec is
    authoritative from challenge.yaml — a challenge supplies only the
    TASK (the prompt), never the agent's system prompt or per-runtime
    config. The fields below (image, model, expected_version,
    round_timeout, max_turns) live in `runtimes/<rt>/info.yaml` and are
    populated by a separate loader. They remain here as Optional for
    backwards-compat with legacy `runtimes:` / `*_agent:` blocks.

    The `runtime_type` / `vendor` / `auth` / `allowed_models` fields come
    from runtimes/<rt>/info.yaml and encode the bundled-vs-byo-model
    distinction:
      - `bundled`     — off-the-shelf vendor CLI (claude_code / codex / gemini).
                        Auth + allowed_models are vendor-specific.
      - `byo_model`   — in-house OpenAI-compatible loop (mini / archharness).
                        Backend selection is via the host-side proxy
                        (archbench/serving/routes.yaml); no per-runtime auth.
    Default is `byo_model` because that's the safer fall-through: a
    missing `type` won't accidentally inherit vendor credentials, and
    the proxy fails fast if the model is unknown.
    """

    name: str                              # e.g. "claude_code"
    image: Optional[str] = None            # override default docker_image
    expected_version: Optional[str] = None # asserted in verify_in_container
    model: Optional[str] = None
    round_timeout: Optional[int] = None    # seconds; None in slim schema
    max_turns: Optional[int] = None        # None in slim schema
    data: dict[str, Any] = field(default_factory=dict)  # full raw block
    # --- bundled-vs-byo-model schema (from runtimes/<rt>/info.yaml) ---
    runtime_type: str = "byo_model"        # "bundled" | "byo_model"
    vendor: Optional[str] = None           # e.g. "anthropic" / "openai" / "google"
    auth: Optional[dict] = None            # {method, host_path, container_path}
    allowed_models: list[str] = field(default_factory=list)


@dataclass
class Challenge:
    """A loaded challenge ready to hand to a runtime + simulator plugin."""

    id: str
    name: str
    simulator: str
    prompt: str
    starter_files: list[str]
    output_files: list[str]
    eval: EvalConfig
    simulator_config: dict[str, Any]
    runtimes: dict[str, RuntimeSpec] = field(default_factory=dict)
    # Multi-simulator challenges (docs/multi_sim_design.md). The PRIMARY
    # simulator is always ``simulator`` (back-compat: every single-sim
    # challenge keeps working unchanged). ``extra_simulators`` lists any
    # ADDITIONAL sims bound in the same session; the connector registers
    # ``<sim>_`` prefixed tools for all of them. Empty = single-sim.
    # Use the ``simulators`` property to get the full ordered list
    # (``[simulator, *extra_simulators]``).
    extra_simulators: list[str] = field(default_factory=list)
    starter_code: dict[str, str] = field(default_factory=dict)
    source_blocklist: list[str] = field(default_factory=list)
    challenge_dir: Optional[Path] = None
    raw_data: dict[str, Any] = field(default_factory=dict)
    # Provenance of the problem (top-level yaml fields, 2026-06-18):
    #   source:    "paper" (reproduce/surpass a published result) | "community"
    #              (tool microbenchmark / championship / in-house). "" = unset.
    #   reference: free-form citation + link of the nominal paper (may be empty
    #              for community challenges or until the link is filled in).
    source: str = ""
    reference: str = ""
    # P6 contract: the agent must produce a list of markdown deliverables
    # AND a pytest-able tests/ dir BEFORE the connector accepts submit().
    # Empty list = legacy contract (free iteration up to max_submissions).
    deliverables: list[str] = field(default_factory=list)
    unit_tests_required: bool = False
    min_deliverable_chars: int = 200  # per-MD length floor — trivially-short
                                       # files reject as BUILD_FAIL.
    # Simulator lifecycle:
    #   "standby" — simulator stays warm; agent iterates against it
    #               mid-session (cache_replacement et al.)
    #   "lazy"    — simulator spun up only at submit time (single-shot).
    # Defaults to "standby" for backwards-compat with challenges that
    # don't declare it.
    lifecycle: str = "standby"
    # Post-session evaluators. Each entry has shape:
    #   {evaluator: str, config: dict, bypass_if_present: str (optional)}
    # The session orchestrator iterates over this list AFTER the agent
    # session ends and workspace copy-out, writing each evaluator's
    # report to results/<run>/eval_<name>.json. A missing or empty list
    # = "no post-session evaluation" (warning, not an error).
    evaluations: list[dict] = field(default_factory=list)
    # Starter visibility regime (spec #2 of the tier-mode rollout). The
    # loader sources this from the top-level ``starter_visibility:`` field
    # in challenge.yaml; missing → "full" for backwards compat. Semantics
    # are enforced in ``archbench.core.runtime_base.stage_workspace``:
    #   - "full":      copy challenge/starter/ to /workspace/starter/ AND
    #                  mirror it into /workspace/ root (current behavior).
    #   - "none":      do NOT copy starter at all. /workspace/ still gets
    #                  advisory validator + traces + requirements + chown.
    #   - "api_stub":  copy challenge/starter/ to /workspace/starter/ only
    #                  (no mirror to /workspace/ root).
    starter_visibility: StarterVisibility = "full"
    # Session environment profile. managed_mcp is the default: normal runtime
    # sandbox + MCP tools. sim_dev_env runs the agent in a simulator development
    # image: simulator source + dependencies/toolchain are present, while the
    # agent still owns reading, editing, building, and running experiments.
    session_profile: SessionProfile = "managed_mcp"
    # Legacy L2 flag retained for old YAMLs/tests. New code should branch on
    # session_profile instead of this implementation detail.
    agent_in_sim_image: bool = False
    # Where the agent runs — the NEW canonical knob (docs/docker_management.md
    # §1.2). Closed enum: "agent_centric" | "simulator_centric" |
    # "challenge_centric". Derived by the loader and kept consistent with
    # session_profile (managed_mcp<->agent_centric, sim_dev_env<->
    # simulator_centric). Resolution is read by archbench.image_management.plan.resolve_images.
    agent_image_mode: AgentImageMode = "agent_centric"
    # Pristine scorer + baseline-provenance image (docs §1.1, §4). A pseudo-path
    # string ("sim/champsim" / a literal tag / "plugin:default") or None. None
    # means "default to this challenge's own simulator image" — the non-breaking
    # anchor. Resolved by archbench.image_management.plan.resolve_images.
    evaluation_sim_image: Optional[str] = None
    # Per-tier MCP tool allowlist. None = all canonical tools (back-compat).
    # A list restricts which the connector registers — e.g. L2 keeps only the
    # Oracle submit + lifecycle tools and drops browse_simulator /
    # read_simulator_file. The single source of truth is still
    # tool_schema.py::TOOLS (CLAUDE.md §1.4); this only FILTERS registration.
    tier_tools: Optional[list[str]] = None
    # Resolved layout paths. For the legacy 3-subdir layout these are
    # ``challenge_dir / "simulator"`` and ``challenge_dir / "evaluation"``
    # — i.e. siblings of challenge.yaml. For the new tier layout (where
    # path is e.g. ``challenges/<family>/tiers/L3/``) these point at the
    # SHARED ``challenges/<family>/common/simulator/`` and
    # ``challenges/<family>/common/evaluation/`` dirs. ``starter_dir`` is
    # always the (tier-local) starter directory; it may not exist for
    # tiers/L2 (api-stub or none visibility regimes).
    #
    # New callers SHOULD prefer these fields over hand-constructed paths
    # like ``challenge.challenge_dir / "simulator"``; legacy callers
    # continue to work because the legacy branch points them at the same
    # directories.
    simulator_dir: Optional[Path] = None
    evaluation_dir: Optional[Path] = None
    starter_dir: Optional[Path] = None
    # Protocol v2: the family's REFERENCE implementation (floor-baseline
    # source; today at <family>/assisted/L1/starter). Never staged into the
    # agent workspace. None when the family ships no reference.
    reference_dir: Optional[Path] = None
    # True when the challenge was loaded from the tier-mode layout
    # (``<family>/tiers/<tier>/``). Callers can use this to opt into
    # tier-aware behavior; legacy single-tier challenges remain ``False``.
    is_tier_layout: bool = False
    # For tier-mode challenges only: the tier directory's parent's parent
    # (``<family>/`` root) and the tier dir's own name (e.g. "L1"/"L2"/"L3").
    # None for legacy 3-subdir challenges.
    family_root: Optional[Path] = None
    tier_name: Optional[str] = None
    # Optional per-evaluator tier labelling for the 3-tier eval framework
    # (see docs/evaluator_framework.md). Maps evaluator name to a single
    # tier (int) or list of tiers (an evaluator MAY serve multiple tiers
    # via different sub-checks — e.g. ``deliverable_files`` does Tier 1
    # via existence checks AND Tier 2 via per-file LLM judges).
    #
    # Two YAML shapes accepted:
    #   rubric_mapping:
    #     deliverable_files: [1, 2]
    #     trajectory_audit: 2
    #     offline_sim_calibration: 2
    #     simulator_metric: 3
    # OR
    #   evaluations:
    #     tier_1_basic: [...]
    #     tier_2_process: [...]
    #     tier_3_outcome: [...]
    # Both are normalized into this dict. Empty = no tier info recorded.
    rubric_mapping: dict[str, list[str]] = field(default_factory=dict)

    @property
    def simulators(self) -> list[str]:
        """All simulators this challenge binds, primary first.

        Single-sim challenges return ``[simulator]`` (the common case).
        Multi-sim challenges (``simulators:`` / ``extra_simulators:`` in
        challenge.yaml) return ``[simulator, *extra_simulators]`` with
        duplicates removed but order preserved. The connector + session
        wiring iterate this to bind one container per sim and register
        the per-sim ``<sim>_`` prefixed MCP tools.
        """
        seen: set[str] = set()
        out: list[str] = []
        for s in [self.simulator, *self.extra_simulators]:
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    def runtime_for(self, name: str) -> RuntimeSpec:
        if name not in self.runtimes:
            raise KeyError(
                f"Challenge {self.id!r} does not declare runtime {name!r}. "
                f"Declared: {sorted(self.runtimes.keys())}"
            )
        return self.runtimes[name]


def _resolve_starter_dir(challenge_dir: Path) -> Path:
    """Resolve the starter dir under the 3-subdir layout, with shim.

    Phase H reorg: starter/ now lives under challenge/. Old layout had
    it at the challenge root. Accept both; warn (once) when the legacy
    location is in use so we know which challenges still need porting.
    """
    new = challenge_dir / "challenge" / "starter"
    if new.exists():
        return new
    legacy = challenge_dir / "starter"
    if legacy.exists():
        log.warning(
            "challenge %s uses legacy starter/ at root (move to challenge/starter/)",
            challenge_dir.name,
        )
        return legacy
    # Return the new path even if missing — caller will raise if declared
    # starter_files don't appear; otherwise empty starter is allowed.
    return new


# Keys lifted from ``final_delivery_evaluation`` into the flat ``eval:`` block.
# These are exactly the fields ``EvalConfig`` is built from in ``load_challenge``
# (``baseline`` maps to ``EvalConfig.baseline_file``); anything else under
# ``final_delivery_evaluation`` that is NOT one of these and NOT
# ``simulator_config`` / ``evaluators`` is preserved as an ``eval.*`` extra so it
# lands in ``EvalConfig.extras`` (same as a flat ``eval:`` block would).
_FD_EVAL_KEYS = (
    "metric", "direction", "threshold", "max_submissions",
    "max_code_lines", "baseline",
)


def _denest_evaluation(data: dict) -> dict:
    """Flatten the nested ``evaluation:`` block into the flat keys the loader reads.

    The nested shape groups the scattered eval-machinery under one readable
    block::

        evaluation:
          final_delivery_evaluation:   # scores the final submitted artifact
            metric: edp
            direction: lower_is_better
            threshold: 0.0
            max_submissions: 1
            max_code_lines: 2000
            baseline: evaluation/baseline.json
            simulator_config: {nn: vgg8, lifecycle: standby}
            evaluators:
              - {evaluator: simulator_metric, tiers: [3], config: {...}}
              - {evaluator: deliverable_files, tiers: [1, 2], config: {...}}
          process_evaluation:          # judges HOW the agent worked
            evaluators:
              - {evaluator: trajectory_audit, tiers: [2], config: {...}}

    This is a PURE SHAPE transform: it rewrites the dict into the EXISTING flat
    keys (``eval``, ``simulator_config``, ``evaluations``, ``rubric_mapping``) that
    every downstream consumer already reads, so nothing past this function needs
    to know the nested form existed.

    Discipline:
      * Each flat key is set ONLY IF ABSENT, so an explicit flat key wins over
        the nested block (flat back-compat) and the function is idempotent — a
        second call (after a denest already ran) is a no-op.
      * ``data["evaluation"]`` is DELETED once consumed.
      * No-op when ``evaluation`` is absent or is not a dict (e.g. a legacy
        ``evaluation:`` STRING scoring alias is left untouched for the caller).
    """
    nested = data.get("evaluation")
    if not isinstance(nested, dict):
        return data

    fd = nested.get("final_delivery_evaluation") or {}
    pr = nested.get("process_evaluation") or {}

    # eval: pull the present EvalConfig keys out of final_delivery_evaluation.
    # Only the keys actually present are lifted (absent → loader defaults), and
    # the whole eval block is set only if there isn't already a flat eval:.
    if "eval" not in data:
        eval_block = {k: fd[k] for k in _FD_EVAL_KEYS if k in fd}
        # Preserve any OTHER scalar keys under final_delivery_evaluation
        # (e.g. round_timeout, latency_gate_fraction, reproduction_*) as eval
        # extras — they are eval config, not simulator_config/evaluators. The
        # EvalConfig parser routes non-reserved keys into EvalConfig.extras.
        for k, v in fd.items():
            if k not in _FD_EVAL_KEYS and k not in ("simulator_config", "evaluators"):
                eval_block[k] = v
        if eval_block:
            data["eval"] = eval_block

    # simulator_config: the user wants the sim config to live HERE.
    if "simulator_config" not in data and "simulator_config" in fd:
        data["simulator_config"] = fd["simulator_config"]

    # evaluators: concat final_delivery + process, pop each entry's `tiers`
    # into rubric_mapping, and append the rest to the flat evaluations list.
    flat_evaluations: list = []
    rubric_mapping: dict[str, list[str]] = {}
    for entry in list(fd.get("evaluators") or []) + list(pr.get("evaluators") or []):
        if not isinstance(entry, dict):
            # Leave malformed entries in the flat list; _normalize_evaluation_entry
            # downstream will warn + drop them, matching flat-shape behavior.
            flat_evaluations.append(entry)
            continue
        entry = dict(entry)
        # `rubric:` (words: basic/process/outcome) is canonical; `tiers:` (ints
        # 1/2/3) is the legacy form, mapped through _normalize_rubric.
        rubric = entry.pop("rubric", None)
        legacy = entry.pop("tiers", None)
        name = entry.get("evaluator")
        spec = rubric if rubric is not None else legacy
        if spec is not None and isinstance(name, str) and name:
            rubric_mapping[name] = _normalize_rubric(spec)
        flat_evaluations.append(entry)

    if "evaluations" not in data and flat_evaluations:
        data["evaluations"] = flat_evaluations
    if "rubric_mapping" not in data and rubric_mapping:
        data["rubric_mapping"] = rubric_mapping

    del data["evaluation"]
    return data


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge ``overlay`` onto ``base`` (overlay wins).

    Nested dicts are merged key-by-key; scalars and LISTS are replaced
    wholesale by the overlay (a tier overlay that sets ``evaluations:`` or
    ``prompt:`` fully replaces the base's). Used for the ``assisted/`` overlay
    layout where the family root's ``challenge.yaml`` (L3) is the base and an
    ``assisted/<tier>/challenge.yaml`` carries only the keys that differ.
    """
    import copy
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _apply_overlay(base_data: dict, overlay_data: dict) -> dict:
    """Merge an ``assisted/`` overlay onto its base (L3), with two conveniences.

    On top of :func:`_deep_merge`:
      - ``prompt_addendum``: appended to the base ``prompt`` (so a tier can add
        a few lines without restating the whole prompt). If the overlay also
        sets ``prompt:`` outright, that override happens first, then the
        addendum appends to it.
      - ``evaluations_remove: [<name>, ...]``: drops the named evaluators from
        the merged ``evaluations`` list (e.g. L1/L2 remove
        ``offline_sim_calibration``).
    Both keys are consumed (not left on the merged dict).
    """
    # Denest BOTH sides BEFORE the deep-merge, so the merge runs on the FLAT
    # eval / evaluations / simulator_config / rubric_mapping keys. This preserves
    # the existing overlay-override semantics (e.g. an overlay's
    # eval.max_submissions deep-merges over the base's) — if we merged the nested
    # `evaluation:` dicts instead, an overlay that only overrides max_submissions
    # would have to restate the whole final_delivery_evaluation block.
    base_data = _denest_evaluation(dict(base_data))
    overlay = _denest_evaluation(dict(overlay_data))
    addendum = overlay.pop("prompt_addendum", None)
    remove = overlay.pop("evaluations_remove", None)
    merged = _deep_merge(base_data, overlay)
    if addendum is not None:
        existing = merged.get("prompt") or ""
        merged["prompt"] = (existing.rstrip() + "\n" + str(addendum)) if existing else str(addendum)
    if remove:
        remove_set = set(remove)
        evs = merged.get("evaluations") or []
        merged["evaluations"] = [
            e for e in evs
            if (e.get("name") if isinstance(e, dict) else e) not in remove_set
        ]
    return merged


def _family_shared_dirs(family_root: Path) -> tuple[Path, Path]:
    """Resolve ``(simulator_dir, evaluation_dir)`` for a family root.

    New flat layout: ``simulator/`` + ``evaluation/`` are DIRECT children of the
    family root — siblings of ``starter/`` and ``assisted/``. The interim layout
    nested them under ``common/``. Prefer flat; fall back to ``common/`` for any
    un-migrated family. Default to flat (the canonical) when neither exists yet.
    """
    if (family_root / "simulator").is_dir() or (family_root / "evaluation").is_dir():
        return family_root / "simulator", family_root / "evaluation"
    if (family_root / "common").is_dir():
        return family_root / "common" / "simulator", family_root / "common" / "evaluation"
    return family_root / "simulator", family_root / "evaluation"


def _assemble_prompt(data: dict) -> str:
    """Build the agent prompt from the structured fields.

    Four prompt fields — each suffixed ``_prompt`` so a human scanning the yaml
    sees at a glance which keys are agent-facing TEXT vs config/machinery
    (eval/evaluations/simulator_config/...). Each is wrapped in its XML tag for
    the agent, in order:
      - ``task_prompt``        = what to build (the deliverable). INVARIANT across tiers.
      - ``constraints_prompt`` = hard limits: legal knobs, fixed params, budgets/gates.
      - ``scoring_prompt``     = how it is scored (the validation method the agent sees).
      - ``others_prompt``      = working conditions: what you're given + how to submit.
                                 The block a tier overlay typically overrides.
    ALL prompt content lives in these four fields — nowhere else. Un-suffixed
    aliases (task/constraints/scoring/others) + the legacy ``setup`` alias for
    ``others`` are still accepted. (The old ``evaluation:`` alias for ``scoring``
    is GONE — ``evaluation:`` is now the nested eval-machinery block, denested by
    :func:`_denest_evaluation` before this runs.) Falls back to the monolithic
    ``prompt:`` for un-migrated challenges.
    """
    task = (data.get("task_prompt") or data.get("task") or "").strip()
    constraints = (data.get("constraints_prompt") or data.get("constraints") or "").strip()
    scoring = (data.get("scoring_prompt") or data.get("scoring") or "").strip()
    others = (data.get("others_prompt") or data.get("others") or "").strip()
    setup = (data.get("setup") or "").strip()  # legacy alias for `others_prompt`
    if task or constraints or scoring or others or setup:
        parts = []
        if task:
            parts.append(f"<task>\n{task}\n</task>")
        if constraints:
            parts.append(f"<constraints>\n{constraints}\n</constraints>")
        if scoring:
            parts.append(f"<scoring>\n{scoring}\n</scoring>")
        if others:
            parts.append(f"<others>\n{others}\n</others>")
        elif setup:
            parts.append(f"<setup>\n{setup}\n</setup>")  # legacy un-migrated
        if data.get("prompt"):
            log.warning("challenge has BOTH structured prompt fields and a "
                        "monolithic prompt; using the structured fields.")
        return "\n\n".join(parts)
    return data.get("prompt", "") or data.get("input", {}).get("prompt", "")


def _detect_family_root(challenge_dir: Path) -> bool:
    """True iff ``challenge_dir`` is a FAMILY ROOT in the assisted/ layout.

    Assisted layout: the family root IS the L3 challenge —

      challenges/<family>/
      ├── challenge.yaml      ← L3 (this challenge_dir)
      ├── starter/            ← L3 api_stub scaffold
      ├── simulator/          ┐ shared across tiers (flat layout); the interim
      ├── evaluation/         ┘ layout nested these under common/
      └── assisted/<tier>/challenge.yaml   ← L1/L2 overlays (extends: ../..)

    Detection: ``challenge.yaml`` exists AND a sibling ``assisted/`` dir exists
    (the unambiguous tell-tale — the legacy 3-subdir layout never has it). The
    ``common/`` form is still accepted for un-migrated families.
    """
    if not (challenge_dir / "challenge.yaml").exists():
        return False
    return (challenge_dir / "assisted").is_dir() or (challenge_dir / "common").is_dir()


def load_challenge(challenge_dir: Path) -> Challenge:
    """Load `challenge_dir/challenge.yaml` + `starter/*` into a Challenge.

    Accepts TWO layouts (spec #1):

      (a) Legacy 3-subdir layout (every existing challenge in 2026-05-31):
          ``challenge_dir`` contains ``challenge.yaml`` AND sibling
          ``challenge/``, ``simulator/``, ``evaluation/`` subdirs. Starter
          files live at ``challenge/starter/`` (with a backwards-compat
          shim accepting the older ``starter/`` at the root). baseline.json
          + evaluate.sh live under ``evaluation/``. config.json + subtraces/
          live under ``simulator/``.

      (b) Tier layout (Phase B onward — see docs/tier_layout.md, to be
          written): ``challenge_dir`` is a tier dir, e.g.
          ``challenges/<family>/tiers/L3/``. ``challenge.yaml`` lives in
          this dir; per-tier ``starter/`` is a sibling (may be absent for
          L2). The SHARED ``simulator/`` and ``evaluation/`` dirs live at
          ``challenges/<family>/common/`` so the family's tiers share the
          same simulator container + evaluate.sh + baseline.json.
          Detection: ``challenge_dir.parent.name == "tiers"`` AND
          ``challenge_dir.parent.parent / "common"`` exists.

    The returned ``Challenge`` shape is identical in both branches;
    callers that need the layout-aware paths read ``simulator_dir`` /
    ``evaluation_dir`` / ``starter_dir`` off the dataclass. The
    ``is_tier_layout`` flag identifies which branch the loader took.
    """
    challenge_dir = Path(challenge_dir)
    yaml_path = challenge_dir / "challenge.yaml"
    if not yaml_path.exists():
        raise FileNotFoundError(f"No challenge.yaml in {challenge_dir}")
    with open(yaml_path) as f:
        data = yaml.safe_load(f) or {}

    # Overlay support (assisted/ layout). If this challenge.yaml `extends` a
    # base (the family root's L3 challenge.yaml), merge the base UNDER it so
    # the overlay only needs to carry the keys that differ.
    extends = data.pop("extends", None)
    overlay_base_dir: Optional[Path] = None
    if extends is not None:
        overlay_base_dir = (challenge_dir / str(extends)).resolve()
        base_yaml = overlay_base_dir / "challenge.yaml"
        if not base_yaml.exists():
            raise FileNotFoundError(
                f"Challenge {challenge_dir}: `extends: {extends}` resolves to "
                f"{overlay_base_dir}, but no challenge.yaml is there."
            )
        with open(base_yaml) as bf:
            base_data = yaml.safe_load(bf) or {}
        base_data.pop("extends", None)
        data = _apply_overlay(base_data, data)

    # Layout detection. Branches diverge ONLY on where starter/, simulator/,
    # and evaluation/ live — everything below is shared (YAML parsing, runtime
    # merging, evaluations block, etc.). Three layouts, in priority order:
    #   (1) assisted overlay  : challenge.yaml had `extends:` → L1/L2 tier
    #   (2) family root        : has assisted/ (or common/) + challenge.yaml → L3
    #   (3) legacy 3-subdir    : the original pre-tier layout
    # (The interim `tiers/<L>/` layout was removed — no challenge used it; see
    # CLAUDE.md §1.3. New challenges MUST use the assisted/ form.)
    is_tier = False
    if overlay_base_dir is not None:
        # (1) Assisted overlay (assisted/L1, assisted/L2). The base is the
        # family root (L3); shared dirs live under <root>/common/.
        is_tier = True
        family_root = overlay_base_dir
        tier_name = challenge_dir.name
        simulator_dir, evaluation_dir = _family_shared_dirs(family_root)
        # Protocol v2 (unified starter): every tier stages the FAMILY-ROOT
        # starter; a tier-local starter/ dir is the REFERENCE implementation
        # (floor-baseline source, never staged).
        starter_dir = family_root / "starter"
    elif challenge_dir.parent.name == "assisted":
        # (1b) STANDALONE assisted tier (the canonical INDEPENDENT form — no
        # `extends`). The yaml fully specifies itself; we only resolve the
        # SHARED sim/eval dirs from the family root (../..). This is what makes
        # L1/L2/L3 readable on their own (CLAUDE.md §1.3): no cross-yaml merge,
        # only shared *artifacts* (one simulator/, one evaluation/baseline.json
        # — the comparability invariant §1.7) referenced by convention.
        is_tier = True
        family_root = challenge_dir.parent.parent
        tier_name = challenge_dir.name
        simulator_dir, evaluation_dir = _family_shared_dirs(family_root)
        # Protocol v2 (unified starter) — see branch (1) note.
        starter_dir = family_root / "starter"
    elif _detect_family_root(challenge_dir):
        # (2) Family root = the L3 challenge itself.
        is_tier = True
        family_root = challenge_dir
        tier_name = "L3"
        simulator_dir, evaluation_dir = _family_shared_dirs(challenge_dir)
        starter_dir = challenge_dir / "starter"
    else:
        # (3) Legacy 3-subdir layout.
        family_root = None
        tier_name = None
        simulator_dir = challenge_dir / "simulator"
        evaluation_dir = challenge_dir / "evaluation"
        starter_dir = _resolve_starter_dir(challenge_dir)

    # Starter split: the staged starter/ holds TWO variants — starter/template/
    # (pure skeleton, no answer) staged to L2/L3, and starter/baseline_assisted/
    # (a copy of the baseline) staged to L1 so the most-help arm tunes the
    # runnable baseline in place. Pick the variant by tier; fall back to the
    # flat starter/ for un-migrated families (backward-compatible, lets families
    # migrate one at a time). The baseline TRUTH still lives in baseline/one_shot
    # (reference_dir below); baseline_assisted is just its staged copy for L1.
    if family_root is not None and tier_name is not None:
        _sub = "baseline_assisted" if tier_name == "L1" else "template"
        _cand = family_root / "starter" / _sub
        if _cand.is_dir():
            starter_dir = _cand

    # The baseline ("reference implementation") lives in its OWN typed
    # folder, baseline/<type>/ — the current hand-written one is
    # baseline/one_shot/. This is the canonical, NEVER-staged source that
    # `archbench baseline` runs and the session drift-guard hashes; it is
    # decoupled from starter/ (which is the agent-facing skeleton). Fall
    # back to the legacy assisted/L1/starter location for un-migrated
    # families (3-subdir challenges have no separate reference at all).
    reference_dir = None
    if family_root is not None:
        for cand in (
            family_root / "baseline" / "one_shot",
            family_root / "assisted" / "L1" / "starter",
        ):
            if cand.is_dir():
                reference_dir = cand
                break

    # Starter files: read everything in the resolved starter dir. The
    # tier branch tolerates a missing starter dir entirely (L2 may have
    # starter_visibility=none). The legacy branch keeps the loud-fail
    # behavior because a missing declared starter file there is always
    # a bug (the starter dir was gitignored and `pip install -e .`
    # clones produced empty starters; that was lessons_learned §X).
    starter_code: dict[str, str] = {}
    if starter_dir.exists():
        for fpath in sorted(starter_dir.iterdir()):
            if fpath.is_file():
                starter_code[fpath.name] = fpath.read_text()

    declared = data.get("input", {}).get("starter_files", []) or []
    missing = [f for f in declared if f not in starter_code]
    if missing:
        raise FileNotFoundError(
            f"Challenge {challenge_dir.name}: declared starter_files {missing} "
            f"not present under {starter_dir}/"
        )

    # Flatten the nested `evaluation:` block (if any) into the flat eval /
    # simulator_config / evaluations / rubric_mapping keys the parsing below reads.
    # For the assisted-overlay path this already ran inside _apply_overlay; this
    # call is idempotent (only-if-absent + deletes the consumed key), so it is a
    # no-op there and only does real work for the family-root / non-overlay path.
    data = _denest_evaluation(data)

    # Simulator config block — accept old `simulator:` shorthand too.
    sim_cfg = data.get("simulator_config") or {}
    if not sim_cfg and isinstance(data.get("simulator"), dict):
        sim_cfg = data["simulator"]

    # Determine simulator name(s). Two shapes:
    #   1. Single-sim (the common case): `simulator: <name>`. The explicit
    #      field wins; otherwise infer from the parent dir name.
    #   2. Multi-sim (docs/multi_sim_design.md): `simulators: [<a>, <b>, ...]`
    #      (alias `extra_simulators:` after a `simulator:`). The FIRST entry
    #      is the primary `simulator`; the rest become `extra_simulators`.
    #      The connector binds one container per sim and registers
    #      `<sim>_`-prefixed MCP tools for all of them.
    extra_simulators: list[str] = []
    sim_list = data.get("simulators")
    if isinstance(sim_list, list) and sim_list:
        # Multi-sim: primary = first, extras = rest. Names must be strings.
        names = [s for s in sim_list if isinstance(s, str) and s]
        if not names:
            raise ValueError(
                f"challenge {challenge_dir.name}: `simulators:` must be a "
                f"non-empty list of simulator name strings (got {sim_list!r})"
            )
        simulator_name = names[0]
        extra_simulators = names[1:]
    else:
        # Fallback when challenge.yaml doesn't declare `simulator:`. For
        # the legacy layout the historical behavior infers from
        # ``challenge_dir.parent.name`` (i.e. the simulator dir name in
        # ``simulators/<sim>/<challenge>/`` / ``challenges/<challenge>``).
        # For tier mode that fallback is meaningless (parent is the
        # literal "tiers" dir), so we raise loudly instead.
        simulator_name = data.get("simulator")
        if isinstance(simulator_name, dict):
            simulator_name = simulator_name.get("name") or (
                None if is_tier else challenge_dir.parent.name
            )
        if not isinstance(simulator_name, str) or not simulator_name:
            if is_tier:
                raise ValueError(
                    f"challenge {challenge_dir.name}: tier-mode challenge "
                    f"must declare `simulator:` (or `simulators:`) in "
                    f"challenge.yaml — there is no parent-dir fallback"
                )
            simulator_name = challenge_dir.parent.name
        # Allow an explicit `extra_simulators:` alongside a scalar `simulator:`.
        raw_extra = data.get("extra_simulators")
        if isinstance(raw_extra, list):
            extra_simulators = [
                s for s in raw_extra
                if isinstance(s, str) and s and s != simulator_name
            ]

    # Eval config
    ev = data.get("eval", {}) or {}
    eval_config = EvalConfig(
        metric=ev.get("metric", "ipc"),
        direction=ev.get("direction", "higher_is_better"),
        threshold=float(ev.get("threshold", 0.0)),
        max_submissions=int(ev.get("max_submissions", 5)),
        max_code_lines=int(ev.get("max_code_lines", 1000)),
        baseline_file=ev.get("baseline", "baseline.json"),
        extras={k: v for k, v in ev.items() if k not in {
            "metric", "direction", "threshold", "max_submissions",
            "max_code_lines", "baseline",
        }},
    )

    # Runtimes — unify legacy 4-way shape into one dict
    runtimes = _build_runtimes(data)

    # Slim schema: per-runtime config (image, model, version, timeouts)
    # lives in `runtimes/<rt>/info.yaml`. Merge those info cards into the
    # `runtimes` dict so callers see a fully-populated RuntimeSpec
    # regardless of whether the challenge.yaml uses the slim or legacy
    # shape. Legacy `runtimes:` block values (if any) win over info.yaml.
    #
    # repo_root resolution differs by layout:
    #   - Legacy: ``challenges/<id>/challenge.yaml`` → repo_root is
    #     ``parents[1]``.
    #   - Tier:   ``challenges/<family>/tiers/<L>/challenge.yaml`` →
    #             repo_root is ``parents[3]``.
    # Falls back to walking up from challenge_dir looking for a sibling
    # ``runtimes/`` dir so future layouts still find runtime info.
    if is_tier:
        repo_root = challenge_dir.resolve().parents[3]
    else:
        repo_root = challenge_dir.resolve().parents[1]
    if not (repo_root / "runtimes").exists():
        # Defensive walk-up: search up to 5 parents for a ``runtimes/``
        # sibling. Keeps legacy/tier hard-coded indices in the common
        # path while still finding it from non-standard test trees.
        for cand in challenge_dir.resolve().parents[:6]:
            if (cand / "runtimes").exists():
                repo_root = cand
                break
    runtimes_root = repo_root / "runtimes"
    if runtimes_root.exists():
        for rt_dir in sorted(runtimes_root.iterdir()):
            if not rt_dir.is_dir():
                continue
            rt_name = rt_dir.name
            info = _load_runtime_info(repo_root, rt_name)
            if info is None:
                continue
            if info.get("public") is False:
                log.info("runtime %s marked public: false; skipping", rt_name)
                continue
            # Legacy values (if any) take precedence over info.yaml so
            # explicit challenge.yaml overrides still win. The slim
            # schema leaves those as None, so info.yaml fills them in.
            legacy = runtimes.get(rt_name)
            # Parse + validate the bundled/byo_model schema from info.yaml.
            # _validate_runtime_type returns the normalized (type, vendor,
            # auth, allowed_models) tuple and raises on hard schema errors.
            (rt_type, rt_vendor, rt_auth, rt_allowed) = _validate_runtime_type(
                rt_name, info,
            )
            runtimes[rt_name] = RuntimeSpec(
                name=rt_name,
                image=(legacy.image if legacy and legacy.image else info.get("image")),
                expected_version=(
                    legacy.expected_version
                    if legacy and legacy.expected_version
                    else info.get("runtime_version")
                ),
                model=(
                    legacy.model if legacy and legacy.model
                    else info.get("default_model")
                ),
                round_timeout=(
                    legacy.round_timeout
                    if legacy and legacy.round_timeout is not None
                    else (info.get("default_round_timeout") or 14400)
                ),
                max_turns=(
                    legacy.max_turns
                    if legacy and legacy.max_turns is not None
                    else (info.get("default_max_turns") or 400)
                ),
                data=dict(info),
                runtime_type=rt_type,
                vendor=rt_vendor,
                auth=rt_auth,
                allowed_models=rt_allowed,
            )

    # Lifecycle is declared inside simulator_config (it's a property of
    # how the agent interacts with the simulator during the session).
    # Defaults to "standby" for backwards-compat.
    lifecycle = sim_cfg.get("lifecycle", "standby") if isinstance(sim_cfg, dict) else "standby"
    if lifecycle not in ("standby", "lazy"):
        log.warning(
            "challenge.yaml simulator_config.lifecycle=%r is not in "
            "('standby', 'lazy'); treating as 'standby'", lifecycle,
        )
        lifecycle = "standby"

    # Post-session evaluators: list of {evaluator, config, ...}. Missing
    # block is OK (just no evaluation runs); log a one-line warning so
    # the operator knows the challenge silently skips the eval step.
    evaluations, rubric_mapping = _load_evaluations(data, challenge_dir)

    # starter_visibility (spec #2): top-level scalar in challenge.yaml.
    # Protocol v2 (2026-06-11) DEPRECATES the field (doctor W6): staging is
    # unified for every tier (family starter → /workspace/starter/) and
    # stage_workspace ignores the value. It is still parsed so explicit
    # legacy declarations keep their semantics (and the contradiction guard
    # below). Absent field (the v2 form) keeps the legacy default "full";
    # simulator_centric tiers are normalized to "none" below (the only
    # value ever legal there).
    raw_sv = data.get("starter_visibility")
    sv_explicit = raw_sv is not None
    if raw_sv is None:
        raw_sv = "full"
    if raw_sv not in _VALID_STARTER_VISIBILITY:
        log.warning(
            "challenge %s starter_visibility=%r is not in %s; treating as 'full'",
            challenge_dir.name, raw_sv, _VALID_STARTER_VISIBILITY,
        )
        raw_sv = "full"
    starter_visibility: StarterVisibility = raw_sv  # type: ignore[assignment]

    raw_session_profile = data.get("session_profile")
    legacy_agent_in_sim = bool(data.get("agent_in_sim_image", False))
    if raw_session_profile is None:
        raw_session_profile = "sim_dev_env" if legacy_agent_in_sim else "managed_mcp"
        if legacy_agent_in_sim:
            log.warning(
                "challenge %s uses legacy agent_in_sim_image=true; treating as "
                "session_profile='sim_dev_env'. New L2 challenges should set "
                "session_profile explicitly.",
                challenge_dir.name,
            )
    if raw_session_profile == "self_provisioning":
        log.warning(
            "challenge %s uses obsolete session_profile='self_provisioning'; "
            "treating as 'sim_dev_env'.",
            challenge_dir.name,
        )
        raw_session_profile = "sim_dev_env"
    if raw_session_profile == "prebuilt_sim_image":
        log.warning(
            "challenge %s uses legacy session_profile='prebuilt_sim_image'; "
            "treating as 'sim_dev_env'.",
            challenge_dir.name,
        )
        raw_session_profile = "sim_dev_env"
    if raw_session_profile not in _VALID_SESSION_PROFILES:
        log.warning(
            "challenge %s session_profile=%r is not in %s; treating as 'managed_mcp'",
            challenge_dir.name, raw_session_profile, _VALID_SESSION_PROFILES,
        )
        raw_session_profile = "managed_mcp"
    session_profile: SessionProfile = raw_session_profile  # type: ignore[assignment]

    # agent_image_mode (docs/docker_management.md §1.2) — the NEW canonical
    # "where does the agent run" knob. Resolution priority:
    #   1. explicit `agent_image_mode:` in YAML (wins; back-fills session_profile)
    #   2. mapped from the resolved session_profile (which already folds in the
    #      legacy `agent_in_sim_image: true` bool above, so that case maps to
    #      simulator_centric for free)
    #   3. default "agent_centric"
    #
    # DELIBERATE DIVERGENCE from session_profile / starter_visibility (which
    # warn-and-degrade to a safe default on an unknown value): an unknown
    # EXPLICIT agent_image_mode RAISES (ValueError). This is what makes the
    # challenge_centric NotImplementedError safety story hold (§1.2/§1.3) — a
    # typo must never silently degrade into a runnable mode.
    raw_agent_image_mode = data.get("agent_image_mode")
    if raw_agent_image_mode is not None:
        if raw_agent_image_mode not in _VALID_AGENT_IMAGE_MODES:
            raise ValueError(
                f"challenge {challenge_dir.name}: agent_image_mode="
                f"{raw_agent_image_mode!r} is not one of "
                f"{_VALID_AGENT_IMAGE_MODES}. (Unknown agent_image_mode RAISES "
                f"by design — docs/docker_management.md §1.2 — so challenge_centric "
                f"can be recognized-but-not-runnable without a typo silently "
                f"degrading into a runnable mode.)"
            )
        agent_image_mode = raw_agent_image_mode
        # The explicit key wins; back-fill session_profile so the two stay
        # consistent (the L2 tests + overlays read session_profile).
        session_profile = _AGENT_MODE_TO_SESSION_PROFILE[agent_image_mode]  # type: ignore[assignment]
    else:
        agent_image_mode = _SESSION_PROFILE_TO_AGENT_MODE.get(
            session_profile, "agent_centric",
        )
    agent_image_mode_typed: AgentImageMode = agent_image_mode  # type: ignore[assignment]

    # belt-and-suspenders / illegal-states-unrepresentable (CLAUDE.md §1.17): simulator_centric
    # bakes the sim SOURCE into the agent image, so an EXPLICIT legacy
    # starter_visibility other than 'none' is contradictory — enforce at LOAD
    # time (not merely documented) so an agent editing a challenge.yaml can't
    # create a nonsensical combo that only blows up deep in a run. (Mirrored
    # by the tests/test_architecture_invariants.py gate — belt AND suspenders.)
    # Protocol v2 yamls OMIT the deprecated field entirely (doctor W6);
    # absence is normalized to 'none' here so legacy consumers (provenance
    # starter-check relaxation, session staging pass-through) keep the
    # simulator_centric semantics unchanged.
    if agent_image_mode_typed == "simulator_centric":
        if sv_explicit and starter_visibility != "none":
            raise ValueError(
                f"challenge {challenge_dir.name}: illegal tier config "
                f"agent_image_mode='simulator_centric' + "
                f"starter_visibility={starter_visibility!r}. simulator_centric bakes "
                f"the sim source into the agent image, so it MUST use "
                f"starter_visibility:'none' (no staged scaffold). See CLAUDE.md §1.17."
            )
        starter_visibility = "none"

    # evaluation_sim_image (docs §1.1, §4): an optional pseudo-path scorer
    # override. None => default to this challenge's own simulator image (the
    # non-breaking anchor). Pseudo-path resolution happens lazily in
    # archbench.image_management.plan.resolve_images, NOT here (the loader is sim-agnostic
    # and must not import the simulator registry).
    raw_eval_sim_image = data.get("evaluation_sim_image")
    evaluation_sim_image = (
        str(raw_eval_sim_image) if raw_eval_sim_image else None
    )

    return Challenge(
        id=data.get("id", challenge_dir.name),
        name=data.get("name", challenge_dir.name),
        simulator=simulator_name,
        extra_simulators=extra_simulators,
        prompt=_assemble_prompt(data),
        starter_files=declared or list(starter_code.keys()),
        output_files=data.get("output", {}).get("files", []) or [],
        eval=eval_config,
        simulator_config=sim_cfg,
        runtimes=runtimes,
        starter_code=starter_code,
        source_blocklist=data.get("source_blocklist", []) or [],
        source=data.get("source", "") or "",
        reference=data.get("reference", "") or "",
        challenge_dir=challenge_dir,
        raw_data=data,
        deliverables=data.get("deliverables", []) or [],
        unit_tests_required=bool(data.get("unit_tests_required", False)),
        min_deliverable_chars=int(data.get("min_deliverable_chars", 200)),
        lifecycle=lifecycle,
        evaluations=evaluations,
        rubric_mapping=rubric_mapping,
        starter_visibility=starter_visibility,
        session_profile=session_profile,
        agent_in_sim_image=legacy_agent_in_sim,
        agent_image_mode=agent_image_mode_typed,
        evaluation_sim_image=evaluation_sim_image,
        tier_tools=(list(data["tier_tools"]) if data.get("tier_tools") else None),
        simulator_dir=simulator_dir,
        evaluation_dir=evaluation_dir,
        starter_dir=starter_dir,
        reference_dir=reference_dir,
        is_tier_layout=is_tier,
        family_root=family_root,
        tier_name=tier_name,
    )


# The three canonical tier keys in the tiered ``evaluations:`` shape.
# See docs/evaluator_framework.md "The 3-tier evaluation framework".
_TIER_KEYS = {
    "tier_1_basic": 1,
    "tier_2_process": 2,
    "tier_3_outcome": 3,
}


def _load_evaluations(
    data: dict[str, Any],
    challenge_dir: Path,
) -> tuple[list[dict], dict[str, list[int]]]:
    """Read the post-session ``evaluations:`` block + ``rubric_mapping:``.

    Lookup order:
      1. Top-level ``evaluations:`` in challenge.yaml.
      2. Sibling ``evaluation.yaml`` in the challenge dir (legacy path).

    Accepts THREE shapes of ``evaluations:``:

      A. Flat list (legacy / current cache_replacement):
           evaluations:
             - evaluator: simulator_metric
               config: {...}
             - evaluator: deliverable_files
               ...

      B. Tier-grouped mapping (3-tier framework, see
         docs/evaluator_framework.md):
           evaluations:
             tier_1_basic:    [{evaluator: ..., ...}, ...]
             tier_2_process:  [...]
             tier_3_outcome:  [...]

      C. Hybrid: flat list + top-level ``rubric_mapping:`` that labels
         each evaluator by tier(s):
           evaluations: [...]
           rubric_mapping:
             deliverable_files: [1, 2]
             trajectory_audit: 2
             simulator_metric: 3

    All three normalize into:
      * A FLAT list of {evaluator, config, ...} entries (no duplicates).
        An evaluator declared in multiple tiers (shape B) is run ONCE;
        its tier membership is recorded in the rubric_mapping return.
      * A ``rubric_mapping`` dict mapping evaluator name → list[int].

    Returns ``([], {})`` and logs a warning if no shape is found.
    """
    raw = data.get("evaluations")
    if raw is None:
        sibling = challenge_dir / "evaluation.yaml"
        if sibling.exists():
            try:
                with open(sibling) as f:
                    sib = yaml.safe_load(f) or {}
                raw = sib.get("evaluations")
                if "rubric_mapping" in sib and "rubric_mapping" not in data:
                    data = dict(data)
                    data["rubric_mapping"] = sib["rubric_mapping"]
            except Exception as e:
                log.warning("evaluation.yaml unreadable: %s", e)
                raw = None
    if raw is None:
        log.warning(
            "challenge %s has no `evaluations:` block (in challenge.yaml "
            "or evaluation.yaml); post-session evaluation will be skipped",
            challenge_dir.name,
        )
        return ([], {})

    # Tier shape: dict with at least one tier_* key.
    is_tier_shape = (
        isinstance(raw, dict)
        and any(k in raw for k in _TIER_KEYS)
    )
    if isinstance(raw, dict) and not is_tier_shape:
        log.warning(
            "challenge %s `evaluations:` is a mapping but no tier_* keys "
            "(expected one of %s); treating as empty",
            challenge_dir.name, sorted(_TIER_KEYS.keys()),
        )
        return ([], {})
    if not isinstance(raw, (list, dict)):
        log.warning(
            "challenge %s `evaluations:` must be a list or tier-keyed mapping "
            "(got %s); skipping",
            challenge_dir.name, type(raw).__name__,
        )
        return ([], {})

    rubric_mapping: dict[str, list[str]] = {}
    flat: list[dict] = []
    if is_tier_shape:
        # Iterate tiers in numerical order so the flat list is reproducible.
        for tier_key, tier_num in sorted(_TIER_KEYS.items(), key=lambda kv: kv[1]):
            entries = raw.get(tier_key) or []
            if not isinstance(entries, list):
                log.warning(
                    "challenge %s evaluations.%s must be a list; skipping",
                    challenge_dir.name, tier_key,
                )
                continue
            for entry in entries:
                norm = _normalize_evaluation_entry(entry, challenge_dir.name)
                if norm is None:
                    continue
                name = norm["evaluator"]
                rubric_mapping.setdefault(name, [])
                _word = _RUBRIC_FROM_INT.get(tier_num, "outcome")
                if _word not in rubric_mapping[name]:
                    rubric_mapping[name].append(_word)
                # If an evaluator appears in multiple tiers, only the FIRST
                # encounter contributes a flat-list entry (we run it once).
                if all(e["evaluator"] != name for e in flat):
                    flat.append(norm)
        # Loud deprecation warning is NOT emitted for the tier shape — it's
        # the recommended modern form. We only warn about flat without
        # rubric_mapping below.
    else:
        # Flat list — the legacy shape. Normalize entries.
        for entry in raw:
            norm = _normalize_evaluation_entry(entry, challenge_dir.name)
            if norm is not None:
                flat.append(norm)

    # Top-level rubric_mapping: explicit per-evaluator labels override
    # anything derived from the tier shape above. Accepts int or list[int].
    explicit = data.get("rubric_mapping") or {}
    if isinstance(explicit, dict):
        for ev_name, val in explicit.items():
            normed = _normalize_rubric(val)
            if normed:
                rubric_mapping[ev_name] = normed
    elif explicit:
        log.warning(
            "challenge %s `rubric_mapping:` must be a mapping; ignoring",
            challenge_dir.name,
        )

    # Soft deprecation: flat-list shape AND no rubric_mapping = legacy.
    if not is_tier_shape and not rubric_mapping and flat:
        log.warning(
            "challenge %s evaluations: are flat with no rubric_mapping — all "
            "evaluators will be reported under 'tier_unspecified'. Consider "
            "adding a rubric_mapping: block (see docs/evaluator_framework.md).",
            challenge_dir.name,
        )

    return (flat, rubric_mapping)


def _normalize_evaluation_entry(
    entry: Any, challenge_name: str,
) -> Optional[dict]:
    """Validate + normalize one `evaluations:` list entry.

    Returns the normalized dict, or None if the entry is malformed (with
    a warning log). The normalized shape is:
      {"evaluator": <str>, "config": <dict>, ...}
    A top-level `bypass_if_present:` is mirrored into `config:` for
    backwards compat with evaluators that read from config.
    """
    if not isinstance(entry, dict):
        log.warning(
            "challenge %s `evaluations:` entry is not a mapping: %r; skipping",
            challenge_name, entry,
        )
        return None
    name = entry.get("evaluator")
    if not isinstance(name, str) or not name:
        log.warning(
            "challenge %s `evaluations:` entry missing string `evaluator:`; skipping",
            challenge_name,
        )
        return None
    norm = dict(entry)
    if "config" not in norm or norm["config"] is None:
        norm["config"] = {}
    if "bypass_if_present" in norm and "bypass_if_present" not in norm["config"]:
        norm["config"]["bypass_if_present"] = norm["bypass_if_present"]
    return norm


def _build_runtimes(data: dict[str, Any]) -> dict[str, RuntimeSpec]:
    """Build the canonical `runtimes:` dict, accepting both old and new shapes.

    Precedence: if both shapes are present (an explicit `runtimes:` block
    AND legacy `*_agent:` blocks), the legacy blocks are merged into
    `runtimes:` only for keys not already there (the new schema wins).

    Emits a deprecation warning if a legacy `runtimes:` block carries
    per-runtime config (image/model/version/timeouts) — those belong in
    `runtimes/<rt>/info.yaml` now. A challenge.yaml should carry no
    per-runtime config at all.
    """
    out: dict[str, RuntimeSpec] = {}
    raw_runtimes = data.get("runtimes") or {}
    if not isinstance(raw_runtimes, dict):
        raise ValueError(
            "challenge.yaml `runtimes:` must be a mapping "
            f"(got {type(raw_runtimes).__name__})"
        )
    if raw_runtimes and _runtimes_block_has_runtime_config(raw_runtimes):
        log.warning(
            "challenge.yaml has legacy 'runtimes:' block; per-runtime "
            "config (image, model, version, timeouts) should move to "
            "runtimes/<rt>/info.yaml. A challenge.yaml should carry no "
            "per-runtime config."
        )
    for name, block in raw_runtimes.items():
        out[name] = _runtime_from_dict(name, block or {})

    for legacy_key, canonical in _LEGACY_RUNTIME_KEYS.items():
        block = data.get(legacy_key)
        if block and canonical not in out:
            out[canonical] = _runtime_from_dict(canonical, block)

    return out


# Keys on a runtime block that indicate per-runtime config (the kind of
# stuff that now belongs in runtimes/<rt>/info.yaml, not challenge.yaml).
_RUNTIME_CONFIG_KEYS = {
    "image", "agent_image", "runtime_version", "model",
    "round_timeout", "max_turns",
}


def _runtimes_block_has_runtime_config(raw_runtimes: dict[str, Any]) -> bool:
    """True if any runtime entry has non-prompt-append fields (legacy shape)."""
    for block in raw_runtimes.values():
        if not isinstance(block, dict):
            continue
        if any(k in block for k in _RUNTIME_CONFIG_KEYS):
            return True
    return False


_VALID_RUNTIME_TYPES = ("bundled", "byo_model")


def _validate_runtime_type(
    runtime_name: str,
    info: dict[str, Any],
) -> tuple[str, Optional[str], Optional[dict], list[str]]:
    """Parse + validate the bundled/byo_model fields from info.yaml.

    Returns a normalized (runtime_type, vendor, auth, allowed_models)
    tuple. Validation policy:

      - Unknown / missing `type`: degrade to "byo_model" with a warning.
        (Safer fallback: missing type won't accidentally inherit vendor
        creds; proxy fails fast if the model is unknown.)
      - `type: bundled` MUST carry `auth` AND a non-empty `allowed_models`,
        AND `default_model` must be in `allowed_models`. Raises ValueError
        on any of those — loud, not graceful, because a misconfigured
        bundled runtime would leak the wrong credentials.
      - `type: byo_model` should NOT carry `auth` or `allowed_models`.
        If present, warn and drop them (the host-side proxy is the source
        of truth for byo backends).
    """
    rt_type = info.get("type", "byo_model")
    if rt_type not in _VALID_RUNTIME_TYPES:
        log.warning(
            "runtimes/%s/info.yaml: unknown type=%r (expected one of %s); "
            "treating as 'byo_model'",
            runtime_name, rt_type, _VALID_RUNTIME_TYPES,
        )
        rt_type = "byo_model"

    vendor = info.get("vendor")
    auth = info.get("auth")
    allowed_models = info.get("allowed_models") or []
    if not isinstance(allowed_models, list):
        raise ValueError(
            f"runtimes/{runtime_name}/info.yaml: allowed_models must be a "
            f"list (got {type(allowed_models).__name__})"
        )

    if rt_type == "bundled":
        # Hard validation: bundled = vendor-bound, can't fall back.
        if not isinstance(auth, dict) or not auth:
            raise ValueError(
                f"runtimes/{runtime_name}/info.yaml: type=bundled requires "
                f"an `auth:` mapping (method/host_path/container_path)"
            )
        if not allowed_models:
            raise ValueError(
                f"runtimes/{runtime_name}/info.yaml: type=bundled requires "
                f"a non-empty `allowed_models:` list"
            )
        default_model = info.get("default_model")
        if default_model is not None and default_model not in allowed_models:
            raise ValueError(
                f"runtimes/{runtime_name}/info.yaml: default_model="
                f"{default_model!r} is not in allowed_models={allowed_models!r}"
            )
        return (rt_type, vendor, auth, list(allowed_models))

    # byo_model: warn + drop vendor-style fields if present.
    if auth is not None:
        log.warning(
            "runtimes/%s/info.yaml: type=byo_model carries an `auth:` "
            "field; ignoring (the host-side proxy handles backend auth)",
            runtime_name,
        )
        auth = None
    if allowed_models:
        log.warning(
            "runtimes/%s/info.yaml: type=byo_model carries `allowed_models:`; "
            "ignoring (archbench/serving/routes.yaml is the source of truth)",
            runtime_name,
        )
        allowed_models = []
    return (rt_type, vendor, auth, allowed_models)


def _load_runtime_info(repo_root: Path, runtime_name: str) -> Optional[dict]:
    """Load `runtimes/<name>/info.yaml`.

    Returns the parsed mapping, or None if the file is missing/unreadable.
    Per-runtime info cards carry image, model, version, and timeout
    defaults — the bits the slim challenge.yaml schema does NOT carry.
    """
    info_path = repo_root / "runtimes" / runtime_name / "info.yaml"
    if not info_path.exists():
        return None
    try:
        with open(info_path) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        log.warning("failed to load %s: %s", info_path, e)
        return None


def _runtime_from_dict(name: str, block: dict[str, Any]) -> RuntimeSpec:
    # Slim shape: a legacy runtime block may be empty. In that case,
    # round_timeout / max_turns should stay None so the caller can fill
    # them in from runtimes/<rt>/info.yaml.
    has_round = "round_timeout" in block
    has_turns = "max_turns" in block
    return RuntimeSpec(
        name=name,
        image=block.get("agent_image") or block.get("image"),
        expected_version=block.get("runtime_version"),
        model=block.get("model"),
        round_timeout=int(block["round_timeout"]) if has_round else None,
        max_turns=int(block["max_turns"]) if has_turns else None,
        data=dict(block),
    )


def list_challenges(
    challenges_root: Path,
    simulator: Optional[str] = None,
) -> list[Challenge]:
    """Scan a directory of challenge dirs. Sub-dirs without challenge.yaml are skipped."""
    challenges = []
    for ch_dir in sorted(Path(challenges_root).iterdir()):
        if not ch_dir.is_dir():
            continue
        if not (ch_dir / "challenge.yaml").exists():
            continue
        try:
            ch = load_challenge(ch_dir)
        except Exception as e:
            log.warning("Skipping %s: %s", ch_dir, e)
            continue
        if simulator and ch.simulator != simulator:
            continue
        challenges.append(ch)
    return challenges
