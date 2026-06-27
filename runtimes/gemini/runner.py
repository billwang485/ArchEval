"""GeminiRuntime — Google Gemini CLI agent.

Off-the-shelf agent (mode: bake_only). The CLI binary is baked into the
image at build time (npm install -g @google/gemini-cli@<pinned-version>
in runtimes/gemini/Dockerfile); the per-user OAuth credentials are
docker-cp'd into the container at session start.

Mirror of CodexRuntime in structure; different binary + auth files.
start_session is stubbed pending the Gemini stream parser port.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from archbench.core.runtime_base import AgentRuntime, RuntimeAuth

log = logging.getLogger("archbench.runtime.gemini")

DEFAULT_VERSION = "0.38.1"
DEFAULT_OAUTH_PATH = "~/.gemini/oauth_creds.json"
DEFAULT_ACCOUNTS_PATH = "~/.gemini/google_accounts.json"


@dataclass
class GeminiRuntime(AgentRuntime):
    expected_version_: str = DEFAULT_VERSION
    oauth_path: str = DEFAULT_OAUTH_PATH
    accounts_path: str = DEFAULT_ACCOUNTS_PATH
    model: str = "gemini-3-pro-preview"

    @classmethod
    def from_runtime_spec(cls, spec) -> "GeminiRuntime":
        auth_files = spec.data.get("auth_files") or []
        oauth_path = next(
            (a for a in auth_files if a.endswith("oauth_creds.json")),
            DEFAULT_OAUTH_PATH,
        )
        accounts_path = next(
            (a for a in auth_files if a.endswith("google_accounts.json")),
            DEFAULT_ACCOUNTS_PATH,
        )
        return cls(
            expected_version_=spec.expected_version or DEFAULT_VERSION,
            oauth_path=oauth_path,
            accounts_path=accounts_path,
            model=spec.model or "gemini-3-pro-preview",
        )

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def docker_image(self) -> str:
        from archbench.image_management import manifest as images
        return images.fully_qualified("agents", self.name)

    @property
    def expected_version(self) -> str:
        return self.expected_version_

    def auth(self) -> RuntimeAuth:
        mounts = {}
        for host_str, ctr_path in (
            (self.oauth_path, "/home/agent/.gemini/oauth_creds.json"),
            (self.accounts_path, "/home/agent/.gemini/google_accounts.json"),
        ):
            host_path = Path(os.path.expanduser(host_str))
            if not host_path.exists():
                raise FileNotFoundError(
                    f"Gemini auth file missing: {host_path}. Run `gemini auth` on host."
                )
            mounts[host_path] = ctr_path
        return RuntimeAuth(mount_files=mounts)

    def verify_runtime(self, challenge_dir: Path) -> list[str]:
        errors = []
        for host_str in (self.oauth_path, self.accounts_path):
            host_path = Path(os.path.expanduser(host_str))
            if not host_path.exists():
                errors.append(f"missing gemini auth file: {host_path}")
        return errors

    def verify_in_container(self, agent) -> list[str]:
        self._ensure_verify_script(agent, "gemini")
        out, rc = agent.exec(
            f"EXPECTED_GEMINI_VERSION={self.expected_version} /work/verify.sh",
            timeout=30,
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
            "GeminiRuntime.start_session not yet ported. P4 ships the verify "
            "path; the full Gemini stream parser is a follow-up. Until then, "
            "use the legacy Gemini runner."
        )
