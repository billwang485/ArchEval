"""Unit tests for the unified eval + baseline refactor.

Covers (per the code-phase test spec):
  (a) cmd_baseline errors LOUDLY (non-zero) when evaluate.sh is missing and
      when plugin.parse_output returns None (never fabricate a baseline,
      CLAUDE.md §1.9).
  (b) stamp_baseline produces a 4-tuple whose starter_sha256 (and trace_sha256)
      byte-match _check_baseline_provenance's derivation — the riskiest
      correctness point (CLAUDE.md §1.7). If this drifts, every freshly
      stamped baseline reads RED at session start.
  (c) dramsys + ramulator run_submit now DISPATCH to evaluation/evaluate.sh
      host-side (the design §3.2 conformance fix), with the in-container
      build_and_run.sh path kept only as a no-evaluate.sh fallback.

None of these need docker/podman: image-digest + subprocess are mocked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import archbench.cli as cli
from archbench.core.provenance import (
    Provenance,
    sha256_of_bytes,
    sha256_of_file,
    stamp_baseline,
    starter_dir_sha256,
    trace_files_sha256,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drift_guard_starter_sha(starter_dir: Path) -> str:
    """Inline copy of session.py::_check_baseline_provenance L989-992.

    This is the spec the stamper MUST match. We replicate it literally here
    so the test fails if EITHER side changes the formula without the other.
    """
    return sha256_of_bytes(
        b"".join(
            f.name.encode() + b":" + sha256_of_file(f).encode() + b"\n"
            for f in sorted(starter_dir.iterdir())
            if f.is_file()
        )
    )


def _drift_guard_trace_sha(trace_files: list[Path]) -> str:
    """Inline copy of session.py::_check_baseline_provenance L1035-1038."""
    return sha256_of_bytes(
        b"".join(
            tn.name.encode() + b":" + sha256_of_file(tn).encode() + b"\n"
            for tn in trace_files
        )
    )


def _make_challenge_dir(tmp_path: Path, simulator: str = "dramsys") -> Path:
    """Build a minimal valid challenge dir with starter + challenge.yaml."""
    cdir = tmp_path / "challenges" / "fake_challenge"
    (cdir / "challenge" / "starter").mkdir(parents=True)
    (cdir / "challenge" / "starter" / "config.json").write_text('{"a": 1}\n')
    (cdir / "challenge" / "starter" / "mc_config.json").write_text('{"b": 2}\n')
    (cdir / "evaluation").mkdir(parents=True)
    (cdir / "simulator").mkdir(parents=True)
    (cdir / "simulator" / "config.json").write_text('{"sim": "cfg"}\n')
    (cdir / "challenge.yaml").write_text(
        "id: fake_challenge\n"
        "name: fake\n"
        f"simulator: {simulator}\n"
        "difficulty: easy\n"
        "prompt: |\n"
        "  do a thing\n"
        "input:\n"
        "  starter_files:\n"
        "    - config.json\n"
        "    - mc_config.json\n"
        "output:\n"
        "  files:\n"
        "    - config.json\n"
        "eval:\n"
        "  metric: bandwidth_gbps\n"
        "  direction: higher_is_better\n"
        "  baseline: evaluation/baseline.json\n"
        "simulator_config:\n"
        "  trace: example.stl\n"
    )
    return cdir


class _FakePlugin:
    """Minimal SimulatorPlugin stand-in for cmd_baseline tests."""

    docker_image = "localhost/archbench-fake:v6"

    def __init__(self, metric):
        self._metric = metric

    def parse_output(self, raw_output: str):
        return self._metric


class _Args:
    def __init__(self, challenge_dir: str):
        self.challenge_dir = challenge_dir


# ---------------------------------------------------------------------------
# (b) stamp_baseline byte-match — the §1.7 invariant
# ---------------------------------------------------------------------------


def test_stamp_baseline_starter_matches_drift_guard(tmp_path, monkeypatch):
    """starter_sha256 from stamp_baseline == drift-guard derivation."""
    cdir = _make_challenge_dir(tmp_path)
    starter = cdir / "challenge" / "starter"
    baseline_path = cdir / "evaluation" / "baseline.json"
    baseline_path.write_text(json.dumps({"metric": 1.0, "per_trace": []}) + "\n")

    monkeypatch.setattr(
        "archbench.core.provenance.docker_image_digest", lambda tag: "ab" * 32,
    )
    prov = stamp_baseline(
        baseline_path,
        image_tag="localhost/archbench-fake:v6",
        config_path=cdir / "simulator" / "config.json",
        starter_dir=starter,
        trace_files=None,
        repo_root=tmp_path,
    )
    assert prov.starter_sha256 == _drift_guard_starter_sha(starter)
    # And it's persisted into baseline.json as a complete 4-tuple.
    written = json.loads(baseline_path.read_text())
    Provenance.from_dict(written["provenance"])  # raises if a field missing
    assert written["provenance"]["starter_sha256"] == prov.starter_sha256
    # config present → real sha (not the zero sentinel).
    assert prov.config_sha256 == sha256_of_file(cdir / "simulator" / "config.json")
    # No trace list → zeroed with a recorded reason.
    assert prov.trace_sha256 == "0" * 64
    assert "trace_sha256_reason" in written


def test_stamp_baseline_trace_matches_drift_guard(tmp_path, monkeypatch):
    """trace_sha256 over a file list == drift-guard derivation (basename key)."""
    cdir = _make_challenge_dir(tmp_path, simulator="champsim")
    starter = cdir / "challenge" / "starter"
    subtraces = cdir / "simulator" / "subtraces"
    subtraces.mkdir(parents=True)
    t0 = subtraces / "600.perlbench_s-1273B.champsimtrace.xz"
    t1 = subtraces / "602.gcc_s-1850B.champsimtrace.xz"
    t0.write_bytes(b"trace-bytes-0")
    t1.write_bytes(b"trace-bytes-1")

    baseline_path = cdir / "evaluation" / "baseline.json"
    baseline_path.write_text(json.dumps({
        "metric": 1.0,
        "per_trace": [
            {"trace": "600.perlbench_s-1273B"},
            {"trace": "602.gcc_s-1850B"},
        ],
    }) + "\n")

    monkeypatch.setattr(
        "archbench.core.provenance.docker_image_digest", lambda tag: "cd" * 32,
    )
    trace_files = [t0, t1]
    prov = stamp_baseline(
        baseline_path,
        image_tag="localhost/archbench-fake:v6",
        config_path=None,
        starter_dir=starter,
        trace_files=trace_files,
        repo_root=tmp_path,
    )
    assert prov.trace_sha256 == _drift_guard_trace_sha(trace_files)
    assert prov.trace_sha256 != "0" * 64
    # trace_files_sha256 helper agrees with the drift-guard form too.
    assert trace_files_sha256(trace_files) == _drift_guard_trace_sha(trace_files)


def test_stamp_baseline_refuses_when_image_absent(tmp_path, monkeypatch):
    """No live image → no stamp (§1.7: never stamp against an absent image)."""
    cdir = _make_challenge_dir(tmp_path)
    baseline_path = cdir / "evaluation" / "baseline.json"
    baseline_path.write_text(json.dumps({"metric": 1.0}) + "\n")
    monkeypatch.setattr(
        "archbench.core.provenance.docker_image_digest", lambda tag: None,
    )
    with pytest.raises(RuntimeError, match="not loaded locally"):
        stamp_baseline(
            baseline_path,
            image_tag="localhost/archbench-fake:v6",
            config_path=None,
            starter_dir=cdir / "challenge" / "starter",
            trace_files=None,
            repo_root=tmp_path,
        )


def test_trace_files_sha256_empty_is_zero_sentinel():
    assert trace_files_sha256([]) == "0" * 64


def test_starter_dir_sha256_is_order_independent_of_iterdir(tmp_path):
    """iterdir() order is arbitrary; the sorted() inside must normalize it."""
    d = tmp_path / "s"
    d.mkdir()
    (d / "z.txt").write_text("z")
    (d / "a.txt").write_text("a")
    (d / "m.txt").write_text("m")
    assert starter_dir_sha256(d) == _drift_guard_starter_sha(d)


# ---------------------------------------------------------------------------
# (a) cmd_baseline errors LOUDLY
# ---------------------------------------------------------------------------


def _patch_cmd_baseline_deps(monkeypatch, plugin, run_result=None):
    """Patch the source modules cmd_baseline imports function-locally.

    cmd_baseline does `from archbench.simulators import get_plugin`,
    `from archbench.core.container import ensure_image`, and `import subprocess`
    INSIDE the function, so we patch the canonical source modules (not
    attributes on archbench.cli).
    """
    import subprocess as _subprocess

    monkeypatch.setattr("archbench.simulators.get_plugin", lambda name: plugin)
    monkeypatch.setattr("archbench.core.container.ensure_image", lambda *a, **k: "ab" * 32)
    monkeypatch.setattr(
        "archbench.core.provenance.docker_image_digest", lambda tag: "ab" * 32,
    )
    # stamp_baseline -> git_head_commit uses subprocess.check_output, which
    # CPython implements via subprocess.run. Patching run globally below
    # would break it; stub the commit lookup so stamping stays self-contained.
    monkeypatch.setattr(
        "archbench.core.provenance.git_head_commit", lambda repo: "deadbeef" * 5,
    )
    if run_result is not None:
        monkeypatch.setattr(_subprocess, "run", lambda *a, **k: run_result)


def test_cmd_baseline_missing_evaluate_sh_errors(tmp_path, monkeypatch):
    cdir = _make_challenge_dir(tmp_path)
    # No evaluation/evaluate.sh and no root evaluate.sh.
    _patch_cmd_baseline_deps(monkeypatch, _FakePlugin({"metric": 1.0}))
    rc = cli.cmd_baseline(_Args(str(cdir)))
    assert rc != 0


def test_cmd_baseline_missing_challenge_yaml_errors(tmp_path):
    empty = tmp_path / "nope"
    empty.mkdir()
    rc = cli.cmd_baseline(_Args(str(empty)))
    assert rc != 0


def test_cmd_baseline_parse_none_does_not_fabricate(tmp_path, monkeypatch):
    """parse_output returns None → non-zero, no baseline.json written (§1.9)."""
    cdir = _make_challenge_dir(tmp_path)
    ev = cdir / "evaluation" / "evaluate.sh"
    ev.write_text("#!/bin/bash\necho SIMULATION_OK\n")
    ev.chmod(0o755)

    class _R:
        returncode = 0
        stdout = "SIMULATION_OK\n"
        stderr = ""

    _patch_cmd_baseline_deps(monkeypatch, _FakePlugin(None), run_result=_R())
    rc = cli.cmd_baseline(_Args(str(cdir)))
    assert rc != 0
    assert not (cdir / "evaluation" / "baseline.json").exists()


def test_cmd_baseline_infra_rc_nonzero_errors(tmp_path, monkeypatch):
    """evaluate.sh non-zero rc (infra failure) → cmd_baseline non-zero."""
    cdir = _make_challenge_dir(tmp_path)
    ev = cdir / "evaluation" / "evaluate.sh"
    ev.write_text("#!/bin/bash\nexit 1\n")
    ev.chmod(0o755)

    class _R:
        returncode = 1
        stdout = ""
        stderr = "boom"

    _patch_cmd_baseline_deps(monkeypatch, _FakePlugin({"metric": 1.0}), run_result=_R())
    rc = cli.cmd_baseline(_Args(str(cdir)))
    assert rc != 0


def test_cmd_baseline_happy_path_writes_and_stamps(tmp_path, monkeypatch):
    """End-to-end (mocked sim): metric parsed → baseline.json written + stamped,
    and the stamped starter_sha256 byte-matches the drift guard."""
    cdir = _make_challenge_dir(tmp_path)
    ev = cdir / "evaluation" / "evaluate.sh"
    ev.write_text("#!/bin/bash\necho SIMULATION_OK\n")
    ev.chmod(0o755)

    metric = {"metric": 9.06, "bandwidth_gbps": 9.06, "total_time_ns": 13249.92}

    class _R:
        returncode = 0
        stdout = "SIMULATION_OK\nARCHBENCH_JSON_START\n{}\nARCHBENCH_JSON_END\n"
        stderr = ""

    _patch_cmd_baseline_deps(monkeypatch, _FakePlugin(metric), run_result=_R())
    rc = cli.cmd_baseline(_Args(str(cdir)))
    assert rc == 0
    bl = json.loads((cdir / "evaluation" / "baseline.json").read_text())
    assert bl["bandwidth_gbps"] == 9.06
    assert bl["metric"] == 9.06
    assert bl["per_trace"] == []
    # 4-tuple present + starter byte-matches the drift guard.
    Provenance.from_dict(bl["provenance"])
    assert bl["provenance"]["starter_sha256"] == _drift_guard_starter_sha(
        cdir / "challenge" / "starter"
    )
    # config under simulator/ → real config sha (not zeroed).
    assert bl["provenance"]["config_sha256"] == sha256_of_file(
        cdir / "simulator" / "config.json"
    )


# ---------------------------------------------------------------------------
# (c) dramsys + ramulator run_submit dispatch to evaluate.sh
# ---------------------------------------------------------------------------


def _challenge_with_evaluate(tmp_path: Path, simulator: str):
    from archbench.core.challenge import load_challenge

    cdir = _make_challenge_dir(tmp_path, simulator=simulator)
    ev = cdir / "evaluation" / "evaluate.sh"
    ev.write_text("#!/bin/bash\necho SIMULATION_OK\n")
    ev.chmod(0o755)
    return load_challenge(cdir), cdir


class _ExplodingSim:
    """A sim container that fails the test if the in-container path is used."""

    name = "should_not_be_used"

    def exec(self, *a, **k):  # pragma: no cover - asserts misuse
        raise AssertionError(
            "run_submit used the in-container sim.exec path despite an "
            "evaluation/evaluate.sh being present — host-side dispatch expected."
        )

    def copy_in(self, *a, **k):  # pragma: no cover
        raise AssertionError("sim.copy_in should not be called in evaluate.sh mode")


@pytest.mark.parametrize("simulator", ["dramsys", "ramulator"])
def test_run_submit_dispatches_to_evaluate_sh(tmp_path, monkeypatch, simulator):
    """dramsys/ramulator run_submit dispatch host-side to evaluation/evaluate.sh."""
    from archbench.simulators import get_plugin

    challenge, cdir = _challenge_with_evaluate(tmp_path, simulator)
    plugin = get_plugin(simulator)

    captured = {}

    class _R:
        returncode = 0
        stdout = "SIMULATION_OK\nARCHBENCH_JSON_START\n{\"bandwidth_gbps\": 9.06}\nARCHBENCH_JSON_END\n"
        stderr = ""

    plugin_mod = type(plugin).__module__

    def _fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        return _R()

    # Patch subprocess.run as seen by the plugin's module.
    import importlib
    mod = importlib.import_module(plugin_mod)
    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    raw = plugin.run_submit(_ExplodingSim(), challenge, {"config.json": "{}"})

    # Dispatched host-side: bash <evaluation/evaluate.sh> <tmpdir>
    assert captured["cmd"][0] == "bash"
    assert captured["cmd"][1] == str(cdir / "evaluation" / "evaluate.sh")
    assert Path(captured["cmd"][2]).is_absolute()
    # stdout forwarded so parse_output sees the markers.
    assert "SIMULATION_OK" in raw
    assert plugin.parse_output(raw) == {"bandwidth_gbps": 9.06}


@pytest.mark.parametrize("simulator", ["dramsys", "ramulator"])
def test_run_submit_falls_back_in_container_without_evaluate_sh(
    tmp_path, monkeypatch, simulator,
):
    """No evaluate.sh → fall back to the in-container build_and_run.sh path.

    Guards the scaffold (the only ramulator challenge today ships no
    sim-running evaluate.sh), so the fallback must still drive sim.exec.
    """
    from archbench.core.challenge import load_challenge
    from archbench.simulators import get_plugin

    cdir = _make_challenge_dir(tmp_path, simulator=simulator)
    # Intentionally NO evaluation/evaluate.sh.
    challenge = load_challenge(cdir)
    plugin = get_plugin(simulator)

    calls = {"exec": [], "copy_in": 0}

    class _Sim:
        name = "sim_fallback"

        def exec(self, cmd, *a, **k):
            calls["exec"].append(cmd)
            return ("SIMULATION_OK\nARCHBENCH_JSON_START\n{\"bandwidth_gbps\": 1.0}\nARCHBENCH_JSON_END\n", 0)

        def copy_in(self, *a, **k):
            calls["copy_in"] += 1

    raw = plugin.run_submit(_Sim(), challenge, {"config.json": "{}"})
    # The fallback invokes build_and_run.sh in-container.
    assert any("build_and_run.sh" in c for c in calls["exec"])
    assert calls["copy_in"] >= 1
    assert "SIMULATION_OK" in raw
