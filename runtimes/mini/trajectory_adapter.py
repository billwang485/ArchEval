"""mini-swe-agent → canonical trajectory adapter — the conversion layer that
lives UNDER the mini agent. Maps the native thread/turn/item event stream onto
the canonical (thinking, action, observation) steps in
``archbench.core.trajectory``, so what gets recorded is already the one schema
analysis + evaluators read. No downstream re-parsing.

Native shape (one JSON event per line):
  thread.started
  turn.started     {turn:{number, started_at}}
  item.completed   {item:{type: reasoning|mcp_tool_call|command_execution|
                          file_change|assistant_message, ...}}
  turn.completed   {turn:{usage?}}

One canonical STEP per turn: reasoning/assistant_message → thinking; the turn's
tool_call/command → action + observation; a `submit` tool call is tagged
action.kind='submit' with its observation marked sim_result (join the deep sim
metric via observation.submission_id → submit_outcomes.jsonl).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from archbench.core import trajectory as T


def to_canonical(native_trajectory_path) -> list[dict]:
    events: list[dict] = []
    for ln in Path(native_trajectory_path).read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            events.append(json.loads(ln))
        except Exception:
            continue
    return _events_to_steps(events)


def _extract_tokens(turn_completed: dict) -> Optional[dict]:
    turn = turn_completed.get("turn", turn_completed)
    usage = turn.get("usage") or turn_completed.get("usage")
    if not isinstance(usage, dict):
        return None

    def pick(*keys):
        for k in keys:
            if usage.get(k) is not None:
                return usage[k]
        return None

    return {
        "prompt": pick("prompt_tokens", "input_tokens"),
        "completion": pick("completion_tokens", "output_tokens"),
        "thinking": pick("reasoning_tokens", "thinking_tokens"),
        "total": pick("total_tokens"),
    }


def _submission_id_from(result: Any) -> Optional[str]:
    if isinstance(result, dict):
        # Direct (mini): the submit result dict carries submission_id.
        if result.get("submission_id"):
            return result["submission_id"]
        # Codex MCP-result shape: {"content":[{"type":"text","text":"<json>"}],
        # "structured_content":{"result":"<json>"}}. Dig into the text payload.
        sc = result.get("structured_content")
        if isinstance(sc, dict) and sc.get("result"):
            sid = _submission_id_from(sc["result"])
            if sid:
                return sid
        for block in (result.get("content") or []):
            if isinstance(block, dict) and block.get("text"):
                sid = _submission_id_from(block["text"])
                if sid:
                    return sid
        return None
    if isinstance(result, str):
        try:
            d = json.loads(result)
            if isinstance(d, dict):
                return d.get("submission_id")
        except Exception:
            pass
    return None


def _events_to_steps(events: list[dict]) -> list[dict]:
    steps: list[dict] = []
    cur: Optional[dict] = None

    def flush():
        nonlocal cur
        if cur is None:
            return
        if not cur["thinking"] and cur["action"] is None:
            cur = None
            return
        a = cur["action"] or {"kind": "none", "name": None, "args": None}
        o = cur["observation"] or {"kind": "none", "text": None,
                                   "submission_id": None, "status": None}
        steps.append(T.step(
            len(steps), ts=cur["ts"],
            thinking_text="\n".join(cur["thinking"]),
            tokens=cur["tokens"],
            action_kind=a["kind"], action_name=a.get("name"), action_args=a.get("args"),
            obs_kind=o["kind"], obs_text=o.get("text"),
            submission_id=o.get("submission_id"), status=o.get("status"),
        ))
        cur = None

    def ensure_cur():
        nonlocal cur
        if cur is None:
            cur = {"ts": None, "thinking": [], "action": None,
                   "observation": None, "tokens": None}

    for e in events:
        et = e.get("type")
        if et == "turn.started":
            flush()
            cur = {"ts": (e.get("turn") or {}).get("started_at"),
                   "thinking": [], "action": None, "observation": None, "tokens": None}
        elif et == "turn.completed":
            if cur is not None:
                cur["tokens"] = _extract_tokens(e) or cur["tokens"]
            flush()
        elif et == "item.completed":
            ensure_cur()
            item = e.get("item") or {}
            it = item.get("type")
            if it in ("reasoning", "assistant_message", "agent_message"):
                # mini emits `assistant_message`; codex's `--json` stream emits
                # `agent_message` for the same thing (final/inline model text).
                # Both map to canonical `thinking`.
                if item.get("text"):
                    cur["thinking"].append(item["text"])
            elif it == "mcp_tool_call":
                tool = item.get("tool")
                is_submit = tool == "submit"
                result = item.get("result")
                cur["action"] = {"kind": "submit" if is_submit else "tool_call",
                                 "name": tool, "args": item.get("arguments")}
                cur["observation"] = {
                    "kind": "sim_result" if is_submit else "tool_result",
                    "text": (result if isinstance(result, str)
                             else json.dumps(result) if result is not None else None),
                    "submission_id": _submission_id_from(result) if is_submit else None,
                    "status": "ok",
                }
            elif it == "command_execution":
                cur["action"] = {"kind": "bash", "name": item.get("command"), "args": None}
                cur["observation"] = {"kind": "tool_result",
                                      "text": item.get("aggregated_output"),
                                      "submission_id": None, "status": item.get("status")}
            elif it == "file_change":
                cur["action"] = {"kind": "tool_call", "name": "file_change", "args": None}
                cur["observation"] = {"kind": "tool_result", "text": None,
                                      "submission_id": None, "status": "ok"}
    flush()
    return steps
