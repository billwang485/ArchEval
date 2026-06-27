"""Registered agent runtimes.

Each runtime ships an AgentRuntime subclass + a docker image + a
verify.sh baked into the image. Runner code uses get_runtime(name)
to instantiate; load_challenge applies challenge.yaml `runtimes:`
spec via `<Runtime>.from_runtime_spec(spec)`.

See `docs/adding_an_agent.md` for the walkthrough.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from runtimes.archharness import ArchharnessRuntime
from runtimes.claude_code import ClaudeCodeRuntime
from runtimes.codex import CodexRuntime
from runtimes.gemini import GeminiRuntime
from runtimes.mini import MiniRuntime

if TYPE_CHECKING:
    from archbench.core.runtime_base import AgentRuntime


_REGISTRY: dict[str, type] = {
    "claude_code":     ClaudeCodeRuntime,
    "codex":           CodexRuntime,
    "gemini":          GeminiRuntime,
    "archharness":     ArchharnessRuntime,
    "mini":            MiniRuntime,
}


def get_runtime(name: str) -> AgentRuntime:
    if name not in _REGISTRY:
        raise KeyError(
            f"No agent runtime registered for {name!r}. "
            f"Registered: {sorted(_REGISTRY.keys())}"
        )
    return _REGISTRY[name]()


def runtime_from_challenge(name: str, challenge) -> AgentRuntime:
    """Build a runtime tailored to this challenge (model + version pinned from yaml)."""
    spec = challenge.runtime_for(name)
    cls = _REGISTRY[name]
    if hasattr(cls, "from_runtime_spec"):
        return cls.from_runtime_spec(spec)
    return cls()
