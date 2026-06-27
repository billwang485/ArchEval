"""PromptBuilder — assemble prompt.md / system / user messages from a Challenge.

Slimmer than the legacy: no more mode_b / mode_c flags (challenges that
need simulator-source browsing get the tools wired in via the connector,
not the prompt). Anonymization is now an injected `Anonymizer`
instance — no global singleton.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from archbench.core.anonymizer import Anonymizer

if TYPE_CHECKING:
    from archbench.core.challenge import Challenge


class PromptBuilder:
    """Build prompt.md and system / user messages for the agent container."""

    @staticmethod
    def build_prompt_md(
        challenge: Challenge,
        anonymizer: Anonymizer | None = None,
        extra_vars: dict | None = None,
    ) -> str:
        """Return the agent-facing prompt.

        Trimmed in P6: the challenge.yaml `prompt:` field is now the
        single source of truth for the task description. No more
        auto-appended Starter/Evaluation/Workflow sections — those
        belong in the yaml prompt if they belong anywhere.
        """
        anonymizer = anonymizer or Anonymizer.disabled()
        text = _render_variables(challenge.prompt, challenge, extra_vars)
        return anonymizer.scrub_outbound(text)

    @staticmethod
    def build_system_prompt(
        challenge: Challenge,
        anonymizer: Anonymizer | None = None,
        plugin_extra: str = "",
    ) -> str:
        """System prompt for the LLM. Frames the role + tool inventory."""
        anonymizer = anonymizer or Anonymizer.disabled()
        text = (
            f"You are an expert computer-architecture engineer working on a "
            f"{challenge.simulator} simulator challenge.\n\n"
            f"## Tools (provided by the connector)\n"
            f"- `submit()`           — compile + simulate; returns one of four typed outcomes\n"
            f"- `browse_simulator()` — list files in the simulator source tree\n"
            f"- `read_simulator_file()` — read a simulator source file (solution paths blocked)\n"
            f"- `get_challenge_info()` — task prompt + starter files\n\n"
            f"## Workspace layout\n"
            f"- `/workspace/`        — your code; this is what `submit()` ships to the simulator\n"
            f"- `/traces/decoded/`   — decoded workload traces (read-only, may not be present)\n"
            f"- `/api/`              — simulator interface docs (read first)\n\n"
            f"## Rules\n"
            f"- Do not modify class names or method signatures in starter files.\n"
            f"- Only `SIM_OK` outcomes consume your submission budget; other\n"
            f"  outcomes are free retries — but if you get `SIM_TIMEOUT`, change\n"
            f"  approach rather than retry the same code.\n"
            f"{plugin_extra}\n"
        )
        return anonymizer.scrub_outbound(text)


def _render_variables(
    template: str,
    challenge: Challenge,
    extra_vars: dict | None,
) -> str:
    """Replace `{var}` placeholders in a prompt template."""
    variables = {
        "max_submissions": challenge.eval.max_submissions,
    }
    if extra_vars:
        variables.update(extra_vars)
    result = template
    for k, v in variables.items():
        result = result.replace("{" + k + "}", str(v))
    return result
