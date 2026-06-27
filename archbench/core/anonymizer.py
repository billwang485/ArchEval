"""Anonymizer — the end-to-end three-layer scrub/translate contract.

Background. The legacy benchmark exposed SPEC benchmark names like
`482.sphinx3-1100B`, `403.gcc-16B`, `605.mcf_s-1152B` to the agent
verbatim. LLMs recognize these names from training data, which
contaminates the evaluation: an LLM may "know" how a workload behaves
without having to reason about it from the trace.

Commit 586b6dbd in the legacy repo introduced a trace anonymizer, but
the rollout was incomplete. Three layers leaked at different times:

    1. **Workspace** — file names in the agent's container (`/traces/...`).
       Fixed first.
    2. **Prompt** — trace references in the natural-language task prompt.
       Fixed second.
    3. **Connector output** — when the agent called `read_simulator_file`
       or got metrics back from a submit, the simulator's output still
       contained the original names. THIS WAS THE LAST LAYER FIXED, and
       the one that's easy to forget when adding new connector tools.

The structural fix: the connector is the only path between agent and
simulator. Every message that crosses it goes through `scrub_outbound`
(simulator→agent) or `translate_inbound` (agent→simulator). The same
`Anonymizer` instance is used for both, holding one canonical mapping.

CI test plants known leak tokens (perlbench, gcc, mcf, sphinx3, …) and
asserts none reach the agent through any tool call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# Canonical list of SPEC tokens an LLM is likely to recognize. Used by
# the CI leak-detector test, not by scrubbing itself (scrubbing uses the
# full forward/reverse map below).
KNOWN_LEAK_TOKENS = (
    "perlbench", "bzip2", "gcc", "mcf", "gobmk", "hmmer", "sjeng",
    "libquantum", "h264ref", "omnetpp", "astar", "xalancbmk", "sphinx3",
    "leela", "deepsjeng", "x264", "lbm", "milc", "gromacs", "cactusBSSN",
)


@dataclass(frozen=True)
class AnonymizerConfig:
    enabled: bool = False
    # Path to JSON file with {original_name: anon_name, ...}. If None,
    # anonymizer is a no-op (suitable for development on local-only runs).
    mapping_file: Optional[str] = None


class Anonymizer:
    """Single source of truth for trace-name anonymization.

    Construct ONCE per run and pass to BOTH the connector's outbound and
    inbound paths — never construct two instances or you risk a mapping
    mismatch (a known way to leak: forward map and reverse map drift).

    Usage:

        anon = Anonymizer.load(config)

        # Agent sends a tool call ("read_simulator_file('/traces/W003.txt')")
        sim_path = anon.translate_inbound("/traces/W003.txt")
        # → "/traces/482.sphinx3-1100B.trace.txt"

        # Simulator returns output containing original name
        agent_view = anon.scrub_outbound(raw_sim_output)
        # All occurrences of original SPEC names → anon tokens
    """

    def __init__(self, forward: dict[str, str]):
        # Canonical map: original_name → anon_name. Reverse is computed
        # at construction so the two cannot drift.
        self._forward = dict(forward)
        self._reverse = {anon: orig for orig, anon in forward.items()}
        # Pre-build a regex for scrub_outbound: alternation of all
        # original names, longest-first to avoid greedy prefix matches.
        keys = sorted(self._forward.keys(), key=len, reverse=True)
        # Empty mapping → no-op regex that matches nothing
        if keys:
            pattern = "|".join(re.escape(k) for k in keys)
            self._scrub_re = re.compile(pattern)
        else:
            self._scrub_re = None

    @classmethod
    def disabled(cls) -> "Anonymizer":
        """No-op anonymizer for runs without --anonymize."""
        return cls(forward={})

    @classmethod
    def load(cls, config: AnonymizerConfig) -> "Anonymizer":
        if not config.enabled:
            return cls.disabled()
        if not config.mapping_file:
            raise ValueError(
                "Anonymizer enabled but no mapping_file configured. "
                "Refusing silent no-op (would leak original names)."
            )
        import json
        with open(config.mapping_file) as f:
            forward = json.load(f)
        if not forward:
            raise ValueError(
                f"Mapping file {config.mapping_file} is empty. "
                "Refusing to run with --anonymize and an empty map."
            )
        return cls(forward=forward)

    @property
    def enabled(self) -> bool:
        return self._scrub_re is not None

    # ---- the two crossing-the-boundary methods ----

    def scrub_outbound(self, text: str) -> str:
        """Simulator → agent. Replace every original name with its anon token.

        Called by the connector on EVERY string returned to the agent —
        submit output, browse listings, file reads, error messages.
        """
        if self._scrub_re is None:
            return text
        return self._scrub_re.sub(
            lambda m: self._forward.get(m.group(0), m.group(0)),
            text,
        )

    def translate_inbound(self, anon_name_or_path: str) -> str:
        """Agent → simulator. Translate anon token back to original.

        Called by the connector on EVERY agent-supplied trace name
        before it touches the simulator. Pass-through if no mapping
        (lets non-anon paths like `/api/reference.md` flow unchanged).
        """
        if not self._reverse:
            return anon_name_or_path
        # Find any anon token substring and replace. Same longest-first
        # ordering as scrub_outbound.
        for anon in sorted(self._reverse.keys(), key=len, reverse=True):
            if anon in anon_name_or_path:
                anon_name_or_path = anon_name_or_path.replace(
                    anon, self._reverse[anon],
                )
        return anon_name_or_path

    # ---- introspection ----

    def all_anon_tokens(self) -> set[str]:
        return set(self._reverse.keys())

    def all_original_tokens(self) -> set[str]:
        return set(self._forward.keys())
