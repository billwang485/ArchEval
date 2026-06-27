"""[concept: EVALUATE — see ARCHITECTURE.md]

BaseEvaluator ABC + LLM-as-judge helper.

Every post-session evaluator subclasses :class:`BaseEvaluator`. The
public surface is one method, :meth:`BaseEvaluator.evaluate`, which
takes the loaded Challenge, the per-run results directory, and the
evaluator's config block (from ``challenge.yaml``'s ``evaluations:``
list) and returns a JSON-serializable dict — the score report.

The session-orchestrator (``archbench/runtimes/session.py``) iterates over
``challenge.evaluations`` in its finally block and writes each report
to ``results/<run>/eval_<evaluator_name>.json``. A failing evaluator
must not crash the session; it logs and continues to the next one
(see :func:`archbench.runtimes.session.run_session`).

LLM-as-judge calls go through :func:`judge`, which:
  - tries the Anthropic SDK first (env: ``ANTHROPIC_API_KEY``);
  - falls back to the local ARCHBENCH proxy if reachable;
  - degrades to ``{"score": None, "rationale": "no judge configured"}``
    plus a warning log if neither is available — does NOT crash.

This MVP keeps the judge dead-simple: one shot, JSON-mode output
expected, parse with a forgiving regex (greedy ``{...}`` block).
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from archbench.core.challenge import Challenge

log = logging.getLogger("archbench.evaluators")


# The model to use for judging. Haiku is cheap+fast and accurate enough
# for binary verdicts on the direct Anthropic SDK; bump if eval quality
# is the bottleneck. Note: vectorengine.ai (3rd-party gateway, default
# below) maps Claude under a different model code — see
# DEFAULT_VECTORENGINE_MODEL.
DEFAULT_JUDGE_MODEL = "claude-haiku-4-5"

# Vectorengine.ai is a third-party Chinese API gateway re-exposing
# Anthropic models via an OpenAI-compatible /chat/completions endpoint.
# It's the default backend for development because it's noticeably
# cheaper than Anthropic direct, and the model code differs from the
# Anthropic SDK's canonical name.
DEFAULT_VECTORENGINE_MODEL = "claude-opus-4-7"
DEFAULT_VECTORENGINE_BASE_URL = "https://api.vectorengine.ai/v1"

# Default proxy URL — only consulted if both vectorengine and Anthropic
# SDK paths fail (or aren't configured).
DEFAULT_PROXY_URL = os.environ.get("ARCHBENCH_PROXY_URL", "http://127.0.0.1:8000/v1")


class BaseEvaluator(ABC):
    """Abstract base for post-session evaluators.

    Subclasses MUST set a class attribute ``name`` (str) — this is the
    key the registry uses to look up the evaluator from
    ``challenge.yaml`` ``evaluations: [{evaluator: <name>, ...}]``.
    """

    name: str = ""

    @abstractmethod
    def evaluate(
        self,
        challenge: "Challenge",
        results_dir: Path,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """Run the evaluation and return a JSON-serializable score dict.

        Args:
          challenge:   The loaded :class:`archbench.core.challenge.Challenge`
                       (sees challenge_dir, baseline.json, deliverables…).
          results_dir: The per-run results directory
                       (``results/<challenge>/<run_name>/``). Read
                       ``trajectory.jsonl``, ``submit_outcomes.jsonl``,
                       ``workspace/`` here; the runner persists the
                       returned dict to ``eval_<name>.json`` here.
          config:      The ``config:`` block from challenge.yaml (free-form).

        Returns:
          A dict with at minimum a top-level ``ok: bool`` and any
          evaluator-specific fields. Must be JSON-serializable.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# LLM-as-judge
# ---------------------------------------------------------------------------


def judge(
    prompt: str,
    context: Optional[dict[str, Any]] = None,
    *,
    model: str = DEFAULT_JUDGE_MODEL,
    max_tokens: int = 1024,
) -> dict[str, Any]:
    """One-shot LLM call returning a parsed JSON verdict.

    The judge is expected to reply with a JSON object containing at
    least ``score`` (0 or 1 for binary MVP) and ``rationale`` (string).
    The function never raises; on any error it returns
    ``{"score": None, "rationale": "<reason>"}``.

    Args:
      prompt:   The judge prompt. The caller is responsible for including
                whatever context the judge needs (file content, trajectory
                excerpts, etc.).
      context:  Optional structured context appended to the prompt as a
                fenced JSON block. Convenience for callers that want to
                pass attachments without hand-formatting them.
      model:    The model name. Defaults to a cheap Haiku-class model.
      max_tokens: Generation cap.

    Returns:
      Parsed dict including ``score`` and ``rationale`` keys (always).
    """
    full_prompt = prompt
    if context:
        try:
            blob = json.dumps(context, indent=2, default=str)
        except Exception:
            blob = str(context)
        full_prompt = f"{prompt}\n\nContext (JSON):\n```json\n{blob}\n```"

    from archbench.core.env_file import read_env

    # 1. Vectorengine.ai gateway (default, cheaper than Anthropic direct).
    ve_key = (
        os.environ.get("VECTORENGINE_API_KEY")
        or read_env("VECTORENGINE_API_KEY")
    )
    if ve_key:
        ve_base = (
            os.environ.get("VECTORENGINE_BASE_URL")
            or read_env("VECTORENGINE_BASE_URL")
            or DEFAULT_VECTORENGINE_BASE_URL
        )
        # If caller didn't pass a custom model, swap to the
        # vectorengine-specific code (claude-opus-4-7 maps to Opus on
        # the gateway's naming).
        ve_model = (
            model if model != DEFAULT_JUDGE_MODEL
            else DEFAULT_VECTORENGINE_MODEL
        )
        result = _judge_via_vectorengine(
            full_prompt, ve_key, ve_base, ve_model, max_tokens,
        )
        if result is not None:
            return result

    # 2. Anthropic SDK direct.
    api_key = os.environ.get("ANTHROPIC_API_KEY") or read_env("ANTHROPIC_API_KEY")
    if api_key:
        result = _judge_via_anthropic(full_prompt, api_key, model, max_tokens)
        if result is not None:
            return result

    # 3. Proxy fallback (only if it's actually reachable).
    proxy_url = os.environ.get("ARCHBENCH_PROXY_URL", DEFAULT_PROXY_URL)
    proxy_model = os.environ.get("ARCHBENCH_JUDGE_MODEL")  # let user pick
    if proxy_model:
        result = _judge_via_proxy(full_prompt, proxy_url, proxy_model, max_tokens)
        if result is not None:
            return result

    # 4. Degrade — log once per call, never crash.
    log.warning(
        "judge: no LLM backend configured (VECTORENGINE_API_KEY unset, "
        "ANTHROPIC_API_KEY unset, ARCHBENCH_JUDGE_MODEL unset). "
        "Returning score=None."
    )
    return {"score": None, "rationale": "no judge configured"}


def _judge_via_vectorengine(
    prompt: str, api_key: str, base_url: str, model: str, max_tokens: int,
) -> Optional[dict[str, Any]]:
    """Try vectorengine.ai (3rd-party gateway, OpenAI-compatible /chat/completions).

    Returns None on unreachable network or transport-level failure so
    the caller can fall through to the next backend. Returns a parsed
    verdict dict on success (or a degraded ``{score: None, rationale: ...}``
    if the model replied but its content didn't parse).
    """
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        text = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        parsed = _parse_judge_json(text)
        if parsed is None:
            return {
                "score": None,
                "rationale": f"judge response not parseable: {text[:200]!r}",
            }
        parsed.setdefault("rationale", "")
        return parsed
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        log.warning("judge: vectorengine unreachable (%s)", e)
        return None
    except Exception as e:
        log.warning("judge: vectorengine call failed: %s", e)
        return None


def _judge_via_anthropic(
    prompt: str, api_key: str, model: str, max_tokens: int,
) -> Optional[dict[str, Any]]:
    """Try the Anthropic SDK. Returns None on import / API failure."""
    try:
        import anthropic  # type: ignore
    except ImportError:
        log.debug("judge: anthropic SDK not installed; skipping")
        return None
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        # Concatenate text blocks (the SDK returns a list of blocks)
        text = "".join(
            getattr(b, "text", "") for b in msg.content
            if getattr(b, "type", "") == "text"
        )
        parsed = _parse_judge_json(text)
        if parsed is None:
            return {
                "score": None,
                "rationale": f"judge response not parseable: {text[:200]!r}",
            }
        # Ensure rationale always present
        parsed.setdefault("rationale", "")
        return parsed
    except Exception as e:
        log.warning("judge: anthropic SDK call failed: %s", e)
        return None


def _judge_via_proxy(
    prompt: str, base_url: str, model: str, max_tokens: int,
) -> Optional[dict[str, Any]]:
    """Try the local ARCHBENCH proxy (OpenAI-compatible chat-completions)."""
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        text = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        parsed = _parse_judge_json(text)
        if parsed is None:
            return {
                "score": None,
                "rationale": f"judge response not parseable: {text[:200]!r}",
            }
        parsed.setdefault("rationale", "")
        return parsed
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        log.debug("judge: proxy unreachable (%s); skipping", e)
        return None
    except Exception as e:
        log.warning("judge: proxy call failed: %s", e)
        return None


def _parse_judge_json(text: str) -> Optional[dict[str, Any]]:
    """Extract the first JSON object from `text`.

    The judge is asked to reply with a JSON object; in practice models
    sometimes wrap it in ```json fences or prose. We try literal parse
    first, then a greedy {...} regex.
    """
    text = text.strip()
    if not text:
        return None
    # Strip code fences if present
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = []
    if fenced:
        candidates.append(fenced.group(1))
    # Try the whole text
    candidates.append(text)
    # Greedy: first { to last }
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        candidates.append(text[first : last + 1])

    for c in candidates:
        try:
            parsed = json.loads(c)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    return None
