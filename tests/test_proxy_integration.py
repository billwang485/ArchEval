"""Proxy + --model integration tests.

These verify the glue between schema (RuntimeSpec.runtime_type +
allowed_models) and proxy (archbench/serving/routes.yaml) — *without* touching
docker. Each test stubs out container start so the only thing exercised
is the validation + lifecycle plumbing in run_session.

Specifically:
  - bundled runtime + --model in allowed_models  → passes validation
  - bundled runtime + --model NOT in allowed     → RuntimeError early
  - byo_model runtime + --model in routes.yaml   → passes validation
  - byo_model runtime + --model NOT in routes    → RuntimeError early
  - byo_model spawns proxy subprocess; bundled does not
  - --thinking redirects to <model>-thinking if present, warns if not
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archbench.core.challenge import Challenge, EvalConfig, RuntimeSpec


def _make_challenge(tmp_path: Path, rt_name: str, spec: RuntimeSpec) -> Challenge:
    return Challenge(
        id="t",
        name="t",
        simulator="champsim",
        prompt="",
        starter_files=[],
        output_files=[],
        eval=EvalConfig(baseline_file="baseline.json"),
        simulator_config={},
        runtimes={rt_name: spec},
        challenge_dir=tmp_path,
    )


def _stub_pipeline(monkeypatch, session_mod, popen_calls):
    """Stub out heavy-IO pieces but record subprocess.Popen calls.

    `popen_calls` is a list — each (cmd, kwargs) Popen invocation is
    appended so the tests can assert proxy vs MCP startup.
    """
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

        @property
        def name(self):
            return self.config.container_name

        def start(self):
            pass

        def stop(self):
            pass

        def copy_out(self, *a, **kw):
            pass

        def exec(self, *a, **kw):
            return ("", 0)

    monkeypatch.setattr(session_mod, "ContainerManager", _Container)

    class _FakeProc:
        pid = 9999

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    # Stub the two subprocess-spawning helpers directly. We append a
    # signature tuple per call so tests can introspect proxy vs MCP.
    def fake_proxy(port, log_path):
        popen_calls.append((
            [__import__("sys").executable, "-m", "archbench.serving.proxy",
             "--port", str(port)],
            {"log_path": str(log_path)},
        ))
        return _FakeProc()

    def fake_mcp(ctx, port, log_path, results_dir=None, **kwargs):
        popen_calls.append((
            [__import__("sys").executable, "-m", "simulators.champsim.connector.server_subprocess",
             "--port", str(port)],
            {"log_path": str(log_path)},
        ))
        return _FakeProc()

    monkeypatch.setattr(session_mod, "_start_proxy_server", fake_proxy)
    monkeypatch.setattr(session_mod, "_start_mcp_server", fake_mcp)
    monkeypatch.setattr(session_mod, "_wait_for_port", lambda *a, **kw: True)
    monkeypatch.setattr(session_mod, "_wait_for_in_flight_submits",
                        lambda *a, **kw: None)


class _BundledRT:
    """Stand-in for a bundled runtime (no llm_base_url plumbing)."""
    name = "claude_code"
    docker_image = "rt:fake"
    dev_mode = False
    model = "claude-opus-4-7"
    last_session_rc = 0
    last_session_timed_out = False

    def verify_in_container(self, agent):
        return []

    def stage_workspace(self, agent, challenge, **kwargs):
        pass

    def start_session(self, agent, mcp_url, prompt, round_timeout):
        traj = Path("/tmp/_test_traj.jsonl")
        traj.write_text("")
        return traj


class _ByoRT:
    """Stand-in for a byo_model runtime (mini-shape: has llm_base_url)."""
    name = "mini"
    docker_image = "rt:fake"
    dev_mode = False
    model = "gemma4"
    llm_base_url: str | None = None
    last_session_rc = 0
    last_session_timed_out = False

    def verify_in_container(self, agent):
        return []

    def stage_workspace(self, agent, challenge, **kwargs):
        pass

    def start_session(self, agent, mcp_url, prompt, round_timeout):
        traj = Path("/tmp/_test_traj.jsonl")
        traj.write_text("")
        return traj


# ---------------------------------------------------------------------------
# Validation: bundled runtime model checks
# ---------------------------------------------------------------------------


def test_bundled_runtime_accepts_model_in_allowed_list(tmp_path, monkeypatch):
    """Happy path: --model is in allowed_models → run_session proceeds
    past validation. The pipeline is fully stubbed so rc == 0."""
    from archbench.runtimes import session as session_mod
    popen_calls: list = []
    _stub_pipeline(monkeypatch, session_mod, popen_calls)

    spec = RuntimeSpec(
        name="claude_code",
        runtime_type="bundled",
        model="claude-opus-4-7",
        allowed_models=["claude-opus-4-7", "claude-sonnet-4-6"],
        round_timeout=60,
        data={"mode": "bake_only"},
    )
    challenge = _make_challenge(tmp_path, "claude_code", spec)
    rc = session_mod.run_session(
        challenge=challenge,
        runtime=_BundledRT(),
        anonymize=False,
        run_name="r",
        results_root=tmp_path / "results",
        model="claude-sonnet-4-6",
    )
    assert rc == 0


def test_bundled_runtime_rejects_model_not_in_allowed_list(tmp_path, monkeypatch):
    """--model claude-bogus must raise BEFORE any container starts."""
    from archbench.runtimes import session as session_mod

    # Sentinel: if ensure_image runs, the validation didn't fire early.
    def _no_call(*a, **kw):
        raise AssertionError("ensure_image must not be called when model "
                             "validation should fail")
    monkeypatch.setattr(session_mod, "ensure_image", _no_call)

    spec = RuntimeSpec(
        name="claude_code",
        runtime_type="bundled",
        model="claude-opus-4-7",
        allowed_models=["claude-opus-4-7"],
        data={"mode": "bake_only"},
    )
    challenge = _make_challenge(tmp_path, "claude_code", spec)
    with pytest.raises(RuntimeError, match="doesn't allow model"):
        session_mod.run_session(
            challenge=challenge,
            runtime=_BundledRT(),
            anonymize=False,
            run_name="r",
            results_root=tmp_path / "results",
            model="claude-bogus",
        )


# ---------------------------------------------------------------------------
# Validation: byo_model runtime checks against routes.yaml
# ---------------------------------------------------------------------------


def test_byo_runtime_accepts_model_in_routes_yaml(tmp_path, monkeypatch):
    """byo_model + --model gemma4 must pass (gemma4 is in routes.yaml)."""
    from archbench.runtimes import session as session_mod
    popen_calls: list = []
    _stub_pipeline(monkeypatch, session_mod, popen_calls)

    spec = RuntimeSpec(
        name="mini",
        runtime_type="byo_model",
        model="gemma4",
        round_timeout=60,
        data={"mode": "dev_capable"},
    )
    challenge = _make_challenge(tmp_path, "mini", spec)
    rc = session_mod.run_session(
        challenge=challenge,
        runtime=_ByoRT(),
        anonymize=False,
        run_name="r",
        results_root=tmp_path / "results",
        model="gemma4",
    )
    assert rc == 0


def test_byo_runtime_rejects_model_not_in_routes_yaml(tmp_path, monkeypatch):
    """byo_model + a model NOT in routes.yaml must raise early."""
    from archbench.runtimes import session as session_mod

    def _no_call(*a, **kw):
        raise AssertionError("ensure_image must not be called when model "
                             "validation should fail")
    monkeypatch.setattr(session_mod, "ensure_image", _no_call)

    spec = RuntimeSpec(
        name="mini",
        runtime_type="byo_model",
        model="gemma4",
        data={"mode": "dev_capable"},
    )
    challenge = _make_challenge(tmp_path, "mini", spec)
    with pytest.raises(RuntimeError, match="not in archbench/serving/routes.yaml"):
        session_mod.run_session(
            challenge=challenge,
            runtime=_ByoRT(),
            anonymize=False,
            run_name="r",
            results_root=tmp_path / "results",
            model="nonexistent-model",
        )


# ---------------------------------------------------------------------------
# Proxy lifecycle: byo_model spawns proxy; bundled does NOT
# ---------------------------------------------------------------------------


def test_byo_runtime_spawns_proxy_subprocess(tmp_path, monkeypatch):
    """For a byo_model runtime, _start_proxy_server is invoked AND its
    Popen carries `-m archbench.serving.proxy`."""
    from archbench.runtimes import session as session_mod
    popen_calls: list = []
    _stub_pipeline(monkeypatch, session_mod, popen_calls)

    spec = RuntimeSpec(
        name="mini",
        runtime_type="byo_model",
        model="gemma4",
        round_timeout=60,
        data={"mode": "dev_capable"},
    )
    challenge = _make_challenge(tmp_path, "mini", spec)
    session_mod.run_session(
        challenge=challenge,
        runtime=_ByoRT(),
        anonymize=False,
        run_name="r",
        results_root=tmp_path / "results",
    )

    proxy_calls = [c for c, _ in popen_calls if "archbench.serving.proxy" in c]
    assert proxy_calls, (
        f"byo_model run did NOT spawn the proxy subprocess. "
        f"popen calls: {[c for c, _ in popen_calls]}"
    )


def test_bundled_runtime_does_not_spawn_proxy(tmp_path, monkeypatch):
    """A bundled runtime hits the vendor API directly; the proxy must
    not be spawned for it."""
    from archbench.runtimes import session as session_mod
    popen_calls: list = []
    _stub_pipeline(monkeypatch, session_mod, popen_calls)

    spec = RuntimeSpec(
        name="claude_code",
        runtime_type="bundled",
        model="claude-opus-4-7",
        allowed_models=["claude-opus-4-7"],
        round_timeout=60,
        data={"mode": "bake_only"},
    )
    challenge = _make_challenge(tmp_path, "claude_code", spec)
    session_mod.run_session(
        challenge=challenge,
        runtime=_BundledRT(),
        anonymize=False,
        run_name="r",
        results_root=tmp_path / "results",
    )

    proxy_calls = [c for c, _ in popen_calls if "archbench.serving.proxy" in c]
    assert not proxy_calls, (
        f"bundled run spawned the proxy unnecessarily. proxy calls: {proxy_calls}"
    )


# ---------------------------------------------------------------------------
# --thinking: redirect to <model>-thinking variant if present, else warn
# ---------------------------------------------------------------------------


def test_thinking_flag_redirects_when_variant_present(tmp_path, monkeypatch):
    """If routes.yaml has both `gemma4` and `gemma4-thinking`, then
    --thinking should auto-redirect to the thinking variant. We assert
    on the runtime's `.model` attribute (which our code mutates after
    validation)."""
    from archbench.runtimes import session as session_mod
    from archbench.serving import routes as routes_mod

    # Inject a thinking variant into the loaded routes registry.
    real_load = routes_mod.load_routes
    def _patched_load(path):
        r = real_load(path)
        # Insert a synthetic gemma4-thinking entry alongside gemma4.
        from archbench.serving.routes import Routes, RouteEntry
        entries = dict(r._entries)
        entries["gemma4-thinking"] = RouteEntry(
            name="gemma4-thinking",
            backend="managed_vllm",
            model_id="google/gemma-4-31B-it",
            endpoint_json=Path("/dev/null"),
            supports_thinking=True,
        )
        return Routes(entries)
    monkeypatch.setattr(routes_mod, "load_routes", _patched_load)

    popen_calls: list = []
    _stub_pipeline(monkeypatch, session_mod, popen_calls)

    rt = _ByoRT()
    spec = RuntimeSpec(
        name="mini",
        runtime_type="byo_model",
        model="gemma4",
        round_timeout=60,
        data={"mode": "dev_capable"},
    )
    challenge = _make_challenge(tmp_path, "mini", spec)
    session_mod.run_session(
        challenge=challenge,
        runtime=rt,
        anonymize=False,
        run_name="r",
        results_root=tmp_path / "results",
        thinking=True,
    )
    assert rt.model == "gemma4-thinking", (
        f"--thinking should have redirected to gemma4-thinking; got {rt.model!r}"
    )


def test_thinking_without_variant_keeps_model(tmp_path, monkeypatch):
    """If `<model>-thinking` route is absent, the model is NOT mutated.
    A warning is logged but the call proceeds."""
    from archbench.runtimes import session as session_mod
    popen_calls: list = []
    _stub_pipeline(monkeypatch, session_mod, popen_calls)

    rt = _ByoRT()
    spec = RuntimeSpec(
        name="mini",
        runtime_type="byo_model",
        model="gemma4",
        round_timeout=60,
        data={"mode": "dev_capable"},
    )
    challenge = _make_challenge(tmp_path, "mini", spec)
    session_mod.run_session(
        challenge=challenge,
        runtime=rt,
        anonymize=False,
        run_name="r",
        results_root=tmp_path / "results",
        thinking=True,
    )
    # No variant in real routes.yaml → model stays as-is.
    assert rt.model == "gemma4"
