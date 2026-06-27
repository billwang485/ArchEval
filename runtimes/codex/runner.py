"""CodexRuntime — OpenAI Codex CLI agent.

Off-the-shelf agent (mode: bake_only). The CLI binary is baked into the
image at build time (npm install -g @openai/codex@<pinned-version> in
runtimes/codex/Dockerfile); the per-user auth.json is mounted into the
container at session start.

start_session drives ``codex exec`` headlessly inside the agent container,
mirroring runtimes/claude_code/runner.py (both are CLI agents that connect
to the harness MCP server over streamable-http and stream a JSONL event
trajectory to stdout):

  - model            ← self.model (e.g. gpt-5.5; pushed by session.run_session)
  - reasoning effort ← env ARCHBENCH_CODEX_EFFORT (default "medium")
                       → ``-c model_reasoning_effort=<effort>``
  - harness MCP      ← ``-c mcp_servers.archbench.url="<mcp_url>"`` so the agent
                       can call submit/validate/... (the SAME tools every
                       runtime discovers; codex tracks the server separately
                       and exposes the BARE tool names — submit, browse_simulator
                       — so the connector handler binding matches §1.4).
  - trajectory       ← ``--json`` JSONL events captured to a host file.

Codex's native ``--json`` event stream is the same thread/turn/item schema
mini emits (thread.started / turn.started / item.completed{reasoning,
agent_message, mcp_tool_call, command_execution} / turn.completed), so the
canonical adapter reuses runtimes/mini/trajectory_adapter.py.

Auth is the mounted ~/.codex/auth.json (ChatGPT OAuth tokens or an API key —
NOT LLM_BASE_URL); auth() declares the mount, the same as the verify path.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from archbench.image_management.engine import container_engine
from archbench.core.runtime_base import AgentRuntime, RuntimeAuth

log = logging.getLogger("archbench.runtime.codex")

DEFAULT_VERSION = "0.137.0"
DEFAULT_AUTH_PATH = "~/.codex/auth.json"
DEFAULT_MODEL = "gpt-5.5"
DEFAULT_EFFORT = "medium"
# Reasoning-effort values Codex accepts for model_reasoning_effort. Used to
# fail loud on a typo'd ARCHBENCH_CODEX_EFFORT rather than silently passing a
# value Codex rejects mid-session (§1.9 no silent failure).
VALID_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}


@dataclass
class CodexRuntime(AgentRuntime):
    expected_version_: str = DEFAULT_VERSION
    auth_path: str = DEFAULT_AUTH_PATH
    model: str = DEFAULT_MODEL
    # Post-session-exit diagnostics, consumed by session.run_session's
    # finally block to pick a distinct rc (mirrors ClaudeCodeRuntime).
    last_session_rc: int = 0
    last_session_timed_out: bool = False

    @classmethod
    def from_runtime_spec(cls, spec) -> "CodexRuntime":
        return cls(
            expected_version_=spec.expected_version or DEFAULT_VERSION,
            auth_path=spec.data.get("auth_file", DEFAULT_AUTH_PATH),
            model=spec.model or DEFAULT_MODEL,
        )

    @property
    def name(self) -> str:
        return "codex"

    @property
    def docker_image(self) -> str:
        from archbench.image_management import manifest as images
        return images.fully_qualified("agents", self.name)

    @property
    def expected_version(self) -> str:
        return self.expected_version_

    def auth(self) -> RuntimeAuth:
        host_path = Path(os.path.expanduser(self.auth_path))
        if not host_path.exists():
            raise FileNotFoundError(
                f"Codex auth.json missing at {host_path}. Run `codex login` on host."
            )
        return RuntimeAuth(
            mount_files={host_path: "/home/agent/.codex/auth.json"},
        )

    def _effort(self) -> str:
        """Resolve the reasoning effort. Env override (ARCHBENCH_CODEX_EFFORT),
        default 'medium'. Raises on an unrecognized value — fail loud, not a
        silent mid-session reject (§1.9)."""
        effort = (os.environ.get("ARCHBENCH_CODEX_EFFORT") or DEFAULT_EFFORT).strip()
        if effort not in VALID_EFFORTS:
            raise ValueError(
                f"ARCHBENCH_CODEX_EFFORT={effort!r} is not a valid Codex "
                f"reasoning effort; expected one of {sorted(VALID_EFFORTS)}"
            )
        return effort

    def verify_runtime(self, challenge_dir: Path) -> list[str]:
        errors = []
        host_path = Path(os.path.expanduser(self.auth_path))
        if not host_path.exists():
            errors.append(f"codex auth.json missing: {host_path}")
        # Validate effort host-side too so a typo is caught at preflight, not
        # only when start_session builds the exec command.
        try:
            self._effort()
        except ValueError as e:
            errors.append(str(e))
        return errors

    def verify_in_container(self, agent) -> list[str]:
        self._ensure_verify_script(agent, "codex")
        out, rc = agent.exec(
            f"EXPECTED_CODEX_VERSION={self.expected_version} /work/verify.sh",
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
        """Run ``codex exec`` inside the agent container via docker exec.

        The Codex CLI is baked into the image at /usr/local/bin/codex
        (npm-installed at build time; mode: bake_only). Per-user auth.json is
        copied into /home/agent/.codex/auth.json at session start (see the
        auth section below for why copy_in, not the declared mount). The
        harness MCP server is wired in as a streamable-http server so the
        agent can call submit/validate/... ; the JSONL event stream is
        captured to a trajectory file.

        Returns the host path to the trajectory.
        """
        import subprocess
        import threading
        import time
        import uuid

        # Fail loud if the codex binary isn't in the image (no silent default).
        out, rc = agent.exec(
            "test -x /usr/local/bin/codex && echo OK || echo MISS", timeout=10,
        )
        if "OK" not in out:
            raise RuntimeError(
                f"CodexRuntime: /usr/local/bin/codex missing in {self.docker_image}. "
                "Rebuild image from runtimes/codex/Dockerfile."
            )

        # Auth: copy the per-user auth.json INTO the container at session
        # start. The AgentRuntime.auth() contract returns mount_files, but the
        # current session.run_session wires only env_vars (mini/archharness's
        # proxy creds) and claude_code reads its env var directly — mount_files
        # is NOT consumed by the orchestrator. So a mount-only auth would never
        # reach the container and codex would fail to authenticate. We copy_in
        # the file (fail-fast via auth() if it's missing on the host) to
        # /home/agent/.codex/auth.json. copy_in (not a read-only mount) also
        # lets codex refresh + write back its ChatGPT OAuth access_token
        # in-container without a stale-token failure mid-session.
        rt_auth = self.auth()  # raises FileNotFoundError if auth.json missing
        host_auth, ctr_auth = next(iter(rt_auth.mount_files.items()))
        effort = self._effort()  # raises on bad value
        agent.exec(f"mkdir -p {os.path.dirname(ctr_auth)}", timeout=10)
        agent.copy_in(host_auth, ctr_auth)

        # Re-assert ownership defensively (docker exec defaults to root; the
        # agent loop runs as `agent`, and the copy_in above lands as root).
        # Mirrors claude_code/mini.
        agent.exec(
            "chown -R agent:agent /workspace /api /traces /home/agent "
            "2>/dev/null || true",
            timeout=15,
        )

        # Persist the prompt to /workspace so the agent can re-read it; codex
        # takes the prompt as a positional arg (the first user message).
        agent.write_file("prompt.md", prompt, base_dir="/workspace")

        trajectory_host_path = Path(
            f"/tmp/archbench_codex_trajectory_{int(time.time())}_{uuid.uuid4().hex[:8]}.jsonl"
        )
        log.info(
            "CodexRuntime: launching codex exec (model=%s, effort=%s, timeout=%ds)",
            self.model, effort, round_timeout,
        )

        # docker exec — bypass agent.exec because we want streaming stdout
        # capture (not a fixed-size buffer) for the JSONL trajectory.
        #
        # Flags (codex 0.137.0; confirmed end-to-end against a streamable-http
        # FastMCP server with gpt-5.5):
        #   exec <PROMPT>                       headless / non-interactive
        #   --json                              JSONL event stream to stdout
        #   -m <model>                          model id
        #   -c model_reasoning_effort=<effort>  reasoning effort
        #   -c mcp_servers.archbench.url=...    harness MCP over streamable-http
        #   --skip-git-repo-check               /workspace is not a git repo
        #   --dangerously-bypass-approvals-and-sandbox
        #       the container IS the sandbox (network=host, per-run, rm -f); we
        #       want the agent to write files + run commands without prompts.
        #   -C /workspace                       working root
        #   -o <FILE>                           last agent message (diagnostic)
        cmd = [
            container_engine(), "exec",
            "--user", "agent",
            "-w", "/workspace",
            "-e", "HOME=/home/agent",
            "-e", "CODEX_HOME=/home/agent/.codex",
            agent.name,
            "/usr/local/bin/codex", "exec", prompt,
            "--json",
            "-m", self.model,
            "-c", f"model_reasoning_effort={effort}",
            "-c", f'mcp_servers.archbench.url="{mcp_url}"',
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C", "/workspace",
            "-o", "/workspace/codex_last_message.txt",
        ]

        with open(trajectory_host_path, "w") as traj_fh:
            proc = subprocess.Popen(
                cmd, stdout=traj_fh, stderr=subprocess.PIPE, text=True,
                stdin=subprocess.DEVNULL,  # prompt is a positional arg; don't block on stdin
            )
            stderr_chunks: list[str] = []

            def _drain():
                try:
                    assert proc.stderr is not None
                    for line in proc.stderr:
                        stderr_chunks.append(line)
                except Exception:
                    pass

            t = threading.Thread(target=_drain, daemon=True)
            t.start()
            timed_out = False
            try:
                rc = proc.wait(timeout=round_timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = -1
                timed_out = True
            t.join(timeout=5)

        if rc not in (0, None):
            log.warning(
                "codex exited rc=%d; stderr tail:\n%s",
                rc, "".join(stderr_chunks)[-2000:],
            )
        # Expose post-exit state to session.run_session so it can pick a
        # distinct rc (round_timeout fired vs non-zero codex exit), mirroring
        # ClaudeCodeRuntime.
        self.last_session_rc = rc if rc is not None else -1
        self.last_session_timed_out = timed_out
        return trajectory_host_path

    def to_canonical_trajectory(self, native_trajectory_path) -> list[dict]:
        """Codex's native ``--json`` events are the same thread/turn/item schema
        mini emits; reuse the mini adapter (the anti-corruption layer under this
        runtime). See runtimes/mini/trajectory_adapter.py and
        archbench/core/trajectory.py for the canonical schema."""
        from runtimes.mini.trajectory_adapter import to_canonical
        return to_canonical(native_trajectory_path)
