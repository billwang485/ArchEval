#!/usr/bin/env python3
"""ArchHarness in-container entrypoint.

Phase B rewrite: this entrypoint is now a proper MCP CLIENT.
Mirrors the pattern in ``runtimes/mini/src/main.py`` — the connector at
``simulators/champsim/connector/`` is the single source of truth for the simulator-side
schema; runtimes discover it via ``tools/list`` and forward args
verbatim. The Phase B bug fixed: the legacy file shipped its own TOOLS
list with empty ``properties`` AND threw away the LLM's args on submit
(replacing them with ``{"wait": True}``, which the new connector rejects
via Pydantic).

What's unique to archharness vs. mini:

  - Plan-Model-Act-Observe loop with a ``state_card`` persistent-memory
    file (notes are local, file-backed, NOT routed through MCP).
  - A ``predict()`` step that must precede each first submit of a fresh
    simulation. Polling calls don't require a fresh prediction.
  - ``execute_command`` (not ``bash``) — for parity with the legacy
    archharness prompt; same semantics.

The LLM endpoint is still controlled by env vars (LLM_BASE_URL,
LLM_API_KEY, LLM_MODEL) so the launcher can swap providers without
touching this file.

Invocation (from host runtimes/archharness/runner.py, via docker exec):

    python /opt/archharness/main.py \\
        --prompt "$USER_PROMPT" \\
        --mcp-url http://localhost:$PORT/mcp \\
        --max-turns 60 --max-submits 5 --timeout 14400

Emitted events (JSON, one per line on stdout):
  {"type":"thread.started","thread":{...}}
  {"type":"turn.started","turn":{...}}
  {"type":"item.completed","item":{"type":"reasoning","text":"..."}}
  {"type":"item.completed","item":{"type":"assistant_message","text":"..."}}
  {"type":"item.completed","item":{"type":"mcp_tool_call","tool":"submit",...,"result":{...}}}
  {"type":"item.completed","item":{"type":"command_execution","command":"...","aggregated_output":"...","exit_code":0,"status":"completed"}}
  {"type":"item.completed","item":{"type":"file_change","changes":[{"path":"..."}]}}
  {"type":"turn.completed","usage":{"input_tokens":N,"cached_input_tokens":M,"output_tokens":K}}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_client import MCPClient


# ---------- stream-json emitter (Codex-compatible) ----------

def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, default=str) + "\n")
    sys.stdout.flush()


# ---------- M1 compression ----------

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


# ---------- workflow preamble (Plan-Model-Act-Observe) ----------

ARCH_WORKFLOW_PREAMBLE = """
<archharness_workflow>
You are a hardware-architecture research agent. Your workflow is
Plan-Model-Act-Observe. Every cycle:

  1. PLAN — state your next move in 1-2 bullets.
  2. MODEL — write an analytical prediction in state_card: "given X, I expect
     metric ~Y because Z". Use back-of-envelope reasoning (working-set size,
     baseline numbers, sensitivity to design choices).
  3. ACT — read, write, audit. Prefer small, testable changes.
  4. PREDICT — call predict(metric, value, reasoning) before each NEW
     submit() of a changed implementation.
  5. OBSERVE — call submit() / submit_and_wait(). For cache_replacement_fast,
     submit_and_wait() blocks synchronously for ~3-6 min and returns the
     final outcome in one call. Update state_card with predicted-vs-actual
     and learnings, then start a new cycle with a new predict().

Rules:
  - Treat state_card as your persistent memory across turns. Write short
    bullets there for every fact you want to keep (trace stats, working-set
    size, baseline numbers, hypotheses tested, what failed).
  - Prefer reasoning from measurements + calibrated analytical models over
    guessing. Each new submission should be a validated hypothesis.
  - Before each submit, run `python3 validate.py .` via
    execute_command to verify your design fits the metadata budget.
    The submit oracle reports only VALIDATION_REJECT on validation failure;
    it does not return the checker's numeric diagnostics.
</archharness_workflow>
""".strip()


# ============================================================================
# Local tool definitions (executed inside the agent container; NOT routed
# through MCP). Their schemas are owned here.
# ============================================================================

LOCAL_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": "Execute a bash command in /workspace (container-local).",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a file. Relative paths resolve under /workspace; "
                "absolute paths (/api/, /traces/decoded/, ...) also work."
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
            "description": "Write content to a file (overwrites).",
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
            "description": "List files in a directory.",
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
            "name": "state_card_write",
            "description": (
                "Append a note to /workspace/.state_card.md, shown to you "
                "every turn. Keep notes SHORT (one-bullet facts)."
            ),
            "parameters": {
                "type": "object",
                "properties": {"note": {"type": "string"}},
                "required": ["note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "predict",
            "description": (
                "Record your pre-submit prediction for the primary metric. "
                "MANDATORY before every submit(). Give metric name, numeric "
                "prediction, and short analytical reasoning."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string"},
                    "value": {"type": "number"},
                    "reasoning": {"type": "string"},
                },
                "required": ["metric", "value", "reasoning"],
            },
        },
    },
]

LOCAL_TOOL_NAMES = {t["function"]["name"] for t in LOCAL_TOOLS}


# ---------- MCP schema discovery ----------

def _mcp_tool_to_openai(tool: dict) -> dict:
    """Convert MCP tool descriptor to OpenAI function-tool shape.

    See ``runtimes/mini/src/main.py`` for the full justification; this
    helper is duplicated here rather than pulled into a shared module
    so each in-house runtime stays a single self-contained file
    (drop-in replaceable, easy to vendor into a new image).
    """
    name = tool.get("name", "")
    description = tool.get("description", "") or ""
    parameters = tool.get("inputSchema") or tool.get("input_schema") or {
        "type": "object", "properties": {}, "required": [],
    }
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
    """Discover MCP-side tools via tools/list and merge with local tools.

    Returns (openai_tools_list_for_llm, mcp_tool_name_set).
    """
    try:
        mcp_tools = mcp.list_tools() or []
    except Exception as e:
        print(f"[archharness] WARN tools/list failed: {e}; "
              f"proceeding with local tools only",
              file=sys.stderr, flush=True)
        mcp_tools = []

    mcp_openai: list[dict] = []
    mcp_names: set[str] = set()
    for t in mcp_tools:
        name = t.get("name")
        if not name:
            continue
        if name in LOCAL_TOOL_NAMES:
            print(f"[archharness] WARN MCP tool {name!r} clashes with local; "
                  f"local wins, MCP version dropped",
                  file=sys.stderr, flush=True)
            continue
        mcp_openai.append(_mcp_tool_to_openai(t))
        mcp_names.add(name)
    all_tools = list(LOCAL_TOOLS) + mcp_openai
    print(f"[archharness] discovered {len(mcp_names)} MCP tool(s): "
          f"{sorted(mcp_names)}",
          file=sys.stderr, flush=True)
    return all_tools, mcp_names


# ---------- local filesystem tool impls (container-local) ----------

def _resolve(path: str) -> Path:
    if path.startswith("/"):
        return Path(path)
    return Path("/workspace") / path


def tool_execute_command(command: str, timeout: int = 300) -> tuple[str, int]:
    try:
        result = subprocess.run(
            ["bash", "-lc", command],
            cwd="/workspace",
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (result.stdout or "") + (result.stderr or "")
        return out, result.returncode
    except subprocess.TimeoutExpired as e:
        return f"[timeout after {timeout}s]\n{e.stdout or ''}\n{e.stderr or ''}", 124
    except Exception as e:
        return f"[exec error: {e}]", 1


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
    # Strip markdown fencing that models sometimes wrap code in.
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


def tool_state_card_append(note: str) -> str:
    p = _resolve(".state_card.md")
    if p.exists():
        existing = p.read_text()
    else:
        existing = "# State Card\n\n"
    ts = datetime.now().strftime("%H:%M:%S")
    new = existing.rstrip() + f"\n- [{ts}] {note.strip()}\n"
    p.write_text(new)
    return f"state_card appended ({len(new)} bytes total)"


def state_card_read() -> str:
    p = _resolve(".state_card.md")
    if not p.exists():
        return ""
    try:
        return p.read_text()
    except Exception:
        return ""


# ---------- LLM call (OpenAI-compatible; Gemma vLLM, Gemini OAI, etc.) ----------

def call_llm(
    messages: list[dict],
    tools: list[dict],
    temperature: float | None = None,
    max_tokens: int = 16384,
    timeout: float = 600.0,
) -> dict:
    """Endpoint / model / auth from env vars:
      LLM_BASE_URL  — OpenAI-compatible endpoint base URL
      LLM_API_KEY   — Bearer auth token (dummy for vLLM, required for Gemini)
      LLM_MODEL     — model name
      ARCHEVAL_TEMPERATURE — sampling temperature
    """
    base_url = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    raw_keys = os.environ.get("LLM_API_KEY", "dummy")
    api_key_pool = [k.strip() for k in raw_keys.split(",") if k.strip()] or ["dummy"]
    model = os.environ.get("LLM_MODEL", "google/gemma-4-31B-it")
    if not base_url:
        raise RuntimeError("LLM_BASE_URL env var not set")
    if temperature is None:
        temperature = float(os.environ.get("ARCHEVAL_TEMPERATURE", "0.0"))
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
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
            status = getattr(getattr(e, "response", None), "status_code", None)
            wait = 2 if status == 429 else min(5 * (2 ** attempt), 60)
            print(
                f"[archharness] LLM call attempt {attempt+1} (key#{attempt%len(api_key_pool)}) "
                f"failed: {e}; sleeping {wait}s",
                file=sys.stderr, flush=True,
            )
            time.sleep(wait)
    raise RuntimeError(f"Gemma call failed after 5 attempts: {last_exc}")


# ---------- prediction gate ----------

def compare_prediction(pred: dict, metrics: dict, primary_metric: str) -> str:
    if not pred:
        return ""
    m = (pred.get("metric") or "").strip().lower()
    want = (primary_metric or "").strip().lower()
    pred_val = pred.get("value")
    actual = None
    for k in (m, want, "ipc", "mpki"):
        if k and metrics and k in metrics and metrics[k] is not None:
            actual = metrics[k]
            break
    if actual is None or pred_val is None:
        return f"[reflection: prediction recorded but actual {want!r} missing]"
    try:
        err = float(actual) - float(pred_val)
        rel = abs(err) / max(abs(float(pred_val)), 1e-9) * 100
        return (
            f"[reflection] you predicted {m}={pred_val}, actual={actual} "
            f"(delta={err:+.4f}, {rel:.1f}% error). "
            f"If |error|>20%, update state_card with why your model was wrong."
        )
    except Exception:
        return f"[reflection: could not compare {pred_val!r} vs {actual!r}]"


def _parse_metrics_from_response(text: str) -> dict[str, float]:
    """Extract numeric metrics from a submit / submit_and_wait response.

    Handles both the new connector JSON shape
    (``{"metric": {"ipc": ..., "mpki": ...}, ...}``) and the legacy
    ``ipc=... mpki=...`` text. Returns an empty dict if nothing parses.
    """
    out: dict[str, float] = {}
    try:
        body = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        body = None
    if isinstance(body, dict):
        metric = body.get("metric") or {}
        if isinstance(metric, dict):
            for k in ("ipc", "mpki"):
                v = metric.get(k)
                if isinstance(v, (int, float)):
                    out[k] = float(v)
        return out
    # Legacy text shape
    for k in ("ipc", "mpki"):
        m = re.search(rf"\b{k}\s*=\s*([0-9.]+)", text)
        if m:
            try:
                out[k] = float(m.group(1))
            except ValueError:
                pass
    return out


def _classify_submit_response(text: str) -> dict[str, bool]:
    """Categorize a submit / submit_and_wait response.

    Returns a dict with boolean flags:
      is_started:  the async-submit handle came back (queued / running)
      is_result:   real completion with metrics
      is_rejected: schema reject / submit was rejected outright
      is_failure:  build/validation/sim/timeout failure (doesn't count)
    """
    # JSON shape from new connector
    try:
        body = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        body = None
    if isinstance(body, dict):
        status = body.get("status")
        outcome = body.get("outcome")
        metric = body.get("metric") or {}
        has_metrics = isinstance(metric, dict) and "ipc" in metric
        is_started = status in ("queued", "running")
        is_result = status == "done" and outcome == "sim_ok" and has_metrics
        is_failure = status == "done" and outcome in (
            "build_fail", "validation_reject", "sim_timeout", "sim_fail"
        )
        is_rejected = bool(body.get("rejected")) or "error" in body
        return {
            "is_started": is_started,
            "is_result": is_result,
            "is_rejected": is_rejected,
            "is_failure": is_failure,
        }
    # Legacy text shape
    is_started = bool(re.search(r"^\s*SUBMIT STARTED\b", text, re.MULTILINE))
    has_result_header = bool(
        re.search(r"^\s*SUBMIT RESULT\b", text, re.MULTILINE)
    ) or bool(
        re.search(r"^\s*SIMULATION_OK\b", text, re.MULTILINE)
    )
    is_failure = bool(re.search(
        r"COMPILATION FAILED|SIMULATION FAILED|VALIDATION_REJECT|STORAGE.*FAIL|"
        r"INTERNAL ERROR|TIMEOUT|LIMIT REACHED",
        text,
    ))
    has_metrics = bool(re.search(r"\bipc\s*=\s*[0-9.]+", text))
    is_result = has_result_header and has_metrics and not is_failure
    is_rejected = bool(
        re.search(r"^\s*SUBMIT REJECTED\b", text, re.MULTILINE)
    ) or (has_result_header and is_failure)
    return {
        "is_started": is_started,
        "is_result": is_result,
        "is_rejected": is_rejected,
        "is_failure": is_failure,
    }


# ---------- main loop ----------

def build_system_prompt(base_system: str) -> str:
    state_card = state_card_read()
    if state_card.strip():
        block = (
            "\n\n<state_card>\n"
            "(Your persistent notes. Update via state_card_write. "
            "Regenerated from /workspace/.state_card.md each turn.)\n"
            f"{state_card.strip()}\n"
            "</state_card>"
        )
    else:
        block = (
            "\n\n<state_card>\n"
            "(empty — use state_card_write to record facts across turns)\n"
            "</state_card>"
        )
    return f"{base_system}\n\n{ARCH_WORKFLOW_PREAMBLE}{block}"


def run_loop(
    user_prompt: str,
    system_append: str,
    mcp_url: str,
    max_turns: int,
    max_submits: int,
    round_timeout: int,
    primary_metric: str = "ipc",
) -> None:
    # Seed state card so first turn sees the block.
    sc = _resolve(".state_card.md")
    if not sc.exists():
        sc.write_text(
            "# State Card\n\n"
            "_(Initially empty — record system facts: trace name+size, "
            "baseline metric, storage budget, hypotheses tried, failures.)_\n"
        )

    # Connect + discover schema BEFORE we hand it to the LLM. The MCP
    # connector is the canonical source of truth (Phase A).
    mcp = MCPClient(mcp_url, timeout=1800.0)
    tools, mcp_tool_names = build_tools(mcp)
    has_submit_and_wait = "submit_and_wait" in mcp_tool_names
    preferred_submit = "submit_and_wait" if has_submit_and_wait else "submit"
    submit_tool_names = {"submit", "submit_and_wait"} & mcp_tool_names
    if not submit_tool_names:
        print("[archharness] WARN MCP server advertises no submit/submit_and_wait — "
              "agent will have nothing to evaluate", file=sys.stderr, flush=True)

    base_system = (
        "You are ArchHarness, running in /workspace. You have local tools "
        "(execute_command, read_file, write_file, list_files, "
        "state_card_write, predict) plus simulator tools advertised by the "
        f"MCP server ({', '.join(sorted(mcp_tool_names)) or '(none)'}).\n\n"
        "Files in /workspace/ are yours to edit. Starter code is there. "
        "Trace samples are in /traces/decoded/ (decoded CSV). API docs in /api/. "
        "Simulator source is accessible via browse_simulator + read_simulator_file.\n\n"
        f"To evaluate your implementation: call `{preferred_submit}` with "
        "`implementation_paths` set to the absolute /workspace/ paths of the "
        "files that make up your submission. For cache_replacement_fast "
        f"(short-eval), `{preferred_submit}` runs the 6 workloads in parallel "
        "(~3-6 min wall-clock) and returns synchronously. "
        f"Max successful submissions: {max_submits}. Compile / validation "
        "failures do NOT count. Calibrate with analytical models first."
        f"{(chr(10) + chr(10) + system_append) if system_append else ''}"
    )

    messages: list[dict] = [
        {"role": "system", "content": build_system_prompt(base_system)},
        {"role": "user", "content": user_prompt},
    ]

    thread_id = str(uuid.uuid4())
    emit({
        "type": "thread.started",
        "thread": {"id": thread_id, "created_at": datetime.now(timezone.utc).isoformat()},
    })

    pending_prediction: dict = {}
    # Prediction is "used" once the first submit starts a job (SUBMIT STARTED);
    # from then on, subsequent submit calls are POLLS and shouldn't require a
    # fresh prediction. We only require a NEW prediction after a real RESULT
    # has come back (which carries metrics).
    submit_in_flight = False   # True between SUBMIT STARTED and SUBMIT RESULT
    submits_done = 0
    blocked_submits_in_row = 0
    text_only_nudges = 0
    session_ended = False
    deadline = time.time() + round_timeout

    turn_num = 0
    for iteration in range(1, max_turns + 1):
        if time.time() > deadline:
            print(
                f"[archharness] round timeout ({round_timeout}s) hit, stopping",
                file=sys.stderr, flush=True,
            )
            break
        if session_ended:
            print("[archharness] session_end requested by agent; stopping cleanly",
                  file=sys.stderr, flush=True)
            break

        # Refresh state_card in system prompt each turn.
        messages[0]["content"] = build_system_prompt(base_system)

        turn_num += 1
        emit({
            "type": "turn.started",
            "turn": {"number": turn_num, "started_at": datetime.now(timezone.utc).isoformat()},
        })

        resp = call_llm(messages, tools)
        # vLLM/OpenAI returns prompt_tokens/completion_tokens/total_tokens.
        # Re-key to Codex-stream schema (input_tokens/output_tokens/
        # cached_input_tokens) so the host-side runner's trajectory parser
        # accumulates them correctly. Total is left as a hint.
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
            emit({
                "type": "item.completed",
                "item": {"type": "reasoning", "text": reasoning_text},
            })

        # Assistant message (text or empty-but-content-header)
        if content.strip() or not tool_calls:
            emit({
                "type": "item.completed",
                "item": {"type": "assistant_message", "text": content},
            })

        # No tool calls: nudge up to 5 times. Don't break on empty content
        # (Gemma sometimes returns empty content mid-thinking when it hits
        # max_tokens — we should give it more chances rather than terminate).
        if not tool_calls:
            text_only_nudges += 1
            messages.append({"role": "assistant", "content": content or "(no content)"})
            if text_only_nudges >= 5:
                emit({"type": "turn.completed", "usage": usage})
                print(f"[archharness] {text_only_nudges} text-only turns in a row — stopping", file=sys.stderr, flush=True)
                break
            messages.append({
                "role": "user",
                "content": (
                    "Thanks for the plan. Now execute a tool — read_file, "
                    "write_file, state_card_write, or predict+submit when "
                    "ready to validate."
                ),
            })
            emit({"type": "turn.completed", "usage": usage})
            continue
        text_only_nudges = 0

        # Order tool calls: predict before submit in same turn.
        def _prio(tc):
            name = tc["function"]["name"]
            if name == "predict":
                return 0
            if name in submit_tool_names:
                return 2
            return 1
        tool_calls = sorted(tool_calls, key=_prio)

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

            is_submit = False
            output = ""
            tool_id = tc["id"]

            # ---- LOCAL container-side tools ----
            if name == "execute_command":
                out, rc = tool_execute_command(args.get("command", ""))
                output = out
                emit({
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "id": tool_id,
                        "command": args.get("command", ""),
                        "aggregated_output": out,
                        "exit_code": rc,
                        "status": "completed",
                    },
                })

            elif name == "read_file":
                output = tool_read_file(args.get("path", ""))
                emit({
                    "type": "item.completed",
                    "item": {
                        "type": "mcp_tool_call", "id": tool_id,
                        "tool": "read_file", "arguments": args,
                        "result": {"content": [{"type": "text", "text": output}]},
                    },
                })

            elif name == "write_file":
                output = tool_write_file(args.get("path", ""), args.get("content", ""))
                # Phase E: preserve full content per change so the host-side
                # ``archbench.core.workspace_history.replay_workspace_history`` can
                # rebuild per-turn workspace snapshots. The trajectory_audit
                # evaluator only reads ``path`` so this is additive.
                emit({
                    "type": "item.completed",
                    "item": {
                        "type": "file_change", "id": tool_id,
                        "changes": [{
                            "path": args.get("path", ""),
                            "kind": "write",
                            "content": args.get("content", ""),
                        }],
                    },
                })

            elif name == "list_files":
                output = tool_list_files(args.get("path", "."))
                emit({
                    "type": "item.completed",
                    "item": {
                        "type": "mcp_tool_call", "id": tool_id,
                        "tool": "list_files", "arguments": args,
                        "result": {"content": [{"type": "text", "text": output}]},
                    },
                })

            elif name == "state_card_write":
                output = tool_state_card_append(args.get("note", ""))
                emit({
                    "type": "item.completed",
                    "item": {
                        "type": "mcp_tool_call", "id": tool_id,
                        "tool": "state_card_write", "arguments": args,
                        "result": {"content": [{"type": "text", "text": output}]},
                    },
                })

            elif name == "predict":
                pending_prediction = {
                    "metric": args.get("metric", ""),
                    "value": args.get("value"),
                    "reasoning": args.get("reasoning", ""),
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                }
                output = (
                    f"prediction recorded: {pending_prediction['metric']}="
                    f"{pending_prediction['value']}. reasoning: "
                    f"{(pending_prediction['reasoning'] or '')[:200]}. "
                    f"Call {preferred_submit}() next."
                )
                emit({
                    "type": "item.completed",
                    "item": {
                        "type": "mcp_tool_call", "id": tool_id,
                        "tool": "predict", "arguments": args,
                        "result": {"content": [{"type": "text", "text": output}]},
                    },
                })

            # ---- MCP-routed tools (dispatched by name; args forwarded verbatim) ----
            elif name in mcp_tool_names:
                # Submit gate: require a prediction only when there is NO
                # submit currently in flight, AND this is actually a submit
                # call. Polling / check_submission / session_end / browse
                # all go through without the gate.
                if name in submit_tool_names:
                    need_prediction = not submit_in_flight and (
                        not pending_prediction
                        or pending_prediction.get("value") is None
                    )
                    if need_prediction:
                        blocked_submits_in_row += 1
                        output = (
                            "SUBMIT BLOCKED: you must call predict() before the "
                            "FIRST submit of a new simulation. "
                            "predict(metric, value, reasoning) records your "
                            "numeric hypothesis. Call predict() now, then submit."
                        )
                        if blocked_submits_in_row >= 3:
                            output += (
                                "\n\nYou have been blocked 3+ times. STOP calling "
                                "submit() and call predict() right now — otherwise "
                                "make progress by reading code or writing code."
                            )
                        emit({
                            "type": "item.completed",
                            "item": {
                                "type": "mcp_tool_call", "id": tool_id,
                                "tool": name, "arguments": args,
                                "result": {
                                    "content": [{"type": "text", "text": output}],
                                    "isError": True,
                                },
                            },
                        })
                        # Append tool result then continue to next tool_call.
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_id,
                            "content": truncate(output),
                        })
                        continue
                    blocked_submits_in_row = 0

                # Phase B fix: forward LLM's args UNMODIFIED. Legacy:
                # mcp.call("submit", {"wait": True}) — discarded
                # implementation_paths and sent a kw the new
                # Pydantic-validated connector rejects.
                try:
                    mcp_result = mcp.call(name, args)
                except Exception as e:
                    mcp_result = {"text": f"MCP ERROR: {e}", "is_error": True}
                output = mcp_result.get("text", "") or ""

                # Submit / poll bookkeeping (only when this is a submit
                # family call; non-submit MCP tools don't touch budget).
                if name in submit_tool_names | {"check_submission"}:
                    flags = _classify_submit_response(output)
                    metrics = _parse_metrics_from_response(output)
                    if flags["is_started"]:
                        submit_in_flight = True
                    if flags["is_result"]:
                        submit_in_flight = False
                        reflection = compare_prediction(
                            pending_prediction, metrics, primary_metric,
                        )
                        if reflection:
                            output = f"{output}\n\n{reflection}"
                        pending_prediction = {}
                        submits_done += 1
                        is_submit = True
                    if flags["is_rejected"] and not flags["is_result"]:
                        submit_in_flight = False

                if name == "session_end":
                    session_ended = True

                emit({
                    "type": "item.completed",
                    "item": {
                        "type": "mcp_tool_call", "id": tool_id,
                        "tool": name, "arguments": args,
                        "result": {
                            "content": [{"type": "text", "text": output}],
                            "structured_content": {"result": output},
                        },
                    },
                })

            else:
                output = (
                    f"Unknown tool: {name}. Available: "
                    f"{sorted(LOCAL_TOOL_NAMES | mcp_tool_names)}"
                )
                emit({
                    "type": "item.completed",
                    "item": {
                        "type": "mcp_tool_call", "id": tool_id,
                        "tool": name, "arguments": args,
                        "result": {
                            "content": [{"type": "text", "text": output}],
                            "isError": True,
                        },
                    },
                })

            # Feed result back to model (M1-compressed).
            messages.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "content": truncate(output),
            })

            if is_submit and submits_done >= max_submits:
                emit({"type": "turn.completed", "usage": usage})
                print(
                    f"[archharness] max_submits={max_submits} reached, stopping",
                    file=sys.stderr, flush=True,
                )
                mcp.close()
                return

        emit({"type": "turn.completed", "usage": usage})

    mcp.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", required=True, help="User task prompt")
    parser.add_argument("--system-append", default="", help="Appended to system prompt")
    parser.add_argument("--mcp-url", required=True, help="MCP server URL (e.g. http://host:PORT/mcp)")
    # LLM endpoint controlled by env vars LLM_BASE_URL / LLM_API_KEY / LLM_MODEL.
    parser.add_argument("--max-turns", type=int, default=60)
    parser.add_argument("--max-submits", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=14400)
    parser.add_argument("--metric", default="ipc")
    args = parser.parse_args()

    run_loop(
        user_prompt=args.prompt,
        system_append=args.system_append,
        mcp_url=args.mcp_url,
        max_turns=args.max_turns,
        max_submits=args.max_submits,
        round_timeout=args.timeout,
        primary_metric=args.metric,
    )


if __name__ == "__main__":
    main()
