"""Core abstractions shared by all simulators, runtimes, and challenges.

Stable public surface:

- `SimulatorPlugin`: ABC every simulator backend implements.
- `AgentRuntime`: ABC every agent runtime (Claude Code, archharness, …) implements.
- `Provenance`: 4-tuple sha bundle stamped onto every baseline + result.
- `SubmitOutcome`: enum the connector emits to the agent after every submit.
- `Anonymizer`: end-to-end scrub/translate pair used by the connector.
- `ensure_image`: idempotent docker image loader with digest verification.
"""

from archbench.core.anonymizer import Anonymizer, AnonymizerConfig
from archbench.core.challenge import (
    Challenge,
    EvalConfig,
    RuntimeSpec,
    list_challenges,
    load_challenge,
)
from archbench.core.container import (
    ContainerConfig,
    ContainerDeadError,
    ContainerManager,
    ImageDigestMismatch,
    ImageNotFoundError,
    ensure_image,
    get_image_digest,
)
from archbench.core.outcomes import OutcomeReport, SubmitOutcome
from archbench.core.plugin_base import SimulatorPlugin
from archbench.core.prompt_builder import PromptBuilder
from archbench.core.provenance import (
    Provenance,
    docker_image_digest,
    git_head_commit,
    sha256_of_bytes,
    sha256_of_file,
    sha256_of_json,
)
from archbench.core.runtime_base import AgentRuntime, RuntimeAuth

__all__ = [
    # ABCs
    "SimulatorPlugin",
    "AgentRuntime",
    "RuntimeAuth",
    # Provenance + outcomes
    "Provenance",
    "docker_image_digest",
    "git_head_commit",
    "sha256_of_bytes",
    "sha256_of_file",
    "sha256_of_json",
    "SubmitOutcome",
    "OutcomeReport",
    # Anonymizer
    "Anonymizer",
    "AnonymizerConfig",
    # Challenge
    "Challenge",
    "EvalConfig",
    "RuntimeSpec",
    "load_challenge",
    "list_challenges",
    # Container
    "ContainerConfig",
    "ContainerManager",
    "ContainerDeadError",
    "ImageNotFoundError",
    "ImageDigestMismatch",
    "ensure_image",
    "get_image_digest",
    # Prompt builder
    "PromptBuilder",
]
