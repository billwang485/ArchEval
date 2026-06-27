#!/usr/bin/env python3.11
"""gen_workspace_manifest.py <challenge_dir> [--write]

Generate the STRUCTURED workspace_setup manifest for a challenge — the explicit
"what the agent sees in its container, and where each file is loaded from on the
host" contract. Replaces the old free-prose `workspace_setup: |` block.

Source of truth: this REPLICATES the file-resolution in
archbench.core.runtime_base.AgentRuntime.stage_workspace (starter_code,
check_storage.py, docs->/api, requirements.txt) + submission_files. It imports
load_challenge + resolved_dirs so the dir resolution is EXACT; the candidate
lists below mirror stage_workspace and must stay in sync (the verify tool
catches drift against a live container).

Emitted shape (per the format the user confirmed):

  workspace_setup:
    files:
      - name: <basename>
        container_path: /workspace/...        # where the agent sees it
        host_path: challenges/.../file        # repo-relative host source
        note: <optional>
    deliverables:
      - /workspace/...                          # what the agent must write

Default is DRY-RUN (prints the new block). Pass --write to rewrite the
challenge.yaml in place (surgical block replace; the rest of the file is
untouched).
"""
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from archbench.core.challenge import load_challenge          # noqa: E402
from archbench.core.path_resolution import resolved_dirs     # noqa: E402


def _rel(p: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(REPO))
    except Exception:
        return str(p)


def compute_manifest(challenge_dir: Path):
    """Return (files, deliverables).

    files: list of {name, container_path, host_path, note?}
    """
    ch = load_challenge(str(challenge_dir))
    sim_dir, eval_dir, starter_dir = resolved_dirs(ch)
    files = []

    def add(container: str, host: Path, note=None):
        e = {"name": os.path.basename(container.rstrip("/")),
             "container_path": container, "host_path": _rel(host)}
        if note:
            e["note"] = note
        files.append(e)

    # 1. Protocol v2: the unified family starter is staged read-only to
    # /workspace/starter/ for EVERY tier — never mirrored to the root.
    for fname in sorted(getattr(ch, "starter_code", {}) or {}):
        host = starter_dir / fname
        add(f"/workspace/starter/{fname}", host,
            "unified family starter staged read-only under /workspace/starter/ "
            "(same for every tier); not mirrored to /workspace root")

    # 2. pre-submit self-check -> /workspace/ (first existing candidate).
    # New canonical name is validate.py; check_storage.py is the legacy name
    # (champsim, where it checks a storage bit-budget) — accepted as fallback
    # during the per-challenge rename migration.
    _names = ["validate.py", "check_storage.py"]
    _dirs = [sim_dir, ch.challenge_dir / "simulator", ch.challenge_dir,
             ch.challenge_dir / "eval", ch.challenge_dir / "evaluation"]
    for cand in [d / n for n in _names for d in _dirs]:
        if cand.exists():
            add(f"/workspace/{cand.name}", cand, "pre-submit self-check; each challenge's evaluate.sh decides if it is also a gate")
            break

    # 3. docs/* -> /api/ (first existing dir; else repo docs/<sim>)
    docs_dir = next((d for d in [sim_dir / "docs", eval_dir / "docs", ch.challenge_dir / "docs"] if d.is_dir()), None)
    if docs_dir is None:
        cand = REPO / "docs" / ch.simulator
        docs_dir = cand if cand.is_dir() else None
    if docs_dir is not None:
        for fpath in sorted(docs_dir.rglob("*")):
            if fpath.is_file():
                relp = fpath.relative_to(docs_dir)
                add(f"/api/{relp}", fpath, "reference docs")

    # 4. requirements.txt -> /workspace/ (pip-installed at startup)
    for cand in [sim_dir / "requirements.txt",
                 ch.challenge_dir / "simulator" / "requirements.txt",
                 eval_dir / "requirements.txt",
                 ch.challenge_dir / "evaluation" / "requirements.txt",
                 ch.challenge_dir / "eval" / "requirements.txt",
                 ch.challenge_dir / "requirements.txt"]:
        if cand.is_file():
            add("/workspace/requirements.txt", cand, "pip-installed into the agent image at startup")
            break

    # Deliverables = what the agent must WRITE. Two sources, matching the
    # harness: (a) the plugin's submission_files == challenge.output_files
    # (+ config.json when config_tunable) — the code artifact(s); (b) the
    # `deliverable_files` evaluator's required_files — the markdown design docs
    # (P6 contract, wired on L1/L2 and sometimes L3).
    deliv: list[str] = []
    for f in (getattr(ch, "output_files", []) or []):
        if f not in deliv:
            deliv.append(f)
    if (getattr(ch, "simulator_config", {}) or {}).get("config_tunable"):
        if "config.json" not in deliv:
            deliv.append("config.json")
    for e in (getattr(ch, "evaluations", []) or []):
        if isinstance(e, dict) and e.get("evaluator") == "deliverable_files":
            for rf in ((e.get("config", {}) or {}).get("required_files", []) or []):
                if rf not in deliv:
                    deliv.append(rf)
    deliverables = [f if f.startswith("/") else f"/workspace/{f}" for f in deliv]

    return files, deliverables


def render_block(files, deliverables) -> str:
    """Render the structured workspace_setup YAML block (2-space indent)."""
    out = ["workspace_setup:"]
    out.append("  # Auto-generated by scripts/gen_workspace_manifest.py — the exact")
    out.append("  # files the agent sees in its container + the repo-relative host source")
    out.append("  # each is loaded from. Verify a live container with scripts/verify_workspace.py.")
    out.append("  files:")
    if not files:
        out.append("    []   # family ships no starter skeleton")
    for e in files:
        out.append(f"    - name: {e['name']}")
        out.append(f"      container_path: {e['container_path']}")
        out.append(f"      host_path: {e['host_path']}")
        if e.get("note"):
            out.append(f"      note: {e['note']}")
    out.append("  deliverables:")
    for d in deliverables:
        out.append(f"    - {d}")
    if not deliverables:
        out.append("    []")
    return "\n".join(out)


def replace_block(text: str, new_block: str):
    """Surgically replace the top-level workspace_setup: block; rest untouched."""
    lines = text.split("\n")
    start = next((i for i, l in enumerate(lines) if re.match(r"^workspace_setup\s*:", l)), None)
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].strip() == "":
            continue
        if re.match(r"^\S", lines[j]):   # next top-level key (indent 0)
            end = j
            break
    new_lines = lines[:start] + new_block.split("\n") + lines[end:]
    return "\n".join(new_lines)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    write = "--write" in sys.argv
    if not args:
        print("usage: gen_workspace_manifest.py <challenge_dir> [--write]", file=sys.stderr)
        sys.exit(2)
    cdir = Path(args[0])
    yaml_path = cdir / "challenge.yaml"
    if not yaml_path.is_file():
        print(f"no challenge.yaml at {cdir}", file=sys.stderr)
        sys.exit(2)
    files, deliverables = compute_manifest(cdir)
    block = render_block(files, deliverables)
    if not write:
        print(f"# ===== DRY RUN: {yaml_path} =====")
        print(block)
        print(f"# ({len(files)} staged files, {len(deliverables)} deliverables)")
        return
    text = yaml_path.read_text()
    new_text = replace_block(text, block)
    if new_text is None:
        print(f"SKIP {yaml_path}: no workspace_setup block found")
        return
    yaml_path.write_text(new_text)
    print(f"WROTE {yaml_path} ({len(files)} files, {len(deliverables)} deliverables)")


if __name__ == "__main__":
    main()
