"""workspace_history — replay Write/Edit events from trajectory.jsonl.

Phase E feature. After a session ends ``run_session`` already copies
``/workspace/`` out of the agent container into
``results/<challenge>/<run>/workspace/`` (a single final snapshot). For
diagnosis we also want a **per-turn** history: what did the agent's
workspace look like after each file-mutating event? That makes it
possible to bisect at the granularity of one tool call when a run goes
sideways.

This module replays every Write/Edit/file_change event from
``trajectory.jsonl`` against the challenge's starter files, writing one
snapshot directory per file-mutating event under
``results/<challenge>/<run>/workspace_history/``.

Schema support
--------------
Two trajectory schemas are recognized, matching
``evaluators/trajectory_audit/evaluator.py``:

  * **claude_code** — Anthropic-style. ``Write`` and ``Edit`` are
    surfaced as ``tool_use`` blocks inside ``assistant`` rows.
    ``Write`` carries ``{file_path, content}``; ``Edit`` carries
    ``{file_path, old_string, new_string}``.

  * **mini / archharness** — in-house. ``item.completed`` rows whose
    ``item.type == "file_change"`` carry ``changes: [{path, kind,
    content}]``. The runners (``runtimes/mini/src/main.py``,
    ``runtimes/archharness/src/main.py``) were extended in Phase E to
    include ``kind`` + ``content`` per change so this replay works
    end-to-end.

Best-effort guarantees
----------------------
- If a row's content payload is missing (e.g. legacy mini trajectories
  prior to the Phase E ``kind/content`` extension), the event is
  recorded as a snapshot directory whose file content is the empty
  string — so ``turn_NNN`` numbering stays aligned with the
  trajectory's event order, and downstream diff tools still get a
  marker for the event.
- A missing trajectory is a no-op (returns 0).
- Any exception is the caller's problem; ``run_session`` wraps this
  function in its own try/except so a malformed trajectory never
  fails the session-end path.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterator, Optional


log = logging.getLogger("archbench.core.workspace_history")


# Cap on the number of snapshots we'll write. Long runs (Claude Code
# with hundreds of Edit calls) could otherwise blow up disk usage. We
# write the FIRST N events; later events are silently dropped after a
# single WARN log. Tuned generously — 500 covers every observed run.
MAX_SNAPSHOTS = 500


def replay_workspace_history(
    trajectory_path: Path,
    starter_files: dict[str, str],
    out_dir: Path,
) -> int:
    """Replay all Write/Edit events; write one snapshot dir per event.

    Args:
      trajectory_path:  Path to ``trajectory.jsonl`` produced by the
                        runtime. May not exist (returns 0).
      starter_files:    ``{absolute_path: content}`` representing the
                        initial workspace state (the challenge's
                        ``starter_code`` mapped under ``/workspace/``).
      out_dir:          Destination directory. One subdir per event is
                        written below it:
                          ``turn_001_write_foo.cc/``
                          ``turn_002_edit_foo.cc/``
                          ...

    Returns:
      Count of snapshot directories written (0 if trajectory missing).
    """
    if not trajectory_path.exists():
        log.info("workspace_history: no %s, skipping", trajectory_path)
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    current: dict[str, str] = dict(starter_files)

    snapshots = 0
    turn = 0
    schema = _detect_schema(trajectory_path)
    log.info("workspace_history: schema=%s", schema)
    for event in _iter_events(trajectory_path, schema):
        kind, path, payload = event
        if kind == "write":
            current[path] = payload if isinstance(payload, str) else ""
        elif kind == "edit":
            old_str, new_str = payload
            # Apply the first-occurrence replacement that Claude Code's
            # Edit tool semantics guarantee.
            existing = current.get(path, "")
            current[path] = existing.replace(old_str, new_str, 1) if old_str else existing
        else:
            continue

        turn += 1
        if turn > MAX_SNAPSHOTS:
            log.warning(
                "workspace_history: hit MAX_SNAPSHOTS=%d at event %d, "
                "stopping snapshot writes (later events still applied "
                "but not snapshotted)",
                MAX_SNAPSHOTS, turn,
            )
            continue

        snap_dir = out_dir / f"turn_{turn:03d}_{kind}_{Path(path).name}"
        snap_dir.mkdir(parents=True, exist_ok=True)
        for p, c in current.items():
            try:
                # Flatten to basename — keeps snapshot dirs simple and
                # the agent's deliverables are always at workspace root
                # in practice (no /workspace/sub/dir/foo writes by any
                # known runtime).
                dest = snap_dir / Path(p).name
                if c is None:
                    c = ""
                dest.write_text(c)
            except Exception as e:
                # Don't let one bad path kill the whole snapshot.
                log.warning(
                    "workspace_history: failed to write %s: %s", dest, e,
                )
        snapshots += 1

    log.info("workspace_history: wrote %d snapshots", snapshots)
    return snapshots


# ---------------------------------------------------------------------------
# trajectory iteration / event extraction
# ---------------------------------------------------------------------------


def _iter_rows(traj_path: Path) -> Iterator[dict[str, Any]]:
    """Yield each JSON object in a JSONL file, skipping unparseable lines."""
    try:
        text = traj_path.read_text()
    except Exception as e:
        log.warning("workspace_history: cannot read %s: %s", traj_path, e)
        return
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def _detect_schema(traj_path: Path) -> str:
    """Return one of {"claude_code", "mini", "generic"}.

    Mirrors ``evaluators/trajectory_audit/evaluator.py::_detect_schema``
    by row-type. Kept as a local copy (rather than importing) so this
    module doesn't take an unconditional dependency on the evaluator
    framework — sessions that disable the evaluator still produce a
    workspace_history.
    """
    for row in _iter_rows(traj_path):
        t = row.get("type")
        if t in {"system", "assistant", "user", "rate_limit_event"}:
            return "claude_code"
        if t in {
            "item.completed", "item.started",
            "turn.started", "turn.completed", "thread.started",
        }:
            return "mini"
        # First non-empty row was something unknown; keep scanning.
    return "generic"


def _iter_events(
    traj_path: Path, schema: str,
) -> Iterator[tuple[str, str, Any]]:
    """Yield ``(kind, path, payload)`` for each file-mutating event.

    ``kind`` ∈ {"write", "edit"}.
    ``payload`` is ``content`` (str) for write, ``(old_str, new_str)`` for edit.
    Non-write/edit rows are skipped silently.
    """
    if schema == "claude_code":
        yield from _iter_events_claude_code(traj_path)
    elif schema == "mini":
        yield from _iter_events_mini(traj_path)
    else:
        # Generic: try claude_code shape, then mini shape, on each row.
        yield from _iter_events_generic(traj_path)


def _iter_events_claude_code(
    traj_path: Path,
) -> Iterator[tuple[str, str, Any]]:
    for row in _iter_rows(traj_path):
        if row.get("type") != "assistant":
            continue
        msg = row.get("message", {})
        if not isinstance(msg, dict):
            continue
        for block in msg.get("content", []) or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            inp = block.get("input", {}) or {}
            if not isinstance(inp, dict):
                continue
            ev = _claude_block_to_event(name, inp)
            if ev is not None:
                yield ev


def _claude_block_to_event(
    name: str, inp: dict[str, Any],
) -> Optional[tuple[str, str, Any]]:
    """Return (kind, path, payload) for a Claude-Code tool_use, or None."""
    path = inp.get("file_path") or inp.get("path") or ""
    if not isinstance(path, str) or not path:
        return None
    if name == "Write":
        content = inp.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        return ("write", path, content)
    if name == "Edit":
        old = inp.get("old_string", "")
        new = inp.get("new_string", "")
        if not isinstance(old, str):
            old = str(old)
        if not isinstance(new, str):
            new = str(new)
        return ("edit", path, (old, new))
    return None


def _iter_events_mini(
    traj_path: Path,
) -> Iterator[tuple[str, str, Any]]:
    for row in _iter_rows(traj_path):
        if row.get("type") != "item.completed":
            continue
        item = row.get("item")
        if not isinstance(item, dict) or item.get("type") != "file_change":
            continue
        changes = item.get("changes") or []
        if not isinstance(changes, list):
            continue
        for ch in changes:
            if not isinstance(ch, dict):
                continue
            path = ch.get("path", "")
            if not isinstance(path, str) or not path:
                continue
            kind = ch.get("kind") or "write"
            if kind == "write":
                content = ch.get("content", "")
                if not isinstance(content, str):
                    content = str(content)
                yield ("write", path, content)
            elif kind == "edit":
                old = ch.get("old_string", "")
                new = ch.get("new_string", "")
                if not isinstance(old, str):
                    old = str(old)
                if not isinstance(new, str):
                    new = str(new)
                yield ("edit", path, (old, new))
            else:
                # Unknown change kind — record as an empty-content write
                # so the event still shows up in the snapshot sequence.
                yield ("write", path, "")


def _iter_events_generic(
    traj_path: Path,
) -> Iterator[tuple[str, str, Any]]:
    """Best-effort: try both shapes per row."""
    for row in _iter_rows(traj_path):
        # Try claude_code shape on assistant rows.
        if row.get("type") == "assistant":
            msg = row.get("message", {})
            if isinstance(msg, dict):
                for block in msg.get("content", []) or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        inp = block.get("input", {}) or {}
                        if isinstance(inp, dict):
                            ev = _claude_block_to_event(
                                block.get("name", ""), inp,
                            )
                            if ev is not None:
                                yield ev
        # Try mini shape on item.completed rows.
        if row.get("type") == "item.completed":
            item = row.get("item")
            if isinstance(item, dict) and item.get("type") == "file_change":
                for ch in item.get("changes") or []:
                    if not isinstance(ch, dict):
                        continue
                    path = ch.get("path", "")
                    if not path:
                        continue
                    kind = ch.get("kind") or "write"
                    if kind == "write":
                        yield ("write", path, ch.get("content", "") or "")
                    elif kind == "edit":
                        yield (
                            "edit", path,
                            (ch.get("old_string", "") or "",
                             ch.get("new_string", "") or ""),
                        )
