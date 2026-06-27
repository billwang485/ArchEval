"""ClaudeCodeRuntime — Claude Code CLI agent.

The Claude binary is OAuth-bound to a specific user account. The image
itself is shared (mode: bake_only): OAuth token is docker-cp'd at session
start (per-user); the Claude binary is baked into the image at build time
via npm (see runtimes/claude_code/Dockerfile).

What's pinned:
  - Binary version (from challenge.yaml runtime.runtime_version) — asserted
    against `claude --version` inside the container at verify-in-container.
  - OAuth token file (challenge.yaml runtime.oauth_token_file).
  - Model id (e.g. claude-opus-4-7) — passed via env to claude -p.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from archbench.image_management.engine import container_engine
from archbench.core.runtime_base import AgentRuntime, RuntimeAuth

if False:  # TYPE_CHECKING
    from archbench.core.container import ContainerManager

log = logging.getLogger("archbench.runtime.claude_code")

DEFAULT_VERSION = "2.1.170"
DEFAULT_OAUTH_TOKEN_PATH = "~/.config/archbench/oauth_token"


@dataclass
class ClaudeCodeRuntime(AgentRuntime):
    """Claude Code runtime. State: pinned version, OAuth token, model id."""

    expected_version_: str = DEFAULT_VERSION
    oauth_token_path: str = DEFAULT_OAUTH_TOKEN_PATH
    model: str = "claude-opus-4-7"
    # Post-session-exit diagnostics, consumed by session.run_session's
    # finally block to pick a distinct rc (Bug 5 fix).
    last_session_rc: int = 0
    last_session_timed_out: bool = False

    @classmethod
    def from_runtime_spec(cls, spec) -> "ClaudeCodeRuntime":
        """Build from challenge.yaml runtimes.claude_code block."""
        return cls(
            expected_version_=spec.expected_version or DEFAULT_VERSION,
            oauth_token_path=spec.data.get("oauth_token_file", DEFAULT_OAUTH_TOKEN_PATH),
            model=spec.model or "claude-opus-4-7",
        )

    @property
    def name(self) -> str:
        return "claude_code"

    @property
    def docker_image(self) -> str:
        from archbench.image_management import manifest as images
        return images.fully_qualified("agents", self.name)

    @property
    def expected_version(self) -> str:
        return self.expected_version_

    # ---- preflight ---------------------------------------------------------

    def auth(self) -> RuntimeAuth:
        token_path = Path(os.path.expanduser(self.oauth_token_path))
        if not token_path.exists():
            raise FileNotFoundError(
                f"Claude OAuth token missing at {token_path}. "
                "Create it with `claude setup-token` and chmod 600."
            )
        return RuntimeAuth(
            env_vars={"CLAUDE_CODE_OAUTH_TOKEN": token_path.read_text().strip()},
        )

    def verify_runtime(self, challenge_dir: Path) -> list[str]:
        """Host-side preflight: OAuth token present and not world-readable.

        The Claude binary used to live on the host under
        ~/.local/share/claude/versions/<v>/ and get docker-cp'd in at
        session start (217 MB; NFS read frequently exceeded the 60s docker
        cp timeout). It's now baked into the image at build time (mode:
        bake_only), so the only per-user artifact left to check host-side
        is the OAuth token. Binary version is asserted inside the
        container by verify.sh (real provenance check against the
        baked-in binary).
        """
        errors: list[str] = []
        token_path = Path(os.path.expanduser(self.oauth_token_path))
        if not token_path.exists():
            errors.append(f"oauth token missing: {token_path}")
        elif token_path.stat().st_mode & 0o077:
            errors.append(
                f"oauth token {token_path} is world-readable; chmod 600"
            )
        return errors

    def verify_in_container(self, agent) -> list[str]:
        """In-container: run /work/verify.sh.

        The script asserts the baked-in Claude binary exists at
        /usr/local/bin/claude and reports the pinned version
        (matches info.yaml runtime_version). This is now a real
        provenance check against the image — no deferral to start_session.
        """
        self._ensure_verify_script(agent, "claude_code")
        out, rc = agent.exec("/work/verify.sh", timeout=30)
        errors = []
        if rc != 0 or "VERIFY_OK" not in out:
            errors.extend(
                line.strip() for line in out.splitlines()
                if "CHECK_FAILED" in line or "VERIFY_FAILED" in line
            )
        return errors

    # ---- session -----------------------------------------------------------

    def start_session(
        self, agent, mcp_url: str, prompt: str, round_timeout: int,
    ) -> Path:
        """Run `claude -p` inside agent via docker exec.

        The Claude binary is baked into the image at /usr/local/bin/claude
        (npm-installed at build time; mode: bake_only). OAuth token comes
        via env (per-user, runtime-injected — only host artifact left).
        MCP config passed as JSON string with type: http (matches our
        FastMCP streamable-http transport at /mcp). Stream-json output →
        trajectory file.
        """
        import json as _json
        import subprocess as _sp
        import threading as _th
        import time as _time
        import uuid as _uuid

        # 1. The Dockerfile creates `agent` (uid 1000) and chowns /workspace
        # /api /traces /home/agent. Re-assert ownership defensively in case
        # stage_workspace wrote files as root (docker exec defaults to root).
        agent.exec(
            "chown -R agent:agent /workspace /api /traces /home/agent "
            "2>/dev/null || true",
            timeout=15,
        )

        # 2. Resolve OAuth token (fail-fast if missing — auth() raises FNF).
        # The Claude binary is baked into the image; only the per-user
        # OAuth token comes from the host at session start.
        auth = self.auth()
        oauth_token = auth.env_vars["CLAUDE_CODE_OAUTH_TOKEN"]

        # 3. MCP config as JSON string, NOT file. type=http matches our
        # FastMCP streamable-http transport on /mcp.
        mcp_config = _json.dumps({
            "mcpServers": {
                "archbench": {"type": "http", "url": mcp_url}
            }
        })

        # 4. Persist prompt as text file (claude takes it via positional
        # arg, but writing it to /workspace/prompt.md helps the agent
        # re-read it mid-session if needed).
        agent.write_file("prompt.md", prompt, base_dir="/workspace")

        trajectory_host_path = Path(
            f"/tmp/archbench_claude_trajectory_{int(_time.time())}_{_uuid.uuid4().hex[:8]}.jsonl"
        )
        log.info(
            "ClaudeCodeRuntime: launching claude -p (model=%s, timeout=%ds)",
            self.model, round_timeout,
        )

        # 5. docker exec — bypass agent.exec because we want streaming stdout
        # capture (not a fixed-size buffer) for stream-json trajectory.
        cmd = [
            container_engine(), "exec",
            "--user", "agent",
            "-w", "/workspace",
            "-e", "HOME=/home/agent",
            "-e", f"CLAUDE_CODE_OAUTH_TOKEN={oauth_token}",
            "-e", "MCP_TIMEOUT=1800000",  # 30-min ceiling for submit() calls
            "-e", f"ANTHROPIC_MODEL={self.model}",
            agent.name,
            "/usr/local/bin/claude", "-p", prompt,
            "--mcp-config", mcp_config,
            "--strict-mcp-config",
            "--model", self.model,
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]

        with open(trajectory_host_path, "w") as traj_fh:
            proc = _sp.Popen(cmd, stdout=traj_fh, stderr=_sp.PIPE, text=True)
            stderr_chunks: list[str] = []

            def _drain():
                try:
                    assert proc.stderr is not None
                    for line in proc.stderr:
                        stderr_chunks.append(line)
                except Exception:
                    pass

            t = _th.Thread(target=_drain, daemon=True)
            t.start()
            timed_out = False
            try:
                rc = proc.wait(timeout=round_timeout)
            except _sp.TimeoutExpired:
                proc.kill()
                rc = -1
                timed_out = True
            t.join(timeout=5)
        if rc not in (0, None):
            log.warning(
                "claude exited rc=%d; stderr tail:\n%s",
                rc, "".join(stderr_chunks)[-2000:],
            )
        # Expose post-exit state to session.run_session so it can pick
        # a distinct rc (rc=7 non-zero claude, rc=8 round_timeout fired).
        self.last_session_rc = rc if rc is not None else -1
        self.last_session_timed_out = timed_out
        return trajectory_host_path

    def to_canonical_trajectory(self, native_trajectory_path) -> list[dict]:
        """ADAPTER STUB — the conversion layer for claude_code (not yet written).

        claude_code's native trajectory is the Anthropic stream-json format:
        lines with ``type`` ∈ {assistant, user, system}; an assistant
        ``message.content`` is a list of typed blocks — ``thinking``, ``text``,
        and ``tool_use`` {name, input} — and the tool result comes back as the
        next ``user`` message's ``tool_result`` block. To wire claude_code into
        the canonical telemetry, map each assistant turn to a canonical step
        (``archbench.core.trajectory.step``): thinking/text -> thinking; a
        ``tool_use`` -> action (kind 'submit' when name=='submit', else
        'tool_call'); the matching ``tool_result`` -> observation.

        Until this is written, claude_code runs still produce the native
        trajectory.jsonl, but session.py logs 'no canonical adapter' and skips
        trajectory.canonical.jsonl (the trajectory_audit evaluator then reports
        it needs an adapter). That is the loud, intended signal — see
        archbench/core/trajectory.py for the target schema.
        """
        raise NotImplementedError(
            "claude_code has no to_canonical_trajectory adapter yet; see this "
            "method's docstring + archbench/core/trajectory.py for the schema"
        )

