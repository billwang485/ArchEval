#!/usr/bin/env python3.11
"""verify_workspace.py <challenge_dir> [--runtime mini] [--no-force-reload]

Prove the AGENT DOCKER actually matches the challenge.yaml workspace_setup
manifest. Stages a real agent container exactly as the harness does
(resolve_images -> ContainerManager.start -> runtime.stage_workspace), lists the
agent-visible files, and diffs them against the declared manifest:

  - MISSING : the manifest declares a file the container does NOT have.
  - EXTRA   : the container has an agent-visible file NOT in the manifest
              (the neutrality concern — a surprise file the agent could see).

agent_centric tiers (L1/L3): checks the staged /workspace + /api (the agent's
whole task tree). simulator_centric (L2): the agent's PRIMARY view is the BAKED
/work sim source (not staged, intentionally) — this tool checks the staged
/workspace + /api bits and prints a note that /work is the baked sim; run
scripts/neutrality_audit.sh for the /work neutrality gate.

Must run on a node with the container engine (e.g. via srun). Exit 0 = match.
"""
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from archbench.core.challenge import load_challenge                       # noqa: E402
from archbench.simulators import get_plugin                              # noqa: E402
from archbench.runtimes import runtime_from_challenge                    # noqa: E402
from archbench.image_management.plan import resolve_images              # noqa: E402
from archbench.image_management.cli import DEFAULT_TAR_SEARCH           # noqa: E402
from archbench.core.container import (                                   # noqa: E402
    ContainerManager, ContainerConfig, ensure_image,
)


def manifest_paths(cdir: Path):
    d = yaml.safe_load((cdir / "challenge.yaml").read_text())
    ws = d.get("workspace_setup")
    if ws is None:
        return []
    if isinstance(ws, str):
        return None  # un-migrated prose
    return [f["container_path"] for f in (ws.get("files") or []) if "container_path" in f]


def main():
    argv = sys.argv[1:]
    pos = [a for a in argv if not a.startswith("--")]
    if not pos:
        print("usage: verify_workspace.py <challenge_dir> [--runtime mini] [--no-force-reload]", file=sys.stderr)
        sys.exit(2)
    cdir = Path(pos[0])
    rt_name = argv[argv.index("--runtime") + 1] if "--runtime" in argv else "mini"
    force = "--no-force-reload" not in argv

    ch = load_challenge(str(cdir))
    expected = manifest_paths(cdir)
    if expected is None:
        print(f"FAIL {cdir}: workspace_setup is still prose — run gen_workspace_manifest.py --write")
        sys.exit(2)

    plugin = get_plugin(ch.simulator)
    runtime = runtime_from_challenge(rt_name, ch)
    plan = resolve_images(ch, plugin, runtime)
    agent_image = plan.agent_image
    mode = getattr(ch, "agent_image_mode", "agent_centric")
    sv = getattr(ch, "starter_visibility", "full")
    print(f"===== verify_workspace {cdir} =====")
    print(f"  agent_image_mode={mode}  starter_visibility={sv}")
    print(f"  agent_image={agent_image}  (runtime={rt_name})")

    ensure_image(agent_image, DEFAULT_TAR_SEARCH, force_reload=force)
    cfg = ContainerConfig.with_run_id(agent_image, "archbench_verify")
    agent = ContainerManager(cfg)
    agent.start()
    try:
        runtime.stage_workspace(agent, ch, starter_visibility=sv)
        out, rc = agent.exec("find /workspace /api -type f 2>/dev/null | sort", timeout=60)
        # keep only real find paths (absolute); drops container-engine noise like
        # podman's "Emulate Docker CLI using podman..." line that lands on stdout.
        actual = [l.strip() for l in out.splitlines() if l.strip().startswith(("/workspace", "/api"))]
    finally:
        try:
            agent.stop()
        except Exception:
            pass

    exp, act = set(expected), set(actual)
    missing = sorted(exp - act)
    extra = sorted(act - exp)
    print(f"  manifest declares {len(exp)} files; container has {len(act)} agent-visible files")
    for m in missing:
        print(f"  MISSING (declared, not in container): {m}")
    for e in extra:
        print(f"  EXTRA   (in container, not declared): {e}")
    if mode == "simulator_centric":
        print("  NOTE: L2 agent's primary view is the BAKED /work sim source (not staged);")
        print("        run scripts/neutrality_audit.sh for the /work neutrality gate.")
    if not missing and not extra:
        print(f"  VERIFY_WORKSPACE OK {cdir} — container matches manifest exactly")
        sys.exit(0)
    print(f"  VERIFY_WORKSPACE MISMATCH {cdir} — {len(missing)} missing, {len(extra)} extra")
    sys.exit(1)


if __name__ == "__main__":
    main()
