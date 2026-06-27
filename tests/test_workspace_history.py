"""Tests for ``archbench.core.workspace_history``.

The replay function produces per-turn workspace snapshots from a
trajectory.jsonl. We pin:

  * 3 Write events  -> 3 snapshot dirs, content reflects each write
  * 1 Write + 1 Edit -> 2 snapshot dirs, second has edit applied
  * Missing trajectory -> 0 snapshots, no exception
  * Same fixture in claude_code vs. mini schema -> same snapshot
    contents (validates the schema dispatcher)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from archbench.core.workspace_history import replay_workspace_history


# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------


def _write_claude_code_trajectory(
    path: Path, events: list[tuple[str, str, dict]],
) -> None:
    """Write a fixture trajectory.jsonl in Claude Code schema.

    ``events`` is a list of (kind, file_path, payload):
      ("write", "/workspace/foo.cc", {"content": "..."})
      ("edit",  "/workspace/foo.cc", {"old_string": "a", "new_string": "b"})
    """
    rows: list[dict] = []
    rows.append({"type": "system", "subtype": "init"})  # banner
    for kind, fp, payload in events:
        if kind == "write":
            inp = {"file_path": fp, "content": payload["content"]}
            block = {"type": "tool_use", "name": "Write", "input": inp}
        elif kind == "edit":
            inp = {
                "file_path": fp,
                "old_string": payload["old_string"],
                "new_string": payload["new_string"],
            }
            block = {"type": "tool_use", "name": "Edit", "input": inp}
        else:
            raise AssertionError(f"unknown kind {kind!r}")
        rows.append({
            "type": "assistant",
            "message": {"content": [block]},
        })
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _write_mini_trajectory(
    path: Path, events: list[tuple[str, str, dict]],
) -> None:
    """Write a fixture trajectory.jsonl in mini / archharness schema."""
    rows: list[dict] = []
    rows.append({"type": "thread.started"})
    for i, (kind, fp, payload) in enumerate(events, start=1):
        rows.append({"type": "turn.started", "turn": {"number": i}})
        if kind == "write":
            change = {"path": fp, "kind": "write", "content": payload["content"]}
        elif kind == "edit":
            change = {
                "path": fp, "kind": "edit",
                "old_string": payload["old_string"],
                "new_string": payload["new_string"],
            }
        else:
            raise AssertionError(f"unknown kind {kind!r}")
        rows.append({
            "type": "item.completed",
            "item": {"type": "file_change", "id": f"t{i}", "changes": [change]},
        })
        rows.append({"type": "turn.completed", "turn": {"number": i}})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_replay_three_writes_claude_code(tmp_path: Path) -> None:
    """3 Write events -> 3 snapshot dirs with the written content."""
    traj = tmp_path / "trajectory.jsonl"
    out = tmp_path / "wh"
    _write_claude_code_trajectory(traj, [
        ("write", "/workspace/a.cc", {"content": "AAA"}),
        ("write", "/workspace/b.cc", {"content": "BBB"}),
        ("write", "/workspace/a.cc", {"content": "AAA2"}),
    ])
    n = replay_workspace_history(traj, starter_files={}, out_dir=out)
    assert n == 3
    dirs = sorted(p.name for p in out.iterdir())
    assert dirs == [
        "turn_001_write_a.cc",
        "turn_002_write_b.cc",
        "turn_003_write_a.cc",
    ]
    # turn_001: only a.cc, content AAA
    assert (out / "turn_001_write_a.cc" / "a.cc").read_text() == "AAA"
    # turn_002: both, a.cc still AAA, b.cc=BBB
    t2 = out / "turn_002_write_b.cc"
    assert (t2 / "a.cc").read_text() == "AAA"
    assert (t2 / "b.cc").read_text() == "BBB"
    # turn_003: a.cc overwritten to AAA2, b.cc still BBB
    t3 = out / "turn_003_write_a.cc"
    assert (t3 / "a.cc").read_text() == "AAA2"
    assert (t3 / "b.cc").read_text() == "BBB"


def test_replay_write_then_edit_claude_code(tmp_path: Path) -> None:
    """1 Write + 1 Edit -> 2 snapshots, edit applied via str.replace."""
    traj = tmp_path / "trajectory.jsonl"
    out = tmp_path / "wh"
    _write_claude_code_trajectory(traj, [
        ("write", "/workspace/foo.cc", {"content": "alpha BETA gamma"}),
        ("edit",  "/workspace/foo.cc", {
            "old_string": "BETA", "new_string": "DELTA",
        }),
    ])
    n = replay_workspace_history(traj, starter_files={}, out_dir=out)
    assert n == 2
    t1 = (out / "turn_001_write_foo.cc" / "foo.cc").read_text()
    t2 = (out / "turn_002_edit_foo.cc" / "foo.cc").read_text()
    assert t1 == "alpha BETA gamma"
    assert t2 == "alpha DELTA gamma"


def test_replay_missing_trajectory_is_noop(tmp_path: Path) -> None:
    """Missing trajectory.jsonl returns 0, no exception, no out_dir polluted."""
    out = tmp_path / "wh"
    n = replay_workspace_history(
        trajectory_path=tmp_path / "does_not_exist.jsonl",
        starter_files={"/workspace/foo.cc": "starter"},
        out_dir=out,
    )
    assert n == 0
    # out_dir may or may not exist; if it does, it should be empty.
    if out.exists():
        assert list(out.iterdir()) == []


def test_replay_starter_files_present_in_first_snapshot(tmp_path: Path) -> None:
    """Starter files appear in every snapshot, even before any write touches them."""
    traj = tmp_path / "trajectory.jsonl"
    out = tmp_path / "wh"
    _write_claude_code_trajectory(traj, [
        ("write", "/workspace/new.cc", {"content": "new content"}),
    ])
    n = replay_workspace_history(
        traj,
        starter_files={"/workspace/starter.h": "starter content"},
        out_dir=out,
    )
    assert n == 1
    t1 = out / "turn_001_write_new.cc"
    # starter still there alongside the new file
    assert (t1 / "starter.h").read_text() == "starter content"
    assert (t1 / "new.cc").read_text() == "new content"


def test_replay_schema_dispatch_parity(tmp_path: Path) -> None:
    """Same logical events in claude_code vs. mini schema produce same snapshots.

    This is the dispatcher-reuse contract: a future schema-helper
    refactor must not change the replay output for either schema.
    """
    events = [
        ("write", "/workspace/foo.cc", {"content": "v1"}),
        ("edit",  "/workspace/foo.cc", {
            "old_string": "v1", "new_string": "v2",
        }),
        ("write", "/workspace/bar.cc", {"content": "B"}),
    ]
    cc_traj = tmp_path / "cc.jsonl"
    mini_traj = tmp_path / "mini.jsonl"
    cc_out = tmp_path / "cc_wh"
    mini_out = tmp_path / "mini_wh"
    _write_claude_code_trajectory(cc_traj, events)
    _write_mini_trajectory(mini_traj, events)

    n_cc = replay_workspace_history(cc_traj, {}, cc_out)
    n_mini = replay_workspace_history(mini_traj, {}, mini_out)
    assert n_cc == n_mini == 3

    cc_dirs = sorted(p.name for p in cc_out.iterdir())
    mini_dirs = sorted(p.name for p in mini_out.iterdir())
    assert cc_dirs == mini_dirs

    # Spot-check the final content matches across schemas.
    for d in cc_dirs:
        cc_files = sorted(p.name for p in (cc_out / d).iterdir())
        mini_files = sorted(p.name for p in (mini_out / d).iterdir())
        assert cc_files == mini_files
        for fn in cc_files:
            assert (cc_out / d / fn).read_text() == (mini_out / d / fn).read_text()


def test_replay_edit_with_missing_old_string_is_idempotent(tmp_path: Path) -> None:
    """Edit whose old_string isn't in the file leaves content unchanged.

    Mirrors Claude Code's Edit semantics (str.replace with count=1 is
    a no-op if the needle is absent). The snapshot is still produced
    so the event sequence stays intact.
    """
    traj = tmp_path / "trajectory.jsonl"
    out = tmp_path / "wh"
    _write_claude_code_trajectory(traj, [
        ("write", "/workspace/foo.cc", {"content": "alpha"}),
        ("edit",  "/workspace/foo.cc", {
            "old_string": "MISSING", "new_string": "X",
        }),
    ])
    n = replay_workspace_history(traj, {}, out)
    assert n == 2
    t1 = (out / "turn_001_write_foo.cc" / "foo.cc").read_text()
    t2 = (out / "turn_002_edit_foo.cc" / "foo.cc").read_text()
    assert t1 == "alpha"
    assert t2 == "alpha"


def test_replay_skips_unparseable_rows(tmp_path: Path) -> None:
    """Non-JSON lines and empty lines are skipped without crashing."""
    traj = tmp_path / "trajectory.jsonl"
    # Mix valid + garbage.
    valid = {
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use", "name": "Write",
            "input": {"file_path": "/workspace/a.cc", "content": "x"},
        }]},
    }
    traj.write_text(
        "not-json-at-all\n"
        + "\n"  # blank
        + json.dumps(valid) + "\n"
        + "{\"truncated\":\n"  # malformed
    )
    n = replay_workspace_history(traj, {}, tmp_path / "wh")
    assert n == 1


def test_replay_no_file_mutating_events(tmp_path: Path) -> None:
    """A trajectory with no Write/Edit events writes zero snapshots."""
    traj = tmp_path / "trajectory.jsonl"
    # Only reasoning + bash, no file_change.
    rows = [
        {"type": "thread.started"},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "hm"}},
        {"type": "item.completed", "item": {
            "type": "command_execution", "command": "ls", "status": "completed",
        }},
    ]
    traj.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    out = tmp_path / "wh"
    n = replay_workspace_history(traj, {}, out)
    assert n == 0
