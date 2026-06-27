"""ArchharnessRuntime — local Gemma 4 via vLLM, in-container Python loop.

Unlike claude/codex/gemini (which wrap vendor CLIs), the agent loop is
*baked* into agent-archharness:v6 as /opt/archharness/main.py. The runtime
class here just starts the container with the right env vars (LLM_BASE_URL,
LLM_API_KEY, LLM_MODEL) and runs main.py.

verify_in_container ALSO probes the vLLM endpoint — the structural fix
for lessons_learned §7 (legacy archharness skipped the probe and only
discovered a dead Gemma server on the first inference call).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from archbench.core.runtime_base import AgentRuntime, RuntimeAuth

log = logging.getLogger("archbench.runtime.archharness")

DEFAULT_VERSION = "v6"
DEFAULT_MODEL = "google/gemma-4-31B-it"


@dataclass
class ArchharnessRuntime(AgentRuntime):
    expected_version_: str = DEFAULT_VERSION
    model: str = DEFAULT_MODEL
    llm_base_url: Optional[str] = None  # If None, taken from env at session start

    @classmethod
    def from_runtime_spec(cls, spec) -> "ArchharnessRuntime":
        return cls(
            expected_version_=spec.expected_version or DEFAULT_VERSION,
            model=spec.model or DEFAULT_MODEL,
            llm_base_url=spec.data.get("llm_base_url"),
        )

    @property
    def name(self) -> str:
        return "archharness"

    @property
    def docker_image(self) -> str:
        from archbench.image_management import manifest as images
        return images.fully_qualified("agents", self.name)

    @property
    def expected_version(self) -> str:
        return self.expected_version_

    def auth(self) -> RuntimeAuth:
        # `llm_base_url` is normally set by session.run_session, which
        # starts the host-side proxy on a free port and points the
        # runtime at it. Explicit env override is still honored for
        # external/standalone use of ArchharnessRuntime.
        url = self.llm_base_url or os.environ.get("LLM_BASE_URL", "")
        if not url:
            raise RuntimeError(
                "ArchharnessRuntime requires LLM_BASE_URL (proxy URL set by "
                "session.run_session, or LLM_BASE_URL env). "
                "No silent fallback — set it explicitly."
            )
        # The proxy doesn't authenticate; LLM_API_KEY is a marker the
        # OpenAI client lib won't complain about. Explicit env override
        # wins so the runtime can hit a real vendor URL directly.
        return RuntimeAuth(
            env_vars={
                "LLM_BASE_URL": url,
                "LLM_API_KEY": os.environ.get("LLM_API_KEY", "local-proxy-token"),
                "LLM_MODEL": self.model,
            },
            endpoint_url=url,
        )

    def verify_runtime(self, challenge_dir: Path) -> list[str]:
        """Host-side pre-flight: nothing strict here.

        verify_runtime runs BEFORE session.run_session has had a chance
        to start the proxy and set llm_base_url. The hard endpoint probe
        happens in verify_in_container, which runs AFTER the proxy is up.
        """
        return []

    def verify_in_container(self, agent) -> list[str]:
        self._ensure_verify_script(agent, "archharness")
        env_inject = ""
        url = self.llm_base_url or os.environ.get("LLM_BASE_URL")
        if url:
            env_inject = f"LLM_BASE_URL={url} "
        out, rc = agent.exec(
            f"{env_inject}/work/verify.sh", timeout=30,
        )
        if rc == 0 and "VERIFY_OK" in out:
            return []
        return [
            line.strip() for line in out.splitlines()
            if "CHECK_FAILED" in line or "VERIFY_FAILED" in line
        ] or [f"verify.sh rc={rc}: {out[:500]}"]

    def start_session(
        self, agent, mcp_url: str, prompt: str, round_timeout: int,
    ) -> Path:
        raise NotImplementedError(
            "ArchharnessRuntime.start_session not yet ported. P4 ships the "
            "verify path with vLLM endpoint probe (lessons §7). Full session "
            "loop port lives in a follow-up; until then, use legacy "
            "legacy archharness runner."
        )
