"""MiniRuntime — miniswe-style Python loop, sibling of ArchharnessRuntime.

Same overall shape: agent code baked at /opt/mini/main.py, talks to
host MCP, uses local Gemma via vLLM. Differs in main-loop strategy
(linear single-prompt vs archharness's plan-model-act-observe).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from archbench.image_management.engine import container_engine
from archbench.core.runtime_base import AgentRuntime, RuntimeAuth

log = logging.getLogger("archbench.runtime.mini")

DEFAULT_VERSION = "v6"
DEFAULT_MODEL = "google/gemma-4-31B-it"


@dataclass
class MiniRuntime(AgentRuntime):
    expected_version_: str = DEFAULT_VERSION
    model: str = DEFAULT_MODEL
    llm_base_url: Optional[str] = None

    @classmethod
    def from_runtime_spec(cls, spec) -> "MiniRuntime":
        return cls(
            expected_version_=spec.expected_version or DEFAULT_VERSION,
            model=spec.model or DEFAULT_MODEL,
            llm_base_url=spec.data.get("llm_base_url"),
        )

    @property
    def name(self) -> str:
        return "mini"

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
        # external/standalone use of MiniRuntime.
        url = self.llm_base_url or os.environ.get("LLM_BASE_URL", "")
        if not url:
            raise RuntimeError(
                "MiniRuntime requires LLM_BASE_URL (proxy URL set by "
                "session.run_session, or LLM_BASE_URL env). "
                "No silent fallback — set it explicitly."
            )
        # The proxy doesn't authenticate; LLM_API_KEY is a marker the
        # OpenAI client lib won't complain about. If the user explicitly
        # exports LLM_API_KEY (e.g. bypassing the proxy to hit a real
        # vendor URL), that wins.
        return RuntimeAuth(
            env_vars={
                "LLM_BASE_URL": url,
                "LLM_API_KEY": os.environ.get("LLM_API_KEY", "local-proxy-token"),
                "LLM_MODEL": self.model,
            },
            endpoint_url=url,
        )

    def verify_runtime(self, challenge_dir: Path) -> list[str]:
        # Host-side pre-flight runs BEFORE session.run_session has had a
        # chance to start the proxy (and so cannot set llm_base_url yet).
        # We accept "no URL yet" as a soft pass; verify_in_container, which
        # runs AFTER the proxy is up, is the hard checkpoint.
        return []

    def verify_in_container(self, agent) -> list[str]:
        self._ensure_verify_script(agent, "mini")
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
        """Run /opt/mini/main.py inside the agent container.

        mini_runtime is baked into agent-mini:v6 by runtimes/mini/
        Dockerfile (COPY the legacy mini runtime into /opt/mini/).
        We just docker-exec it with the right env + CLI args and capture
        its stream-json stdout to a trajectory file.

        Returns the host path to the trajectory.
        """
        import subprocess
        import threading
        import time
        import uuid

        # Ensure mini runtime is in the image (no silent default — fail loud
        # if image was built differently)
        out, rc = agent.exec(
            "test -x /opt/mini/main.py && echo OK || echo MISS", timeout=10,
        )
        if "OK" not in out:
            raise RuntimeError(
                f"MiniRuntime: /opt/mini/main.py missing in {self.docker_image}. "
                "Rebuild image from runtimes/mini/Dockerfile."
            )

        # --dev override: copy the host's runtimes/mini/src/ over the
        # baked /opt/mini/ so iterations on the runtime code don't need
        # a full image rebuild. Implements the dev_capable contract
        # declared in runtimes/mini/info.yaml. Used today to bypass a
        # stale legacy agent-mini:v6 tarball that bakes a pre-Phase-B
        # main.py with hardcoded `{"wait": True}` submits.
        if getattr(self, "dev_mode", False):
            host_src = Path(__file__).resolve().parent / "src"
            if host_src.is_dir():
                for f in sorted(host_src.iterdir()):
                    if f.is_file():
                        agent.copy_in(f, f"/opt/mini/{f.name}")
                agent.exec("chmod +x /opt/mini/main.py", timeout=10)
                log.info("MiniRuntime: --dev → copied host src/ to /opt/mini/")
            else:
                log.warning(
                    "MiniRuntime: --dev requested but host src/ not at %s",
                    host_src,
                )

        # Resolve auth + env
        rt_auth = self.auth()  # raises if LLM_BASE_URL unset
        llm_base_url = rt_auth.env_vars["LLM_BASE_URL"]
        llm_api_key = rt_auth.env_vars["LLM_API_KEY"]
        llm_model = rt_auth.env_vars["LLM_MODEL"]
        llm_temperature = os.environ.get("ARCHBENCH_TEMPERATURE", "0.0")

        # (P6: no prompt.md written to /workspace/ — mini takes prompt via
        # --prompt CLI arg → first user message. Filesystem prompt would
        # be dead weight for this runtime.)
        agent.exec(
            "chown -R agent:agent /workspace /home/agent 2>/dev/null || true",
            timeout=10,
        )

        # UNIQUE per run: second-resolution names collided when a campaign
        # launched cells concurrently — two runners shared the SAME /tmp file
        # (mode "w") and interleaved/clobbered each other's stream, corrupting
        # trajectory-derived artifacts for 13 wave-1 cells (lessons §26).
        trajectory_path = Path(
            f"/tmp/archbench_mini_trajectory_{int(time.time())}_{uuid.uuid4().hex[:8]}.jsonl"
        )
        log.info("MiniRuntime: launching /opt/mini/main.py (model=%s, max_turns=200, timeout=%ds)",
                 llm_model, round_timeout)

        cmd = [
            container_engine(), "exec",
            "--user", "agent",
            "--workdir", "/workspace",
            "-e", "HOME=/home/agent",
            "-e", f"LLM_BASE_URL={llm_base_url}",
            "-e", f"LLM_API_KEY={llm_api_key}",
            "-e", f"LLM_MODEL={llm_model}",
            # smoke harness pass-through: when set, /opt/mini runs the
            # deterministic NoopAgent instead of the LLM loop.
            *( ["-e", f"ARCHBENCH_NOOP_SUBMIT={os.environ['ARCHBENCH_NOOP_SUBMIT']}"]
               if os.environ.get("ARCHBENCH_NOOP_SUBMIT") else [] ),
            "-e", f"ARCHBENCH_TEMPERATURE={llm_temperature}",
            agent.name,
            "python3", "/opt/mini/main.py",
            "--prompt", prompt,
            "--mcp-url", mcp_url,
            # Submit budget = the challenge's eval.max_submissions (L1=10, L2=5,
            # L3=1), pushed onto the runtime by session.run_session. Defaults to
            # 1 only if unset. The old hardcoded "1" silently collapsed every
            # tier to a single submit (the single-sim early-stop fires at
            # submits_done >= max_submits), defeating the L1/L2/L3 ablation.
            "--max-submits", str(int(getattr(self, "max_submits", 1) or 1)),
            "--max-turns", "200",
            "--timeout", str(round_timeout),
        ]
        with open(trajectory_path, "w") as traj_fh:
            proc = subprocess.Popen(
                cmd,
                stdout=traj_fh,
                stderr=subprocess.PIPE,
                text=True,
            )
            stderr_chunks: list[str] = []

            def _drain_stderr():
                try:
                    assert proc.stderr is not None
                    for line in proc.stderr:
                        stderr_chunks.append(line)
                except Exception:
                    pass

            t = threading.Thread(target=_drain_stderr, daemon=True)
            t.start()
            try:
                rc = proc.wait(timeout=round_timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = -1
            t.join(timeout=5)

        if rc not in (0, None):
            log.warning(
                "mini agent exited rc=%d; stderr tail:\n%s",
                rc, "".join(stderr_chunks)[-2000:],
            )
        return trajectory_path

    def to_canonical_trajectory(self, native_trajectory_path) -> list[dict]:
        """mini-swe thread/turn/item events -> canonical (thinking/action/
        observation) steps. The conversion layer under this agent; see
        runtimes/mini/trajectory_adapter.py."""
        from runtimes.mini.trajectory_adapter import to_canonical
        return to_canonical(native_trajectory_path)
