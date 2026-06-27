"""Unit tests for archbench.runtimes.session._check_baseline_provenance.

Sub-agent review caught: original impl only compared image_digest, not
all 4 sha fields. Now we compare all 4. These tests lock that in.

Also tests the lesson §1 invariant: baseline without `provenance` block
is a HARD RED (the legacy state), not a silent pass.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from archbench.core.challenge import Challenge, EvalConfig
from archbench.core.provenance import Provenance, sha256_of_bytes, sha256_of_file
from archbench.runtimes.session import _check_baseline_provenance


def _baseline(prov_dict: dict | None, per_trace: list[dict] | None = None) -> dict:
    return {
        "policy": "LRU@32KB",
        "average_ipc": 0.3373,
        "per_trace": per_trace or [],
        **({"provenance": prov_dict} if prov_dict is not None else {}),
    }


def _ch(challenge_dir: Path) -> Challenge:
    return Challenge(
        id="t", name="t", simulator="champsim", prompt="",
        starter_files=[], output_files=[],
        eval=EvalConfig(baseline_file="baseline.json"),
        simulator_config={},
        challenge_dir=challenge_dir,
    )


# Helper to materialize a challenge dir on disk and stamp matching provenance
def _setup_challenge(tmp_path: Path, config_text: str = '{"LLC":{"sets":2048}}',
                     starter_files: dict | None = None,
                     trace_files: dict | None = None) -> tuple[Path, Provenance]:
    starter_files = starter_files or {"main.cc": "int main(){}\n"}
    trace_files = trace_files or {}

    (tmp_path / "config.json").write_text(config_text)
    starter_dir = tmp_path / "starter"
    starter_dir.mkdir()
    for name, content in starter_files.items():
        (starter_dir / name).write_text(content)

    subtraces_dir = tmp_path / "subtraces"
    if trace_files:
        subtraces_dir.mkdir()
        for name, content in trace_files.items():
            (subtraces_dir / name).write_bytes(content)

    # Compute matching provenance (with placeholder image_digest)
    config_sha = sha256_of_file(tmp_path / "config.json")
    starter_sha = sha256_of_bytes(b"".join(
        f.name.encode() + b":" + sha256_of_file(f).encode() + b"\n"
        for f in sorted(starter_dir.iterdir())
    ))
    if trace_files:
        trace_sha = sha256_of_bytes(b"".join(
            tn.encode() + b":" + sha256_of_file(subtraces_dir / tn).encode() + b"\n"
            for tn in sorted(trace_files.keys())
        ))
    else:
        trace_sha = "00" * 32
    prov = Provenance(
        image_digest="DIGEST_IMAGE_LIVE",
        config_sha256=config_sha,
        starter_sha256=starter_sha,
        trace_sha256=trace_sha,
        harness_commit="abc123",
    )
    return tmp_path, prov


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_matching_provenance_returns_empty(tmp_path):
    challenge_dir, prov = _setup_challenge(tmp_path)
    (challenge_dir / "baseline.json").write_text(json.dumps(_baseline(prov.to_dict())))
    drifts = _check_baseline_provenance(_ch(challenge_dir), "DIGEST_IMAGE_LIVE")
    assert drifts == []


# ---------------------------------------------------------------------------
# Critical fix: unstamped baseline → HARD RED (sub-agent finding §1 partial)
# ---------------------------------------------------------------------------


def test_baseline_without_provenance_is_hard_red(tmp_path):
    """Past impl silently passed. Now: explicit RED, no comparison possible."""
    challenge_dir, _ = _setup_challenge(tmp_path)
    (challenge_dir / "baseline.json").write_text(json.dumps(_baseline(None)))
    drifts = _check_baseline_provenance(_ch(challenge_dir), "ANY_DIGEST")
    assert any("missing the `provenance` block" in d for d in drifts)


# ---------------------------------------------------------------------------
# Critical fix: all 4 sha fields are now compared, not just image_digest
# ---------------------------------------------------------------------------


def test_drift_image_digest(tmp_path):
    challenge_dir, prov = _setup_challenge(tmp_path)
    (challenge_dir / "baseline.json").write_text(json.dumps(_baseline(prov.to_dict())))
    drifts = _check_baseline_provenance(_ch(challenge_dir), "DIFFERENT_DIGEST")
    assert any("image_digest" in d for d in drifts)


def test_drift_config_sha(tmp_path):
    """Mutate config.json after stamping → config_sha drift caught."""
    challenge_dir, prov = _setup_challenge(tmp_path)
    (challenge_dir / "baseline.json").write_text(json.dumps(_baseline(prov.to_dict())))
    (challenge_dir / "config.json").write_text('{"LLC":{"sets":4096}}')  # mutate
    drifts = _check_baseline_provenance(_ch(challenge_dir), "DIGEST_IMAGE_LIVE")
    assert any("config_sha256" in d for d in drifts), (
        f"config sha drift NOT caught — runner would silently report against "
        f"a stale baseline. drifts={drifts}"
    )


def test_drift_starter_sha(tmp_path):
    """Edit starter file → starter_sha drift caught."""
    challenge_dir, prov = _setup_challenge(tmp_path)
    (challenge_dir / "baseline.json").write_text(json.dumps(_baseline(prov.to_dict())))
    (challenge_dir / "starter" / "main.cc").write_text("// edited\nint main(){}\n")
    drifts = _check_baseline_provenance(_ch(challenge_dir), "DIGEST_IMAGE_LIVE")
    assert any("starter_sha256" in d for d in drifts)


def test_drift_trace_sha(tmp_path):
    """Mutate a trace file → trace_sha drift caught."""
    trace_files = {"t1.champsimtrace.xz": b"trace data here"}
    challenge_dir, prov = _setup_challenge(tmp_path, trace_files=trace_files)
    baseline = _baseline(prov.to_dict(), per_trace=[{"trace": "t1"}])
    (challenge_dir / "baseline.json").write_text(json.dumps(baseline))
    (challenge_dir / "subtraces" / "t1.champsimtrace.xz").write_bytes(b"MUTATED")
    drifts = _check_baseline_provenance(_ch(challenge_dir), "DIGEST_IMAGE_LIVE")
    assert any("trace_sha256" in d for d in drifts)


def test_dict_per_trace_does_not_crash_guard(tmp_path):
    """Non-champsim sims (ramulator/mnsim/...) bake traces into the sim image
    (trace_sha256=0) and report per_trace as a DICT keyed by trace name. The
    guard must SKIP that, not iterate its str keys and raise on t["trace"].
    Regression: that TypeError crashed ramulator_rowhammer_tracker's session at
    start (the first paper-derived challenge); §27-class path-shape bug."""
    challenge_dir, prov = _setup_challenge(tmp_path)  # no trace files → trace_sha=0
    baseline = _baseline(prov.to_dict(),
                         per_trace={"470.lbm": {"overhead_pct": 28.5},
                                    "429.mcf": {"overhead_pct": 34.5}})
    (challenge_dir / "baseline.json").write_text(json.dumps(baseline))
    drifts = _check_baseline_provenance(_ch(challenge_dir), "DIGEST_IMAGE_LIVE")
    assert drifts == [], f"dict per_trace must not crash/drift the guard; got {drifts}"


def test_drift_unreadable_baseline(tmp_path):
    """Corrupted baseline.json → loud error, not silent pass."""
    challenge_dir, _ = _setup_challenge(tmp_path)
    (challenge_dir / "baseline.json").write_text("not json at all {")
    drifts = _check_baseline_provenance(_ch(challenge_dir), "ANY")
    assert any("unreadable" in d for d in drifts)


def test_no_baseline_file_silent_pass(tmp_path):
    """No baseline.json file at all → silent pass (caller handles)."""
    challenge_dir, _ = _setup_challenge(tmp_path)
    # No baseline.json written
    drifts = _check_baseline_provenance(_ch(challenge_dir), "ANY")
    assert drifts == []


# ---------------------------------------------------------------------------
# FIX 3: session.json rc reflects actual failure on uncaught exception
# ---------------------------------------------------------------------------


def test_run_session_rc_nonzero_on_uncaught_exception(tmp_path, monkeypatch):
    """Past bug: rc=0 was set at top of run_session, broad exception
    propagated past the finally block, session.json got written with
    rc=0 for a failed run. Fix: broad except sets rc=6 before re-raise."""
    import json as _json
    from archbench.core.challenge import Challenge, EvalConfig, RuntimeSpec
    from archbench.runtimes import session as session_mod

    # Stub out the heavy bits: image preflight, plugin lookup, baseline
    # check. We force sim.start() to raise so run_session enters the
    # broad except path. The assert is that session.json reflects the
    # failure (rc != 0), proving the rc-on-exception fix is in place.

    def fake_ensure_image(image, dirs, **kw):
        return "sha256:" + "0" * 64

    class _NullPlugin:
        docker_image = "img:fake"

    monkeypatch.setattr(session_mod, "ensure_image", fake_ensure_image)
    monkeypatch.setattr(session_mod, "get_plugin", lambda name: _NullPlugin())
    monkeypatch.setattr(
        session_mod, "_check_baseline_provenance", lambda *a, **kw: []
    )

    class _ExplodingManager:
        def __init__(self, cfg):
            self.config = cfg

        @property
        def name(self):
            return self.config.container_name

        def start(self):
            raise RuntimeError("simulated docker failure")

        def stop(self):
            pass

    monkeypatch.setattr(session_mod, "ContainerManager", _ExplodingManager)

    class _Runtime:
        name = "claude_code"
        docker_image = "rt:fake"
        dev_mode = False

    challenge = Challenge(
        id="t",
        name="t",
        simulator="champsim",
        prompt="",
        starter_files=[],
        output_files=[],
        eval=EvalConfig(baseline_file="baseline.json"),
        simulator_config={},
        runtimes={"claude_code": RuntimeSpec(
            name="claude_code",
            runtime_type="bundled",
            model="claude-opus-4-7",
            allowed_models=["claude-opus-4-7"],
        )},
        challenge_dir=tmp_path,
    )

    results_root = tmp_path / "results"
    with pytest.raises(RuntimeError, match="simulated docker failure"):
        session_mod.run_session(
            challenge=challenge,
            runtime=_Runtime(),
            anonymize=False,
            run_name="t_run",
            results_root=results_root,
        )

    session_json = results_root / "t" / "t_run" / "session.json"
    assert session_json.exists(), "session.json must still be written on failure"
    meta = _json.loads(session_json.read_text())
    assert meta["rc"] != 0, (
        f"session.json claims rc=0 for a failed run; the rc-on-exception "
        f"fix regressed. meta={meta}"
    )


def test_run_session_rejects_dev_on_bake_only_runtime(tmp_path, monkeypatch):
    """--dev (dev_mode=True) against a runtime whose info.yaml has
    mode=bake_only (or no mode field, defaulting to bake_only) must
    raise BEFORE any container work."""
    from archbench.core.challenge import Challenge, EvalConfig, RuntimeSpec
    from archbench.runtimes import session as session_mod

    # Make sure ensure_image is never called: if it is, the test fails
    # because the mode-compat check should run first.
    def _no_call(*a, **kw):
        raise AssertionError("ensure_image should not be called when "
                             "mode-compat check fails")
    monkeypatch.setattr(session_mod, "ensure_image", _no_call)

    class _Runtime:
        name = "claude_code"
        docker_image = "rt:fake"
        dev_mode = False

    challenge = Challenge(
        id="t",
        name="t",
        simulator="champsim",
        prompt="",
        starter_files=[],
        output_files=[],
        eval=EvalConfig(baseline_file="baseline.json"),
        simulator_config={},
        # No `mode` in data → defaults to bake_only
        runtimes={"claude_code": RuntimeSpec(
            name="claude_code", data={},
            runtime_type="bundled",
            model="claude-opus-4-7",
            allowed_models=["claude-opus-4-7"],
        )},
        challenge_dir=tmp_path,
    )

    with pytest.raises(RuntimeError, match="dev_capable"):
        session_mod.run_session(
            challenge=challenge,
            runtime=_Runtime(),
            anonymize=False,
            run_name="t_run",
            results_root=tmp_path / "results",
            dev_mode=True,
        )


def _stub_session_pipeline(monkeypatch, session_mod, runtime_flags=None,
                            agent_copy_out_calls=None):
    """Shared scaffolding for run_session rc tests: stubs out image
    preflight, baseline check, container start/stop, MCP server spawn,
    and the runtime so the test only exercises the rc-derivation path.

    `runtime_flags` is a dict applied to the runtime instance AFTER
    start_session returns (used to simulate timeout / non-zero exit).
    `agent_copy_out_calls` is a list to record copy_out invocations.
    """
    import json as _json

    runtime_flags = runtime_flags or {}
    agent_copy_out_calls = agent_copy_out_calls if agent_copy_out_calls is not None else []

    monkeypatch.setattr(
        session_mod, "ensure_image", lambda *a, **kw: "sha256:" + "0" * 64,
    )

    class _NullPlugin:
        docker_image = "img:fake"

        def verify_simulator(self, *a, **kw):
            return []

        def configure_simulator(self, *a, **kw):
            pass

        def export_workload_files(self, *a, **kw):
            pass

    monkeypatch.setattr(session_mod, "get_plugin", lambda name: _NullPlugin())
    monkeypatch.setattr(
        session_mod, "_check_baseline_provenance", lambda *a, **kw: []
    )

    class _Container:
        def __init__(self, cfg):
            self.config = cfg
            self._stopped = False

        @property
        def name(self):
            return self.config.container_name

        def start(self):
            pass

        def stop(self):
            self._stopped = True

        def copy_out(self, container_path, host_path):
            agent_copy_out_calls.append((container_path, str(host_path)))

        def exec(self, *a, **kw):
            return ("", 0)

    monkeypatch.setattr(session_mod, "ContainerManager", _Container)

    # Stub MCP server spawn so no real subprocess is launched.
    class _FakeProc:
        pid = 999
        returncode = 0

        def poll(self):
            return 0  # already exited — grace wait will short-circuit

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(session_mod, "_start_mcp_server",
                        lambda *a, **kw: _FakeProc())
    monkeypatch.setattr(session_mod, "_wait_for_port",
                        lambda *a, **kw: True)
    monkeypatch.setattr(session_mod, "_wait_for_in_flight_submits",
                        lambda *a, **kw: None)

    class _RT:
        name = "claude_code"
        docker_image = "rt:fake"
        dev_mode = False
        last_session_rc = runtime_flags.get("last_session_rc", 0)
        last_session_timed_out = runtime_flags.get("last_session_timed_out", False)

        def verify_in_container(self, agent):
            return []

        def stage_workspace(self, agent, challenge, **kwargs):
            pass

        def start_session(self, agent, mcp_url, prompt, round_timeout):
            # Write a dummy trajectory so session.py's read succeeds.
            traj = Path("/tmp/_test_traj.jsonl")
            traj.write_text("")
            return traj

    return _RT, agent_copy_out_calls


def test_run_session_rc_8_when_round_timeout_fires(tmp_path, monkeypatch):
    """Bug 5 fix: round_timeout firing must yield rc=8, not the legacy rc=0."""
    import json as _json
    from archbench.core.challenge import Challenge, EvalConfig, RuntimeSpec
    from archbench.runtimes import session as session_mod

    _RT, _ = _stub_session_pipeline(
        monkeypatch, session_mod,
        runtime_flags={"last_session_rc": -1, "last_session_timed_out": True},
    )

    challenge = Challenge(
        id="t", name="t", simulator="champsim", prompt="",
        starter_files=[], output_files=[],
        eval=EvalConfig(baseline_file="baseline.json"),
        simulator_config={},
        runtimes={"claude_code": RuntimeSpec(
            name="claude_code",
            data={"mode": "dev_capable"},
            runtime_type="bundled",
            model="claude-opus-4-7",
            allowed_models=["claude-opus-4-7"],
        )},
        challenge_dir=tmp_path,
    )
    results_root = tmp_path / "results"
    rc = session_mod.run_session(
        challenge=challenge, runtime=_RT(), anonymize=False,
        run_name="r", results_root=results_root,
    )
    assert rc == 8
    meta = _json.loads((results_root / "t" / "r" / "session.json").read_text())
    assert meta["rc"] == 8


def test_run_session_rc_7_on_nonzero_claude_exit(tmp_path, monkeypatch):
    """Bug 5 fix: claude subprocess returning non-zero (e.g. expired OAuth)
    must yield rc=7. The legacy code logged a warning and claimed rc=0."""
    import json as _json
    from archbench.core.challenge import Challenge, EvalConfig, RuntimeSpec
    from archbench.runtimes import session as session_mod

    _RT, _ = _stub_session_pipeline(
        monkeypatch, session_mod,
        runtime_flags={"last_session_rc": 1, "last_session_timed_out": False},
    )

    challenge = Challenge(
        id="t", name="t", simulator="champsim", prompt="",
        starter_files=[], output_files=[],
        eval=EvalConfig(baseline_file="baseline.json"),
        simulator_config={},
        runtimes={"claude_code": RuntimeSpec(
            name="claude_code",
            data={"mode": "dev_capable"},
            runtime_type="bundled",
            model="claude-opus-4-7",
            allowed_models=["claude-opus-4-7"],
        )},
        challenge_dir=tmp_path,
    )
    results_root = tmp_path / "results"
    rc = session_mod.run_session(
        challenge=challenge, runtime=_RT(), anonymize=False,
        run_name="r", results_root=results_root,
    )
    assert rc == 7
    meta = _json.loads((results_root / "t" / "r" / "session.json").read_text())
    assert meta["rc"] == 7


def test_run_session_rc_0_on_clean_exit(tmp_path, monkeypatch):
    """Successful runtime exit (rc=0, not timed out) keeps rc=0."""
    import json as _json
    from archbench.core.challenge import Challenge, EvalConfig, RuntimeSpec
    from archbench.runtimes import session as session_mod

    _RT, _ = _stub_session_pipeline(
        monkeypatch, session_mod,
        runtime_flags={"last_session_rc": 0, "last_session_timed_out": False},
    )

    challenge = Challenge(
        id="t", name="t", simulator="champsim", prompt="",
        starter_files=[], output_files=[],
        eval=EvalConfig(baseline_file="baseline.json"),
        simulator_config={},
        runtimes={"claude_code": RuntimeSpec(
            name="claude_code",
            data={"mode": "dev_capable"},
            runtime_type="bundled",
            model="claude-opus-4-7",
            allowed_models=["claude-opus-4-7"],
        )},
        challenge_dir=tmp_path,
    )
    results_root = tmp_path / "results"
    rc = session_mod.run_session(
        challenge=challenge, runtime=_RT(), anonymize=False,
        run_name="r", results_root=results_root,
    )
    assert rc == 0


def test_run_session_copies_out_workspace_before_stop(tmp_path, monkeypatch):
    """Bug 4 fix: /workspace/ is copied out BEFORE agent.stop() so the
    judge can read deliverables.md etc. We assert copy_out was called
    with a /workspace/ container path."""
    from archbench.core.challenge import Challenge, EvalConfig, RuntimeSpec
    from archbench.runtimes import session as session_mod

    copy_calls: list = []
    _RT, _ = _stub_session_pipeline(
        monkeypatch, session_mod,
        runtime_flags={"last_session_rc": 0, "last_session_timed_out": False},
        agent_copy_out_calls=copy_calls,
    )

    challenge = Challenge(
        id="t", name="t", simulator="champsim", prompt="",
        starter_files=[], output_files=[],
        eval=EvalConfig(baseline_file="baseline.json"),
        simulator_config={},
        runtimes={"claude_code": RuntimeSpec(
            name="claude_code",
            data={"mode": "dev_capable"},
            runtime_type="bundled",
            model="claude-opus-4-7",
            allowed_models=["claude-opus-4-7"],
        )},
        challenge_dir=tmp_path,
    )
    results_root = tmp_path / "results"
    session_mod.run_session(
        challenge=challenge, runtime=_RT(), anonymize=False,
        run_name="r", results_root=results_root,
    )
    # Both sim and agent containers were created, but only the agent's
    # /workspace/ should have been copy_out'd. The fake records EVERY
    # copy_out call regardless of which container — so we just check
    # at least one call targeted /workspace/.
    workspace_calls = [c for c in copy_calls if c[0].startswith("/workspace")]
    assert workspace_calls, f"no /workspace/ copy_out recorded; copy_calls={copy_calls}"


def test_run_session_accepts_dev_on_dev_capable_runtime(tmp_path, monkeypatch):
    """--dev against a runtime with info.yaml mode=dev_capable passes the
    mode-compat check (and proceeds to image work, which we stub)."""
    from archbench.core.challenge import Challenge, EvalConfig, RuntimeSpec
    from archbench.runtimes import session as session_mod

    # Stub: ensure_image returns a digest. We force a later failure
    # (sim.start) so we don't need to mock the entire pipeline — we
    # only care that dev_capable doesn't trip the mode-compat raise.
    monkeypatch.setattr(
        session_mod, "ensure_image", lambda *a, **kw: "sha256:" + "0" * 64,
    )

    class _NullPlugin:
        docker_image = "img:fake"

    monkeypatch.setattr(session_mod, "get_plugin", lambda name: _NullPlugin())
    monkeypatch.setattr(
        session_mod, "_check_baseline_provenance", lambda *a, **kw: []
    )

    class _ExplodingManager:
        def __init__(self, cfg):
            self.config = cfg
        @property
        def name(self):
            return self.config.container_name
        def start(self):
            raise RuntimeError("expected_failure_past_mode_check")
        def stop(self):
            pass

    monkeypatch.setattr(session_mod, "ContainerManager", _ExplodingManager)

    class _Runtime:
        name = "mini"
        docker_image = "rt:fake"
        dev_mode = False

    challenge = Challenge(
        id="t", name="t", simulator="champsim", prompt="",
        starter_files=[], output_files=[],
        eval=EvalConfig(baseline_file="baseline.json"),
        simulator_config={},
        runtimes={"mini": RuntimeSpec(
            name="mini",
            data={"mode": "dev_capable"},
            runtime_type="byo_model",
            model="gemma4",
        )},
        challenge_dir=tmp_path,
    )

    rt = _Runtime()
    # Mode-compat passes; we fail later (expected) — proves we got past it.
    with pytest.raises(RuntimeError, match="expected_failure_past_mode_check"):
        session_mod.run_session(
            challenge=challenge,
            runtime=rt,
            anonymize=False,
            run_name="t_run",
            results_root=tmp_path / "results",
            dev_mode=True,
        )
    # And dev_mode was propagated to the runtime instance.
    assert rt.dev_mode is True


# ---------------------------------------------------------------------------
# Phase B FIX: ANTHROPIC_API_KEY (and friends) propagation to subprocesses
# ---------------------------------------------------------------------------


def test_start_mcp_server_forwards_env(tmp_path, monkeypatch):
    """The MCP server subprocess must inherit the parent env so its
    in-process judge calls (and any other env-dependent code) see
    ANTHROPIC_API_KEY etc.

    Phase B fix: ``_start_mcp_server`` now passes ``env=os.environ.copy()``
    explicitly via ``_child_env``. This test pins that behavior so a
    future regression doesn't silently drop the env.
    """
    from archbench.runtimes import session as session_mod
    from simulators.champsim.connector.server import SubmitContext

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("ARCHBENCH_JUDGE_MODEL", "claude-opus-4-7-test")

    captured_kwargs = {}

    def fake_popen(cmd, **kwargs):
        captured_kwargs.update(kwargs)
        # Return a mock object that looks Popen-shaped enough for the caller.
        class _MockProc:
            pid = 999
            def poll(self):
                return 0
            def terminate(self):
                pass
            def wait(self, timeout=None):
                return 0
            def kill(self):
                pass
        return _MockProc()

    monkeypatch.setattr(session_mod.subprocess, "Popen", fake_popen)

    # Build the minimal ctx _start_mcp_server needs.
    class _NullChallenge:
        simulator = "champsim"
    class _NullPlugin:
        docker_image = "img:fake"
    class _NullManager:
        name = "fake"
    ctx = SubmitContext(
        challenge=_NullChallenge(),  # type: ignore[arg-type]
        challenge_dir=tmp_path,
        plugin=_NullPlugin(),  # type: ignore[arg-type]
        agent=_NullManager(),  # type: ignore[arg-type]
        sim=_NullManager(),    # type: ignore[arg-type]
        anonymizer=type("A", (), {"enabled": False})(),  # type: ignore[arg-type]
    )

    session_mod._start_mcp_server(
        ctx, port=12345, log_path=tmp_path / "mcp.log",
        results_dir=tmp_path,
    )
    env = captured_kwargs.get("env")
    assert env is not None, (
        "_start_mcp_server must pass env= explicitly so child process inherits "
        "ANTHROPIC_API_KEY and other secrets"
    )
    assert env.get("ANTHROPIC_API_KEY") == "test-anthropic-key"
    assert env.get("ARCHBENCH_JUDGE_MODEL") == "claude-opus-4-7-test"


def test_start_proxy_server_forwards_env(tmp_path, monkeypatch):
    """The proxy subprocess must inherit the parent env so backend
    handlers can read API keys for routes.yaml entries that use
    `api_key_env: OPENAI_API_KEY` / similar.

    Phase B fix: ``_start_proxy_server`` now passes
    ``env=os.environ.copy()`` via ``_child_env``.
    """
    from archbench.runtimes import session as session_mod

    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        class _MockProc:
            pid = 1001
            def poll(self):
                return 0
            def terminate(self):
                pass
            def wait(self, timeout=None):
                return 0
            def kill(self):
                pass
        return _MockProc()

    monkeypatch.setattr(session_mod.subprocess, "Popen", fake_popen)
    session_mod._start_proxy_server(port=12346, log_path=tmp_path / "proxy.log")
    env = captured.get("env")
    assert env is not None, (
        "_start_proxy_server must pass env= so backend dispatchers see "
        "OPENAI_API_KEY / ANTHROPIC_API_KEY / etc."
    )
    assert env.get("OPENAI_API_KEY") == "test-openai-key"


def test_child_env_returns_full_environment(monkeypatch):
    """The env-forwarding helper must NOT drop variables — that was
    the legacy bug (explicit-allowlist Popen stripped everything else)."""
    from archbench.runtimes.session import _child_env

    monkeypatch.setenv("ARCHBENCH_PHASE_B_SENTINEL", "yes")
    env = _child_env()
    assert env.get("ARCHBENCH_PHASE_B_SENTINEL") == "yes"
    # And it must be a copy, not a live os.environ reference (so the
    # caller can mutate without affecting the parent).
    env["ARCHBENCH_PHASE_B_SENTINEL"] = "changed"
    assert os.environ.get("ARCHBENCH_PHASE_B_SENTINEL") == "yes"


def test_grace_period_waits_for_inflight_marker(tmp_path):
    """Regression test: branch_haiku (Phase I) had no submit_outcomes.jsonl
    written before claude -p exited; the old "stable file size for 10 s"
    heuristic returned after 10 s and tore down a live sim. With the
    in-flight marker fix, the grace-period must wait while a marker
    exists and release once it is cleared.
    """
    import threading
    import time
    from archbench.runtimes.session import _wait_for_in_flight_submits

    class _FakeProc:
        def poll(self): return None  # MCP still alive

    inflight = tmp_path / ".in_flight"
    inflight.mkdir()
    (inflight / "sub_001").touch()

    # Run the waiter with a 5 s budget. Clear the marker after 1.5 s
    # in a background thread; expect the waiter to return promptly
    # after the marker disappears, not at the 5 s deadline.
    def _clear():
        time.sleep(1.5)
        (inflight / "sub_001").unlink()

    threading.Thread(target=_clear, daemon=True).start()
    t0 = time.time()
    _wait_for_in_flight_submits(_FakeProc(), tmp_path, grace_seconds=5)
    elapsed = time.time() - t0
    # Should be between 1.5 s and 4 s (poll interval is 2 s).
    assert 1.4 < elapsed < 4.5, (
        f"waiter elapsed {elapsed:.2f}s — expected ~1.5-4s "
        "(released after marker cleared, before deadline)"
    )


def test_grace_period_drains_without_marker(tmp_path):
    """If no submit was ever issued, .in_flight/ won't exist (or is
    empty); the waiter must return immediately, not block for grace_seconds.
    """
    import time
    from archbench.runtimes.session import _wait_for_in_flight_submits

    class _FakeProc:
        def poll(self): return None

    t0 = time.time()
    _wait_for_in_flight_submits(_FakeProc(), tmp_path, grace_seconds=30)
    elapsed = time.time() - t0
    # No markers means immediate release on first iteration.
    assert elapsed < 1.0, f"waiter elapsed {elapsed:.2f}s — expected <1s"
