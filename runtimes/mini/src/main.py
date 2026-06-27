#!/usr/bin/env python3
"""mini_runtime — in-container Gemma 4 agent (miniswe-style loop + simulator MCP).

Phase B rewrite: this entrypoint is now a proper MCP CLIENT.

  - Tools are discovered at startup via ``mcp.list_tools()`` — the
    connector at ``simulators/champsim/connector/`` is the single source of truth for
    the schema; runtimes never hardcode it. The Phase B bug fixed:
    the legacy file shipped its own TOOLS list with empty
    ``properties`` AND threw away the LLM's args on submit (replacing
    them with ``{"wait": True}`` which the new connector rejects via
    Pydantic — 165 failed submits in the last Gemma 4 run).

  - In-container local tools (read_file, write_file, list_files, bash)
    are merged in alongside the MCP-discovered ones. Names cannot
    collide: ``simulators/champsim/connector/tool_schema.py`` owns ``submit``,
    ``submit_and_wait``, ``check_submission``, ``session_end``,
    ``browse_simulator``, ``read_simulator_file``; local tools are
    file-ops + bash, none of which the MCP server advertises.

  - Args are forwarded verbatim to ``mcp.call(name, args)``. No silent
    overrides. If the LLM picks ``submit_and_wait``, it must supply
    ``implementation_paths`` per the connector's schema.

The LLM endpoint is still controlled by env vars (LLM_BASE_URL,
LLM_API_KEY, LLM_MODEL) so the launcher can swap providers without
touching this file.

Invocation (from host runtimes/mini/runner.py, via docker exec):
    python /opt/mini/main.py \
        --prompt "$USER_PROMPT" \
        --mcp-url http://localhost:$PORT/mcp \
        --max-turns 60 --max-submits 5 --timeout 7200
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_client import MCPClient


# ============================================================================
# Stream-json emitter (Codex-compatible)
# ============================================================================

def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, default=str) + "\n")
    sys.stdout.flush()


# ============================================================================
# Output truncation (M1 compression)
# ============================================================================

HEAD_BYTES = 4000
TAIL_BYTES = 2000
KEEP_THRESHOLD = HEAD_BYTES + TAIL_BYTES + 200


def truncate(output: str) -> str:
    if len(output) <= KEEP_THRESHOLD:
        return output
    head = output[:HEAD_BYTES]
    tail = output[-TAIL_BYTES:]
    omitted = len(output) - HEAD_BYTES - TAIL_BYTES
    return (
        f"{head}\n\n"
        f"... [M1: {omitted} bytes elided — {HEAD_BYTES} head + {TAIL_BYTES} tail kept] ...\n\n"
        f"{tail}"
    )


# ============================================================================
# Local tool definitions (executed inside the agent container; NOT routed
# through MCP). Their schemas are owned here because they're container-local
# and the MCP server doesn't advertise them.
# ============================================================================

LOCAL_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a file. Relative paths resolve under /workspace; absolute "
                "paths (/api/, /traces/decoded/, ...) also work."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file under /workspace (overwrites).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in a directory (default /workspace).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Execute a bash command inside the agent container "
                "(/workspace is the working directory). Use this for everything "
                "not covered by the other tools — running validate.py, "
                "grepping the API docs, diff'ing files, etc. Timeout 60s."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string",
                                "description": "Bash command to run"},
                },
                "required": ["command"],
            },
        },
    },
]

LOCAL_TOOL_NAMES = {t["function"]["name"] for t in LOCAL_TOOLS}


# ============================================================================
# Schema discovery — turn MCP's tools/list response into OpenAI-style tool defs.
# ============================================================================

def _mcp_tool_to_openai(tool: dict) -> dict:
    """Convert one MCP tool descriptor to the OpenAI Chat Completions
    function-tool shape.

    MCP shape (per FastMCP / 2025-03-26 spec):
       {"name": "...", "description": "...", "inputSchema": {"type":"object", "properties": {...}, "required":[...]}}

    OpenAI shape:
       {"type": "function", "function": {"name": "...", "description": "...", "parameters": {"type":"object", ...}}}

    We pass ``inputSchema`` through to ``parameters`` verbatim — FastMCP
    already produces a valid JSONSchema fragment whose ``type`` is
    ``object``. If the field is missing (defensive), we synthesize an
    empty object schema so the LLM sees a well-formed tool.
    """
    name = tool.get("name", "")
    description = tool.get("description", "") or ""
    parameters = tool.get("inputSchema") or tool.get("input_schema") or {
        "type": "object", "properties": {}, "required": [],
    }
    # Ensure parameters carries a "type": "object" — some endpoints validate it.
    if isinstance(parameters, dict) and "type" not in parameters:
        parameters = {"type": "object", **parameters}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def build_tools(mcp: MCPClient) -> tuple[list[dict], set[str]]:
    """Return (openai_tools, mcp_tool_names).

    ``openai_tools`` is the merged list passed to the LLM: local
    container-side tools first, then every tool the MCP server
    advertised (schema-discovered at startup, NOT hardcoded).
    ``mcp_tool_names`` is the set of names that should be dispatched
    to ``mcp.call(...)`` rather than handled locally.

    Defensive: if discovery returns zero MCP tools (server still
    coming up, or transport hiccup), we log to stderr and proceed
    with just the local set — the local agent can still read files
    and run validate.py, surfacing the breakage clearly to the
    operator instead of crashing the loop.
    """
    try:
        mcp_tools = mcp.list_tools() or []
    except Exception as e:
        print(f"[mini] WARN tools/list failed: {e}; proceeding with local tools only",
              file=sys.stderr, flush=True)
        mcp_tools = []

    mcp_openai: list[dict] = []
    mcp_names: set[str] = set()
    for t in mcp_tools:
        name = t.get("name")
        if not name:
            continue
        if name in LOCAL_TOOL_NAMES:
            # Name collision: local tool wins (we own the container-side impl).
            # This should never happen with the Phase A connector but we
            # defend anyway in case someone adds an MCP-side `bash` tool.
            print(f"[mini] WARN MCP tool {name!r} clashes with local tool; "
                  f"local wins, MCP version dropped",
                  file=sys.stderr, flush=True)
            continue
        mcp_openai.append(_mcp_tool_to_openai(t))
        mcp_names.add(name)

    all_tools = list(LOCAL_TOOLS) + mcp_openai
    print(f"[mini] discovered {len(mcp_names)} MCP tool(s): {sorted(mcp_names)}",
          file=sys.stderr, flush=True)
    return all_tools, mcp_names


# ============================================================================
# Local tools (executed inside the agent container)
# ============================================================================

def _resolve(path: str) -> Path:
    if path.startswith("/"):
        return Path(path)
    return Path("/workspace") / path


def _safe_tool(fn, *a, **k):
    """Never let a tool error kill the agent loop: return the error AS the tool
    output so the model can react. An uncaught IsADirectoryError in
    tool_write_file killed two wave-1 sessions mid-flight (lessons §26)."""
    try:
        return fn(*a, **k)
    except Exception as e:
        return f"TOOL_ERROR: {type(e).__name__}: {e}"


def tool_read_file(path: str) -> str:
    p = _resolve(path)
    try:
        return p.read_text()
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    except Exception as e:
        return f"ERROR: {e}"


def tool_write_file(path: str, content: str) -> str:
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Strip markdown fencing models sometimes wrap code in.
    content = re.sub(r"^```\w*\n(.*?)```\s*$", r"\1", content, flags=re.DOTALL)
    p.write_text(content)
    return f"wrote {len(content)} bytes to {path}"


def tool_list_files(path: str = ".") -> str:
    p = _resolve(path)
    if not p.exists():
        return f"ERROR: not found: {path}"
    if not p.is_dir():
        return f"ERROR: not a dir: {path}"
    entries = []
    for x in sorted(p.iterdir()):
        marker = "/" if x.is_dir() else ""
        entries.append(x.name + marker)
    return "\n".join(entries)


def tool_bash(command: str, timeout: int = 60) -> str:
    """Run bash command inside container, return combined stdout+stderr."""
    try:
        proc = subprocess.run(
            ["bash", "-lc", command],
            cwd="/workspace",
            capture_output=True, text=True, timeout=timeout,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return f"<rc>{proc.returncode}</rc>\n{out}"
    except subprocess.TimeoutExpired:
        return f"<rc>124</rc>\n[timeout after {timeout}s]"
    except Exception as e:
        return f"<rc>1</rc>\n[exec error: {e}]"


# ============================================================================
# LLM chat-completions call (OpenAI-compatible; supports Gemma vLLM, Gemini
# OpenAI-compat layer, and any other OpenAI-style endpoint)
# ============================================================================

def call_llm(
    messages: list[dict],
    tools: list[dict],
    temperature: float | None = None,
    max_tokens: int = 16384,
    timeout: float = 600.0,
) -> dict:
    """Make a chat-completions call to whatever endpoint LLM_BASE_URL points at.

    Endpoint / model / auth controlled entirely by env vars so the launcher
    script can swap LLM provider without touching this file:
      LLM_BASE_URL    — base URL of OpenAI-compatible endpoint
                          (Gemma vLLM:   http://<host>:8000/v1
                           Gemini OAI:   https://generativelanguage.googleapis.com/v1beta/openai)
      LLM_API_KEY     — Bearer auth token (vLLM ignores; required for Gemini)
      LLM_MODEL       — model name (google/gemma-4-31B-it | gemini-2.5-pro | ...)
      ARCHEVAL_TEMPERATURE — sampling temperature (default 0.0)
    """
    base_url = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    raw_keys = os.environ.get("LLM_API_KEY", "dummy")
    # User's Gemini setup gives a comma-separated key pool that auto-rotates
    # on 429 / quota errors. We rotate per retry attempt.
    api_key_pool = [k.strip() for k in raw_keys.split(",") if k.strip()] or ["dummy"]
    model = os.environ.get("LLM_MODEL", "google/gemma-4-31B-it")
    if not base_url:
        raise RuntimeError("LLM_BASE_URL env var not set — launcher script must export it")

    if temperature is None:
        # ARCHBENCH_ is the canonical name; ARCHEVAL_ kept as legacy fallback.
        # The 6/7 shorthand->archbench rename changed the RUNNER side but missed
        # this baked-in reader -> every mini run since was silently GREEDY
        # (temp 0.0) regardless of the requested temperature.
        temperature = float(os.environ.get("ARCHBENCH_TEMPERATURE")
                            or os.environ.get("ARCHEVAL_TEMPERATURE", "0.0"))

    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    # Gemma-only vLLM extension to surface thinking traces.
    if "gemma" in model.lower():
        payload["chat_template_kwargs"] = {"enable_thinking": True}

    last_exc: Exception | None = None
    for attempt in range(max(5, len(api_key_pool) * 2)):
        api_key = api_key_pool[attempt % len(api_key_pool)]
        headers = {"Content-Type": "application/json"}
        if api_key and api_key != "dummy":
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            resp = httpx.post(
                f"{base_url}/chat/completions",
                content=json.dumps(payload),
                headers=headers,
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except (
            httpx.ConnectError, httpx.ReadError, httpx.ReadTimeout,
            httpx.RemoteProtocolError, httpx.HTTPStatusError,
        ) as e:
            last_exc = e
            # Shorter wait on rate-limit (429) since we'll rotate key.
            status = getattr(getattr(e, "response", None), "status_code", None)
            wait = 2 if status == 429 else min(5 * (2 ** attempt), 60)
            print(f"[mini] LLM attempt {attempt+1} (key#{attempt%len(api_key_pool)}) "
                  f"failed: {e}; sleep {wait}s", file=sys.stderr, flush=True)
            time.sleep(wait)
    raise RuntimeError(f"LLM call failed after retries: {last_exc}")


# ============================================================================
# Main loop
# ============================================================================

def _is_real_submit_result(text: str) -> bool:
    """True iff a submit / submit_and_wait response indicates a completed
    simulation that produced metrics (counts against max_submits).

    Two response shapes show up:
      1. Legacy text protocol: ``SUBMIT RESULT ... ipc=... mpki=...``
      2. New connector JSON (the Phase A refactor):
         ``{"submission_id": ..., "status": "done", "outcome": "sim_ok", "metric": {"ipc": ...}}``

    Both must be recognized so we don't burn the submit budget on
    compile / validation rejections.
    """
    # JSON shape from the new connector
    try:
        body = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        body = None
    if isinstance(body, dict):
        if body.get("status") == "done" and body.get("outcome") == "sim_ok":
            metric = body.get("metric") or {}
            # Any non-empty metric block counts. Earlier this required an
            # ``ipc`` key (ChampSim-only); other sims report bandwidth_gbps,
            # overhead_pct, energy, etc. A SIM_OK with a metric is a real
            # submission regardless of which field it carries.
            if isinstance(metric, dict) and metric:
                return True
        return False
    # Legacy text shape
    has_result_header = bool(
        re.search(r"^\s*SUBMIT RESULT\b", text, re.MULTILINE)
    )
    is_failure = bool(re.search(
        r"COMPILATION FAILED|SIMULATION FAILED|VALIDATION_REJECT|STORAGE.*FAIL|"
        r"INTERNAL ERROR|TIMEOUT|LIMIT REACHED",
        text,
    ))
    has_metrics = bool(re.search(r"^\s*ipc\s*=\s*[0-9.]+", text, re.MULTILINE))
    return has_result_header and has_metrics and not is_failure


def _is_multi_sim(mcp_tool_names) -> bool:
    """True iff more than one simulator is bound this session.

    Each sim advertises exactly one bare/``_submit`` tool (``submit`` for
    single-sim; ``dramsys_submit``/``ramulator_submit`` for multi-sim). A
    single sim ALSO advertises ``submit_and_wait`` — so counting the union of
    submit+submit_and_wait would wrongly flag every single-sim run as
    multi-sim (and disable the single-sim early-stop). Count only the
    ``_submit``-suffixed names. Mirrors the server's ``len(simulators) > 1``
    (server_subprocess.py). Module-level + pure so it is unit-testable.
    """
    submit_names = {n for n in mcp_tool_names
                    if n == "submit" or n.endswith("_submit")}
    return len(submit_names) > 1


def run_loop(
    user_prompt: str,
    system_append: str,
    mcp_url: str,
    max_turns: int,
    max_submits: int,
    round_timeout: int,
    prompt_style: str = "current",
) -> None:
    # Connect + discover schema BEFORE we hand it to the LLM. The MCP
    # connector is the canonical source of truth (Phase A), and
    # tools/list returns the very schema FastMCP registered against
    # ``simulators/champsim/connector/handlers/`` -- no drift possible.
    mcp = MCPClient(mcp_url, timeout=1800.0)
    tools, mcp_tool_names = build_tools(mcp)
    # Multi-sim sessions (docs/multi_sim_design.md) prefix every tool with
    # ``<sim>_`` (dramsys_submit, ramulator_submit, ...). Detect submit-like
    # tools by SUFFIX so this loop works for both bare (single-sim) and
    # prefixed (multi-sim) names — never hardcode the prefix (CLAUDE.md §1.4).
    def _ends(suffix: str) -> set[str]:
        return {n for n in mcp_tool_names
                if n == suffix or n.endswith("_" + suffix)}

    submit_names = _ends("submit")
    submit_and_wait_names = _ends("submit_and_wait")
    check_names = _ends("check_submission")
    session_end_names = _ends("session_end")
    # Names whose SIM_OK result counts against the submit budget.
    budget_names = submit_names | submit_and_wait_names | check_names
    has_submit_and_wait = bool(submit_and_wait_names)
    # Tool the system prompt steers the agent toward. In multi-sim there are
    # several; list them all so the agent submits to each simulator.
    _pref_set = submit_and_wait_names or submit_names
    preferred_submit = (
        ", ".join(sorted(_pref_set)) if _pref_set else "submit"
    )
    # Multi-sim iff >1 sim bound; see _is_multi_sim (do NOT union
    # submit_and_wait — single sim has both). Drives the single-sim early-stop.
    is_multi_sim = _is_multi_sim(mcp_tool_names)

    if prompt_style == "old":
        # FROZEN legacy prompt (pre-2026-06-21 rewrite). Served by the
        # `mini_old_prompt` runtime so old-vs-new is a controlled A/B that
        # differs ONLY in this string (same image, same loop). Do NOT "improve"
        # it — it is a fixed baseline.
        base_system = (
            "You are an in-container agent for an ARCHEVAL challenge, working in "
            "/workspace. You have a set of local tools (read_file, write_file, "
            "list_files, bash) plus simulator tools advertised by the MCP server "
            f"({', '.join(sorted(mcp_tool_names)) or '(none)'}).\n\n"
            "Files in /workspace/ are yours to edit (candidate.h, "
            "candidate.cc, etc). Starter code is at /workspace/starter/. "
            "Trace samples are in /traces/decoded/ (decoded CSV). API docs in "
            "/api/. Simulator source is accessible via browse_simulator + "
            "read_simulator_file. There is a validate.py at /workspace/ to "
            "validate your design fits the metadata budget.\n\n"
            f"To submit your implementation: call `{preferred_submit}` with "
            "`implementation_paths` set to the absolute /workspace/ paths of "
            "the files that make up your submission. For short-eval challenges "
            f"(~5 min sim) `{preferred_submit}` is synchronous — one call returns "
            "the final outcome, no polling needed. Storage-check / compile "
            "failures do NOT consume your submission budget.\n\n"
            f"Maximum {max_submits} successful submissions per round. "
            "Use `bash` for anything not covered by the other tools — running "
            "validate.py, grep'ing docs, computing offsets, etc.\n\n"
            "IMPORTANT: keep reasoning tight (<= 800 words per turn). Do NOT "
            "enumerate alternatives repeatedly or restate the same option multiple "
            "times. Pick ONE concrete next step and call the corresponding tool. "
            "Every turn MUST end with a tool call."
        )
    else:
        # Current prompt: role + verifier-submit protocol only. Task specifics
        # (paths, budget, surrogate workflow) live in the per-challenge prompt;
        # reasoning length is a model-layer concern.
        base_system = (
            "You are a computer-architecture engineer. You design hardware in "
            "/workspace, using local tools (read_file, write_file, list_files, "
            "bash) plus the tools advertised by the MCP server "
            f"({', '.join(sorted(mcp_tool_names)) or '(none)'}).\n\n"
            "When your design is ready, submit it for verification with "
            f"`{preferred_submit}`: set `implementation_paths` to the absolute "
            "/workspace/ paths of your design files. It verifies your design and "
            "returns the result; submissions that fail to build are free.\n\n"
            "Use `bash` for anything the other tools don't cover."
        )
    if system_append:
        base_system = base_system + "\n\n" + system_append

    messages: list[dict] = [
        {"role": "system", "content": base_system},
        {"role": "user", "content": user_prompt},
    ]

    thread_id = str(uuid.uuid4())
    emit({
        "type": "thread.started",
        "thread": {"id": thread_id, "created_at": datetime.now(timezone.utc).isoformat()},
    })

    submits_done = 0
    text_only_nudges = 0
    session_ended = False
    # Multi-sim: track which sims' session_end the agent has called. The loop
    # only exits once every namespace's session_end fired (single-sim: the one).
    _ended_sessions: set[str] = set()
    deadline = time.time() + round_timeout

    turn_num = 0
    for iteration in range(1, max_turns + 1):
        if time.time() > deadline:
            print(f"[mini] round timeout ({round_timeout}s) hit", file=sys.stderr, flush=True)
            break
        if session_ended:
            print("[mini] session_end requested by agent; stopping cleanly",
                  file=sys.stderr, flush=True)
            break

        turn_num += 1
        emit({
            "type": "turn.started",
            "turn": {"number": turn_num, "started_at": datetime.now(timezone.utc).isoformat()},
        })

        resp = call_llm(messages, tools)
        _u = resp.get("usage") or {}
        usage = {
            "input_tokens": _u.get("prompt_tokens", 0) or 0,
            "output_tokens": _u.get("completion_tokens", 0) or 0,
            "cached_input_tokens": 0,
            "total_tokens": _u.get("total_tokens", 0) or 0,
        }
        choices = resp.get("choices") or []
        if not choices:
            emit({"type": "turn.completed", "usage": usage})
            break

        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or []
        reasoning_text = msg.get("reasoning") or msg.get("reasoning_content") or ""

        if content.startswith("thought\n"):
            inline = content[len("thought\n"):]
            if not reasoning_text and "\n\n" in inline:
                reasoning_text, _, inline = inline.partition("\n\n")
            content = inline

        if reasoning_text.strip():
            emit({"type": "item.completed",
                  "item": {"type": "reasoning", "text": reasoning_text}})

        if content.strip() or not tool_calls:
            emit({"type": "item.completed",
                  "item": {"type": "assistant_message", "text": content}})

        # No tool calls: nudge up to 5 times. We KEEP nudging even when the
        # model produced only reasoning (empty content) — the prior break-on-
        # empty path lets Gemma silently kill the round if it hits max_tokens
        # mid-thinking. Better to push it toward a concrete action.
        if not tool_calls:
            text_only_nudges += 1
            messages.append({"role": "assistant", "content": content or "(no content)"})
            if text_only_nudges >= 5:
                emit({"type": "turn.completed", "usage": usage})
                print(f"[mini] {text_only_nudges} text-only turns in a row — stopping", file=sys.stderr, flush=True)
                break
            nudge = (
                "You produced no tool call. Take a concrete action NOW:\n"
                "  1. read_file('/workspace/starter/candidate.h') to see the API\n"
                "  2. write_file('/workspace/candidate.h', ...) and ...cc to draft your policy\n"
                "  3. bash('python3 validate.py /workspace --budget 256') to verify storage\n"
                f"  4. {preferred_submit}(implementation_paths=['/workspace/candidate.h', "
                "'/workspace/candidate.cc']) to compile + run\n"
                "Stop deliberating — write code and submit."
            )
            messages.append({"role": "user", "content": nudge})
            emit({"type": "turn.completed", "usage": usage})
            continue
        text_only_nudges = 0

        # Append assistant turn to history.
        messages.append({
            "role": "assistant",
            "content": content or None,
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": (
                            tc["function"]["arguments"]
                            if isinstance(tc["function"]["arguments"], str)
                            else json.dumps(tc["function"]["arguments"])
                        ),
                    },
                }
                for tc in tool_calls
            ],
        })

        for tc in tool_calls:
            name = tc["function"]["name"]
            raw_args = tc["function"]["arguments"]
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}

            tool_id = tc["id"]
            output = ""
            is_submit_success = False

            # ---- LOCAL container-side tools ----
            if name == "read_file":
                output = _safe_tool(tool_read_file, args.get("path", ""))
                emit({"type": "item.completed",
                      "item": {"type": "mcp_tool_call", "id": tool_id,
                               "tool": "read_file", "arguments": args,
                               "result": {"content": [{"type": "text", "text": output}]}}})

            elif name == "write_file":
                output = _safe_tool(tool_write_file, args.get("path", ""), args.get("content", ""))
                # Phase E: preserve full content per change so the host-side
                # ``archbench.core.workspace_history.replay_workspace_history`` can
                # rebuild per-turn workspace snapshots. The trajectory_audit
                # evaluator only reads ``path`` so this is additive.
                emit({"type": "item.completed",
                      "item": {"type": "file_change", "id": tool_id,
                               "changes": [{
                                   "path": args.get("path", ""),
                                   "kind": "write",
                                   "content": args.get("content", ""),
                               }]}})

            elif name == "list_files":
                output = _safe_tool(tool_list_files, args.get("path", "."))
                emit({"type": "item.completed",
                      "item": {"type": "mcp_tool_call", "id": tool_id,
                               "tool": "list_files", "arguments": args,
                               "result": {"content": [{"type": "text", "text": output}]}}})

            elif name == "bash":
                output = _safe_tool(tool_bash, args.get("command", ""))
                emit({"type": "item.completed",
                      "item": {"type": "command_execution", "id": tool_id,
                               "command": args.get("command", ""),
                               "aggregated_output": output,
                               "status": "completed"}})

            # ---- MCP-routed tools (dispatched by name; args forwarded verbatim) ----
            elif name in mcp_tool_names:
                try:
                    # Phase B fix: forward LLM's args UNMODIFIED. The legacy
                    # code at this line was `mcp.call("submit", {"wait": True})`
                    # which (a) discarded `implementation_paths` and (b)
                    # sent a kw the new Pydantic-validated connector
                    # rejects. Both bugs killed the last Gemma 4 run.
                    mcp_result = mcp.call(name, args)
                except Exception as e:
                    mcp_result = {"text": f"MCP ERROR: {e}", "is_error": True}
                output = mcp_result.get("text", "") or ""

                # Submit budget bookkeeping — applies to submit /
                # submit_and_wait (and check_submission, which can surface a
                # final result via poll), under bare OR <sim>_-prefixed names.
                # This is advisory: the connector enforces the real per-sim
                # budget server-side. In multi-sim the budget is PER sim, so we
                # don't stop the whole loop on it (see the break below).
                if name in budget_names:
                    if _is_real_submit_result(output):
                        submits_done += 1
                        is_submit_success = True
                if name in session_end_names:
                    # A single sim's session_end ends the WHOLE mini loop only
                    # in single-sim mode. In multi-sim the agent must end every
                    # namespace; require all of them before stopping.
                    _ended_sessions.add(name)
                    if not is_multi_sim or _ended_sessions >= session_end_names:
                        session_ended = True

                # File-change emission isn't applicable here (workspace
                # was edited locally earlier). Emit mcp_tool_call event.
                emit({"type": "item.completed",
                      "item": {"type": "mcp_tool_call", "id": tool_id,
                               "tool": name, "arguments": args,
                               "result": {
                                   "content": [{"type": "text", "text": output}],
                                   "structured_content": {"result": output},
                               }}})

            # ---- Unknown ----
            else:
                output = (
                    f"Unknown tool: {name}. Available: "
                    f"{sorted(LOCAL_TOOL_NAMES | mcp_tool_names)}"
                )
                emit({"type": "item.completed",
                      "item": {"type": "mcp_tool_call", "id": tool_id,
                               "tool": name, "arguments": args,
                               "result": {"content": [{"type": "text", "text": output}],
                                          "isError": True}}})

            messages.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "content": truncate(output),
            })

            # Single-sim: stop the loop once the per-round budget is spent.
            # Multi-sim: ``max_submits`` is PER sim but ``submits_done`` counts
            # across sims, so this global early-stop would cut the agent off
            # after max_submits TOTAL successes (e.g. 4 instead of 4-per-sim).
            # Let the connector enforce each sim's budget server-side and let
            # the agent finish via session_end / round_timeout instead.
            if (not is_multi_sim
                    and is_submit_success and submits_done >= max_submits):
                emit({"type": "turn.completed", "usage": usage})
                print(f"[mini] max_submits={max_submits} reached", file=sys.stderr, flush=True)
                mcp.close()
                return

        emit({"type": "turn.completed", "usage": usage})

    mcp.close()


def noop_loop(mcp_url: str, paths: list[str]) -> None:
    """[smoke harness] Deterministic NoopAgent: no LLM. Submit the staged
    starter artifacts once via MCP, report the outcome, end the session.
    Exercises the ENTIRE loop an agent would (MCP discovery -> submit ->
    sim -> typed outcome -> session_end) so `archbench smoke` can verify a
    challenge end-to-end and check baseline parity — the runnability gate
    static checks can't provide (lessons §26: dram_pride/.todo, libclang,
    sourceless gem5 were all invisible until something actually ran)."""
    mcp = MCPClient(mcp_url, timeout=1800.0)
    tools = [t.get("name") for t in (mcp.list_tools() or [])]
    emit({"type": "item.completed",
          "item": {"type": "reasoning", "id": "noop_0",
                   "text": f"NoopAgent: tools={tools}; submitting {paths}"}})
    name = "submit_and_wait" if "submit_and_wait" in tools else "submit"
    result = mcp.call(name, {"implementation_paths": paths})
    emit({"type": "item.completed",
          "item": {"type": "mcp_tool_call", "id": "noop_1", "tool": name,
                   "arguments": {"implementation_paths": paths},
                   "result": {"content": [{"type": "text", "text": str(result)[:4000]}]}}})
    print(f"[noop] {name} -> {str(result)[:500]}", file=sys.stderr, flush=True)
    if "session_end" in tools:
        mcp.call("session_end", {"reason": "noop smoke complete"})
    mcp.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--prompt", required=True)
    p.add_argument("--system-append", default="")
    p.add_argument("--mcp-url", required=True)
    # LLM endpoint/model/auth come from env vars (LLM_BASE_URL, LLM_MODEL,
    # LLM_API_KEY) set by the launcher script — no CLI args needed.
    p.add_argument("--max-turns", type=int, default=60)
    p.add_argument("--max-submits", type=int, default=5)
    p.add_argument("--timeout", type=int, default=7200)
    p.add_argument("--prompt-style", choices=["current", "old"], default="current",
                   help="Which baked system prompt to use; 'old' = frozen "
                        "pre-rewrite prompt (the mini_old_prompt runtime).")
    args = p.parse_args()

    noop = os.environ.get("ARCHBENCH_NOOP_SUBMIT", "").strip()
    if noop:
        noop_loop(args.mcp_url, [s for s in noop.split(",") if s.strip()])
        return

    run_loop(
        user_prompt=args.prompt,
        system_append=args.system_append,
        mcp_url=args.mcp_url,
        max_turns=args.max_turns,
        max_submits=args.max_submits,
        round_timeout=args.timeout,
        prompt_style=args.prompt_style,
    )


if __name__ == "__main__":
    main()
