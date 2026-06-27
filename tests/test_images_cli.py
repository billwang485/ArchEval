"""test_images_cli.py — PHASE K5: the `archbench images` lifecycle verbs + the gated
autobuild fallback in ensure_image (docs/docker_management.md §7-K5, §8).

NOTHING here calls a real container engine or builds a 13 GB image. Every
engine call is mocked: `container_engine()` is forced to a known string and
`subprocess.run` is intercepted so the test asserts the *argv shape* the verb
constructs, never executing it. The autobuild tests stub the build helper +
digest probe so the only thing under test is the SLURM/ARCHBENCH_NO_AUTOBUILD gate.

Covers (per the K5 spec):
  - build constructs `<engine> build -t <fq> -f <dockerfile> <context>` and
    stages workloads for a SIMULATOR (delegates sim_agents to the recipe).
  - save / load / rm / pull build the right argv.
  - gc targets ONLY archbench_* containers + dangling layers.
  - category | all expansion via _resolve_image_targets.
  - autobuild is SKIPPED when SLURM_JOB_ID / ARCHBENCH_NO_AUTOBUILD is set, and
    ATTEMPTED otherwise (build mocked).
  - `--dry-run build all` prints a plan and builds nothing.
  - sim_agents fully_qualified == _l2agent_image(sim) (default-identity proof).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from archbench import cli
from archbench.core import container as container_mod
from archbench.image_management import manifest as images_mod
from archbench.image_management.plan import _l2agent_image
from archbench.simulators import get_plugin


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


class _FakeRun:
    """A captured-argv recorder standing in for subprocess.run.

    Records every argv it sees and returns a configurable CompletedProcess-ish
    object so nothing actually shells out. `rc`/`stdout`/`stderr` are global
    defaults; per-prefix overrides let `ps -aq` return container ids while a
    later `rm -f` returns success.
    """

    def __init__(self, rc=0, stdout="", stderr=""):
        self.calls: list[list[str]] = []
        self._rc = rc
        self._stdout = stdout
        self._stderr = stderr
        self._overrides: list[tuple[list[str], object]] = []

    def when(self, prefix: list[str], stdout="", rc=0, stderr=""):
        self._overrides.append((prefix, SimpleNamespace(
            returncode=rc, stdout=stdout, stderr=stderr)))
        return self

    def __call__(self, cmd, *args, **kwargs):
        argv = list(cmd) if isinstance(cmd, (list, tuple)) else [cmd]
        self.calls.append(argv)
        for prefix, result in self._overrides:
            if argv[:len(prefix)] == prefix:
                return result
        return SimpleNamespace(
            returncode=self._rc, stdout=self._stdout, stderr=self._stderr)

    def argvs_with(self, *needles: str) -> list[list[str]]:
        """All recorded argvs where every needle appears as (a substring of)
        some element — so an absolute path element matches its basename
        needle."""
        def _has(argv, needle):
            return any(needle in str(part) for part in argv)
        return [c for c in self.calls if all(_has(c, n) for n in needles)]


@pytest.fixture
def fake_engine(monkeypatch):
    """Force container_engine() -> 'docker' everywhere it's consulted, so argv
    assertions are deterministic regardless of the host's engine.

    cli verbs do `from archbench.image_management.engine import container_engine` lazily inside
    the function body, so patching the source module covers them; container.py
    binds the name at import, so patch that already-bound name too."""
    monkeypatch.setattr("archbench.image_management.engine.container_engine", lambda: "docker")
    monkeypatch.setattr(container_mod, "container_engine", lambda: "docker")
    return "docker"


@pytest.fixture
def manifest():
    return images_mod.load_manifest()


def _args(**kw):
    return SimpleNamespace(**kw)


# ---------------------------------------------------------------------------
# (3) sim_agents default-identity proof vs _l2agent_image
# ---------------------------------------------------------------------------


def test_sim_agents_fully_qualified_matches_l2agent_image(manifest):
    """The K4 invariant: every sim_agents entry's fully_qualified() byte-equals
    _l2agent_image(get_plugin(<sim>).docker_image). Nothing K4 relies on may
    break (the simulator_centric L2 overlays resolve via _l2agent_image)."""
    keys = images_mod.keys("sim_agents", manifest)
    assert keys, "expected at least champsim under sim_agents"
    for key in keys:
        fq = images_mod.fully_qualified("sim_agents", key, manifest)
        expected = _l2agent_image(get_plugin(key).docker_image)
        assert fq == expected, (
            f"sim_agents/{key} -> {fq} != _l2agent_image -> {expected}")


def test_sim_agents_includes_champsim(manifest):
    assert "champsim" in images_mod.keys("sim_agents", manifest)
    assert (images_mod.fully_qualified("sim_agents", "champsim", manifest)
            == "localhost/archbench-champsim-l2agent:v6")


# ---------------------------------------------------------------------------
# Target expansion (category | all | name | <cat>/<key>)
# ---------------------------------------------------------------------------


def test_target_all_expands_to_every_image(manifest):
    got = cli._resolve_image_targets("all", manifest)
    assert set(got) == set(images_mod.iter_images(manifest))


def test_target_category_simulators(manifest):
    got = cli._resolve_image_targets("simulators", manifest)
    cats = {c for c, _, _ in got}
    assert cats == {"simulators"}
    assert {k for _, k, _ in got} == set(images_mod.keys("simulators", manifest))


def test_target_hyphenated_sim_agents_alias(manifest):
    """`sim-agents` (CLI spelling) maps to the `sim_agents` manifest key."""
    got = cli._resolve_image_targets("sim-agents", manifest)
    assert {c for c, _, _ in got} == {"sim_agents"}


def test_target_bare_name_prefers_simulators(manifest):
    """champsim exists under BOTH simulators and sim_agents; a bare name
    resolves to simulators first."""
    got = cli._resolve_image_targets("champsim", manifest)
    assert got == [("simulators", "champsim",
                    "localhost/archbench-champsim:v6")]


def test_target_cat_slash_key_disambiguates(manifest):
    got = cli._resolve_image_targets("sim_agents/champsim", manifest)
    assert got == [("sim_agents", "champsim",
                    "localhost/archbench-champsim-l2agent:v6")]
    got2 = cli._resolve_image_targets("sim/champsim", manifest)
    assert got2 == [("simulators", "champsim", "localhost/archbench-champsim:v6")]


def test_target_unknown_raises(manifest):
    with pytest.raises(KeyError):
        cli._resolve_image_targets("not_a_real_image", manifest)


# ---------------------------------------------------------------------------
# (1) build argv: <engine> build -t <fq> -f <dockerfile> <context>
# ---------------------------------------------------------------------------


def test_build_image_from_manifest_argv_shape(fake_engine):
    """build_image_from_manifest(dry_run=True) returns the exact build argv,
    engine-shimmed, no staging, no engine call."""
    argv = container_mod.build_image_from_manifest(
        "localhost/archbench-champsim:v6", repo_root=images_mod.REPO_ROOT,
        dry_run=True)
    assert argv[0] == "docker"
    assert argv[1] == "build"
    assert argv[2:4] == ["-t", "localhost/archbench-champsim:v6"]
    assert argv[4] == "-f"
    assert argv[5].endswith("simulators/champsim/Dockerfile")
    # context = repo root (last arg)
    assert argv[-1] == str(images_mod.REPO_ROOT)


def test_build_simulator_stages_workloads(fake_engine, monkeypatch):
    """A SIMULATOR build stages site.workloads_dir into the build context
    before invoking the engine; an agent build does NOT."""
    staged: list[str] = []
    monkeypatch.setattr(
        container_mod, "stage_workloads_for_build",
        lambda sim, rr: staged.append(sim))
    fake = _FakeRun(rc=0)
    monkeypatch.setattr(container_mod.subprocess, "run", fake)

    container_mod.build_image_from_manifest(
        "localhost/archbench-champsim:v6", repo_root=images_mod.REPO_ROOT)
    assert staged == ["champsim"], "simulator build must stage workloads"
    built = fake.argvs_with("build", "localhost/archbench-champsim:v6")
    assert built and built[0][0] == "docker"

    staged.clear()
    container_mod.build_image_from_manifest(
        "localhost/archbench-agent-mini:v6", repo_root=images_mod.REPO_ROOT)
    assert staged == [], "agent build must NOT stage workloads"


def test_build_unknown_tag_raises_image_not_found(fake_engine):
    with pytest.raises(container_mod.ImageNotFoundError):
        container_mod.build_image_from_manifest(
            "localhost/archbench-nope:v0", repo_root=images_mod.REPO_ROOT)


def test_cli_build_dry_run_all_builds_nothing(fake_engine, monkeypatch, capsys):
    """`archbench images build all --dry-run` prints a plan for every image and
    NEVER calls the build helper or the engine."""
    called = {"build": 0}
    monkeypatch.setattr(
        container_mod, "build_image_from_manifest",
        lambda *a, **k: called.__setitem__("build", called["build"] + 1))
    fake = _FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)

    rc = cli.cmd_images_build(_args(target="all", dry_run=True))
    assert rc == 0
    assert called["build"] == 0, "dry-run must not build"
    out = capsys.readouterr().out
    # A line for every manifest image.
    for _, _, fq in images_mod.iter_images():
        assert fq in out
    # The dry-run argv is shown (engine-shimmed) but not executed.
    assert "docker build -t localhost/archbench-champsim:v6" in out
    assert "DRY-RUN" in out


def test_cli_build_sim_agent_delegates_to_recipe(fake_engine, monkeypatch, capsys):
    """A sim_agents target invokes the recipe script (build_l2agent_image.sh),
    NOT build_image_from_manifest."""
    called = {"build": 0}
    monkeypatch.setattr(
        container_mod, "build_image_from_manifest",
        lambda *a, **k: called.__setitem__("build", called["build"] + 1))
    fake = _FakeRun(rc=0)
    # cmd_images_build imports subprocess lazily; patch the real module's run.
    monkeypatch.setattr(subprocess, "run", fake)

    rc = cli.cmd_images_build(_args(target="sim_agents/champsim", dry_run=False))
    assert rc == 0
    assert called["build"] == 0, "sim_agent must NOT use the Dockerfile builder"
    recipe_calls = fake.argvs_with("build_l2agent_image.sh", "champsim")
    assert recipe_calls, f"expected recipe invocation, calls={fake.calls}"
    assert recipe_calls[0][0] == "bash"


# ---------------------------------------------------------------------------
# (1) save / load / rm / pull argv shapes
# ---------------------------------------------------------------------------


def test_save_argv_and_tar_path(fake_engine, monkeypatch, tmp_path, capsys):
    """save -> `<engine> save -o <tar_dir>/<slug>/<repo>-<tag>.tar <image>`
    and only for LOCAL images (skips absent)."""
    from archbench.image_management import site as site_mod

    fake = _FakeRun(rc=0)
    monkeypatch.setattr(subprocess, "run", fake)
    # champsim is "local"; gem5 is not.
    monkeypatch.setattr(
        container_mod, "get_image_digest",
        lambda img: "sha256:abc" if "champsim" in img else None)
    # tar_dir -> tmp_path so we don't write into the repo pool.
    monkeypatch.setattr(
        cli, "DEFAULT_TAR_SEARCH", [tmp_path])
    monkeypatch.setattr(
        site_mod, "load_site",
        lambda *a, **k: SimpleNamespace(tar_dir=tmp_path))

    rc = cli.cmd_images_save(_args(target="simulators"))
    assert rc == 0
    saved = fake.argvs_with("save", "-o", "localhost/archbench-champsim:v6")
    assert saved, f"no save argv captured: {fake.calls}"
    argv = saved[0]
    assert argv[0] == "docker"
    out_idx = argv.index("-o")
    out_path = Path(argv[out_idx + 1])
    # slug-subdir form: <tar_dir>/champsim/archbench-champsim-v6.tar
    assert out_path == tmp_path / "champsim" / "archbench-champsim-v6.tar"
    # gem5 (not local) was skipped -> no save argv for it.
    assert not fake.argvs_with("save", "localhost/archbench-gem5:v7")


def test_save_tar_path_matches_ensure_image_first_candidate(tmp_path):
    """The pool path save writes MUST be the FIRST tar candidate ensure_image
    reads (slug-subdir form) so a saved tar reloads."""
    p = cli._save_tar_path("localhost/archbench-champsim:v6", tmp_path)
    assert p == tmp_path / "champsim" / "archbench-champsim-v6.tar"
    # mirror container.ensure_image's candidate[0] derivation.
    bare = "localhost/archbench-champsim:v6".split("/")[-1]
    name, _, tag = bare.partition(":")
    slug = name[len("archbench-"):]
    assert p == tmp_path / slug / f"{name}-{tag}.tar"


def test_load_uses_ensure_image(fake_engine, monkeypatch, capsys):
    """load -> ensure_image() per resolved image (reuses the tar path)."""
    seen: list[str] = []

    def fake_ensure(image, dirs, *a, **k):
        seen.append(image)
        return "sha256:deadbeef0000"

    monkeypatch.setattr(container_mod, "ensure_image", fake_ensure)
    rc = cli.cmd_images_load(_args(target="sim/champsim"))
    assert rc == 0
    assert seen == ["localhost/archbench-champsim:v6"]
    assert "OK" in capsys.readouterr().out


def test_load_missing_tar_reports_and_fails(fake_engine, monkeypatch, capsys):
    def fake_ensure(image, dirs, *a, **k):
        raise container_mod.ImageNotFoundError("no tar")

    monkeypatch.setattr(container_mod, "ensure_image", fake_ensure)
    rc = cli.cmd_images_load(_args(target="sim/champsim"))
    assert rc == 1
    assert "MISSING" in capsys.readouterr().out


def test_rm_argv_is_rmi_force(fake_engine, monkeypatch, capsys):
    fake = _FakeRun(rc=0)
    monkeypatch.setattr(subprocess, "run", fake)
    rc = cli.cmd_images_rm(_args(target="sim/champsim"))
    assert rc == 0
    rmi = fake.argvs_with("rmi", "-f", "localhost/archbench-champsim:v6")
    assert rmi and rmi[0][0] == "docker"


def test_pull_no_registry_exits_nonzero(fake_engine, monkeypatch):
    """pull with no registry configured -> rc=1, no engine call."""
    from archbench.image_management import site as site_mod
    monkeypatch.setattr(
        site_mod, "load_site",
        lambda *a, **k: SimpleNamespace(registry=""))
    fake = _FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    rc = cli.cmd_images_pull(_args(target="champsim"))
    assert rc == 1
    assert not fake.calls, "no engine call when registry is unset"


def test_pull_with_registry_pulls_then_tags(fake_engine, monkeypatch, capsys):
    """pull -> `<engine> pull <registry>/<repo:tag>` then `<engine> tag ...`."""
    from archbench.image_management import site as site_mod
    monkeypatch.setattr(
        site_mod, "load_site",
        lambda *a, **k: SimpleNamespace(registry="myreg.io/team"))
    fake = _FakeRun(rc=0)
    monkeypatch.setattr(subprocess, "run", fake)

    rc = cli.cmd_images_pull(_args(target="sim/champsim"))
    assert rc == 0
    pulls = fake.argvs_with("pull", "myreg.io/team/archbench-champsim:v6")
    tags = fake.argvs_with("tag", "myreg.io/team/archbench-champsim:v6",
                           "localhost/archbench-champsim:v6")
    assert pulls and pulls[0][0] == "docker"
    assert tags and tags[0][0] == "docker"


# ---------------------------------------------------------------------------
# (1) gc targets ONLY archbench_* containers + dangling layers
# ---------------------------------------------------------------------------


def test_gc_targets_only_archbench_containers_and_dangling(fake_engine, monkeypatch, capsys):
    """gc filters containers by name=archbench_ and prunes only dangling images."""
    fake = _FakeRun(rc=0)
    fake.when(["docker", "ps", "-aq", "--filter", "name=archbench_"],
              stdout="ctr1\nctr2\n")
    monkeypatch.setattr(subprocess, "run", fake)

    rc = cli.cmd_images_gc(_args(dry_run=False))
    assert rc == 0
    # The container query is name-filtered to archbench_.
    ps = fake.argvs_with("ps", "-aq", "--filter", "name=archbench_")
    assert ps, "gc must filter containers by name=archbench_"
    # It removes exactly the two archbench_ ids it found.
    rm = fake.argvs_with("rm", "-f", "ctr1", "ctr2")
    assert rm, f"gc must rm -f the found archbench_ ids: {fake.calls}"
    # Dangling prune is the conservative `image prune -f` (NOT `-a`).
    prune = fake.argvs_with("image", "prune", "-f")
    assert prune
    assert not any("-a" in c for c in prune), "gc must NOT prune all images"


def test_gc_dry_run_reaps_nothing(fake_engine, monkeypatch, capsys):
    fake = _FakeRun(rc=0)
    fake.when(["docker", "ps", "-aq", "--filter", "name=archbench_"],
              stdout="ctr1\n")
    fake.when(["docker", "images", "-qf", "dangling=true"], stdout="")
    monkeypatch.setattr(subprocess, "run", fake)

    rc = cli.cmd_images_gc(_args(dry_run=True))
    assert rc == 0
    # No destructive call in dry-run: no `rm -f`, no `image prune`.
    assert not fake.argvs_with("rm", "-f")
    assert not fake.argvs_with("image", "prune")
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "ctr1" in out


# ---------------------------------------------------------------------------
# (2) gated autobuild fallback in ensure_image
# ---------------------------------------------------------------------------


def _stub_engine_for_container(monkeypatch):
    monkeypatch.setattr(container_mod, "container_engine", lambda: "docker")


def test_autobuild_skipped_under_slurm(monkeypatch, tmp_path):
    """With SLURM_JOB_ID set, a missing image (no local, no tar) raises
    ImageNotFoundError and NEVER builds (a batch node must not drift the
    provenance gate — CLAUDE.md §1.16)."""
    _stub_engine_for_container(monkeypatch)
    monkeypatch.setenv("SLURM_JOB_ID", "12345")
    monkeypatch.delenv("ARCHBENCH_NO_AUTOBUILD", raising=False)
    monkeypatch.setattr(container_mod, "get_image_digest", lambda img: None)
    monkeypatch.setattr(container_mod.subprocess, "run", _FakeRun(rc=0))

    built = {"n": 0}
    monkeypatch.setattr(
        container_mod, "build_image_from_manifest",
        lambda *a, **k: built.__setitem__("n", built["n"] + 1))

    with pytest.raises(container_mod.ImageNotFoundError) as e:
        container_mod.ensure_image("localhost/archbench-champsim:v6", [tmp_path])
    assert built["n"] == 0, "must NOT build under SLURM_JOB_ID"
    assert "autobuild SKIPPED" in str(e.value)


def test_autobuild_skipped_with_archbench_no_autobuild(monkeypatch, tmp_path):
    _stub_engine_for_container(monkeypatch)
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    monkeypatch.setenv("ARCHBENCH_NO_AUTOBUILD", "1")
    monkeypatch.setattr(container_mod, "get_image_digest", lambda img: None)
    monkeypatch.setattr(container_mod.subprocess, "run", _FakeRun(rc=0))
    built = {"n": 0}
    monkeypatch.setattr(
        container_mod, "build_image_from_manifest",
        lambda *a, **k: built.__setitem__("n", built["n"] + 1))

    with pytest.raises(container_mod.ImageNotFoundError):
        container_mod.ensure_image("localhost/archbench-champsim:v6", [tmp_path])
    assert built["n"] == 0


def test_autobuild_attempted_when_interactive(monkeypatch, tmp_path):
    """Interactive (no SLURM_JOB_ID, no ARCHBENCH_NO_AUTOBUILD, no registry) -> the
    missing image is BUILT from its Dockerfile. The build is mocked; we only
    assert it was attempted and the resulting digest returned."""
    _stub_engine_for_container(monkeypatch)
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    monkeypatch.delenv("ARCHBENCH_NO_AUTOBUILD", raising=False)
    monkeypatch.delenv("ARCHBENCH_FORCE_IMAGE_RELOAD", raising=False)
    # No registry -> the build branch (not pull). container.py binds load_site
    # at import (`from archbench.image_management.site import load_site`), so patch THAT name.
    monkeypatch.setattr(
        container_mod, "load_site",
        lambda *a, **k: SimpleNamespace(registry=""))

    # digest: None before the build, a value after (simulate a successful build).
    state = {"built": False}

    def fake_digest(img):
        return "sha256:freshbuild00" if state["built"] else None

    def fake_build(image, *a, **k):
        state["built"] = True

    monkeypatch.setattr(container_mod, "get_image_digest", fake_digest)
    monkeypatch.setattr(container_mod, "build_image_from_manifest", fake_build)

    digest = container_mod.ensure_image(
        "localhost/archbench-champsim:v6", [tmp_path])
    assert state["built"] is True, "interactive autobuild must build"
    assert digest == "sha256:freshbuild00"


def test_autobuild_prefers_pull_when_registry_set(monkeypatch, tmp_path):
    """Interactive + registry configured -> PULL (then tag), not build."""
    _stub_engine_for_container(monkeypatch)
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    monkeypatch.delenv("ARCHBENCH_NO_AUTOBUILD", raising=False)
    monkeypatch.setattr(
        container_mod, "load_site",
        lambda *a, **k: SimpleNamespace(registry="myreg.io"))

    state = {"pulled": False}

    def fake_digest(img):
        return "sha256:pulled000000" if state["pulled"] else None

    fake = _FakeRun(rc=0)
    real_call = fake.__call__

    def run_recording(cmd, *a, **k):
        argv = list(cmd)
        if argv[:2] == ["docker", "pull"]:
            state["pulled"] = True
        return real_call(cmd, *a, **k)

    monkeypatch.setattr(container_mod, "get_image_digest", fake_digest)
    monkeypatch.setattr(container_mod.subprocess, "run", run_recording)
    built = {"n": 0}
    monkeypatch.setattr(
        container_mod, "build_image_from_manifest",
        lambda *a, **k: built.__setitem__("n", built["n"] + 1))

    digest = container_mod.ensure_image(
        "localhost/archbench-champsim:v6", [tmp_path])
    assert state["pulled"] is True
    assert built["n"] == 0, "registry set -> pull, never build"
    assert digest == "sha256:pulled000000"
    # argv: pull <registry>/<repo:tag>
    assert fake.argvs_with("pull", "myreg.io/archbench-champsim:v6")


def test_autobuild_does_not_fire_on_origin_box_local_image(monkeypatch, tmp_path):
    """Behavior-preserving: when the image is already local, ensure_image
    returns its digest WITHOUT consulting the autobuild path at all."""
    _stub_engine_for_container(monkeypatch)
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    monkeypatch.delenv("ARCHBENCH_NO_AUTOBUILD", raising=False)
    monkeypatch.delenv("ARCHBENCH_FORCE_IMAGE_RELOAD", raising=False)
    monkeypatch.setattr(
        container_mod, "get_image_digest", lambda img: "sha256:already0000")
    built = {"n": 0}
    monkeypatch.setattr(
        container_mod, "build_image_from_manifest",
        lambda *a, **k: built.__setitem__("n", built["n"] + 1))

    digest = container_mod.ensure_image(
        "localhost/archbench-champsim:v6", [tmp_path])
    assert digest == "sha256:already0000"
    assert built["n"] == 0


# ---------------------------------------------------------------------------
# stage_workloads_for_build (the autobuild blocker fix)
# ---------------------------------------------------------------------------


def test_stage_workloads_noop_when_present(monkeypatch, tmp_path):
    """If <repo>/workload_pools/<sim> already exists, staging is a no-op."""
    repo = tmp_path / "repo"
    (repo / "workload_pools" / "champsim").mkdir(parents=True)
    got = container_mod.stage_workloads_for_build("champsim", repo)
    assert got == repo / "workload_pools" / "champsim"


def test_stage_workloads_symlinks_from_site(monkeypatch, tmp_path):
    """Absent in the repo -> symlink from site.workloads_dir/<sim>."""
    from archbench.image_management import site as site_mod
    repo = tmp_path / "repo"
    repo.mkdir()
    site_pool = tmp_path / "sitepool"
    (site_pool / "champsim").mkdir(parents=True)
    monkeypatch.setattr(
        container_mod, "load_site",
        lambda *a, **k: SimpleNamespace(workloads_dir=site_pool))
    got = container_mod.stage_workloads_for_build("champsim", repo)
    assert got == repo / "workload_pools" / "champsim"
    assert got.is_symlink()
    assert got.resolve() == (site_pool / "champsim").resolve()


def test_stage_workloads_none_when_no_site_workloads(monkeypatch, tmp_path):
    """No workloads anywhere -> returns None (build proceeds without staging;
    a Dockerfile that needs them fails loudly at COPY, the correct signal)."""
    from archbench.image_management import site as site_mod
    repo = tmp_path / "repo"
    repo.mkdir()
    site_pool = tmp_path / "empty_sitepool"
    site_pool.mkdir()
    monkeypatch.setattr(
        container_mod, "load_site",
        lambda *a, **k: SimpleNamespace(workloads_dir=site_pool))
    got = container_mod.stage_workloads_for_build("champsim", repo)
    assert got is None


# ---------------------------------------------------------------------------
# digest verb
# ---------------------------------------------------------------------------


def test_digest_local_prints_full_digest(fake_engine, monkeypatch, capsys):
    monkeypatch.setattr(
        container_mod, "get_image_digest",
        lambda img: "sha256:abcdef123456")
    rc = cli.cmd_images_digest(_args(name="champsim"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "localhost/archbench-champsim:v6" in out
    assert "sha256:abcdef123456" in out


def test_digest_absent_image_exits_nonzero(fake_engine, monkeypatch, capsys):
    monkeypatch.setattr(container_mod, "get_image_digest", lambda img: None)
    rc = cli.cmd_images_digest(_args(name="champsim"))
    assert rc == 1
    assert "not local" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# reverse-lookup helpers (images.py)
# ---------------------------------------------------------------------------


def test_find_by_tag_forward_reverse_roundtrip(manifest):
    for cat, key, fq in images_mod.iter_images(manifest):
        assert images_mod.find_by_tag(fq, manifest) == (cat, key)


def test_find_by_tag_bare_local_tag(manifest):
    assert images_mod.find_by_tag("archbench-champsim:v6", manifest) == (
        "simulators", "champsim")


def test_build_context_for_simulator(manifest):
    df, ctx = images_mod.build_context_for(
        "localhost/archbench-champsim:v6", manifest, repo_root=images_mod.REPO_ROOT)
    assert df == images_mod.REPO_ROOT / "simulators/champsim/Dockerfile"
    assert ctx == images_mod.REPO_ROOT


def test_build_context_for_sim_agent_is_none(manifest):
    """sim_agents have no `build:` Dockerfile (recipe-only) -> None."""
    assert images_mod.build_context_for(
        "localhost/archbench-champsim-l2agent:v6", manifest) is None


def test_recipe_for_sim_agent(manifest):
    rec = images_mod.recipe_for("localhost/archbench-champsim-l2agent:v6", manifest)
    assert rec is not None
    assert rec["recipe"] == "scripts/build_l2agent_image.sh"
    assert rec["base"] == "champsim"


def test_recipe_for_simulator_is_none(manifest):
    assert images_mod.recipe_for("localhost/archbench-champsim:v6", manifest) is None
