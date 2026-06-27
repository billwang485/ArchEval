"""Backend dispatchers — one async function per backend kind.

The proxy looks up a :class:`RouteEntry` and hands the OpenAI-shaped
request payload to the matching handler. Each handler returns either a
JSON-serialisable dict (for non-streaming requests) or an async iterator
of SSE byte chunks (when ``stream: true``).

MVP scope: ``managed_vllm`` is fully wired. ``openai`` / ``anthropic`` /
``together`` / ``bedrock`` are stubbed with a clear 501 message and a
TODO; the architecture is in place so we can fill them in via litellm
without touching the proxy server itself.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncIterator, Dict, Optional, Tuple, Union

import httpx

from archbench.serving.endpoint_discovery import get_managed_vllm_url
from archbench.serving.routes import RouteEntry

log = logging.getLogger("archbench.serving.backends")

# Default HTTP timeout (seconds) when forwarding to managed vLLM.
# Generous because long-context reasoning models can chew on a request
# for a while. The Phase B uptick: thinking-mode generations on
# gemma4-thinking routinely exceed 5 min and were tripping httpx's
# default ReadTimeout, surfacing to the caller as a 502 Bad Gateway. We
# now default to 1800s (30 min) so a normal thinking generation never
# hits the cap. Per-route override via routes.yaml's
# ``upstream_timeout_s`` (see _route_timeout below).
UPSTREAM_TIMEOUT_S = 1800.0


def _route_timeout(route: "RouteEntry") -> float:
    """Resolve the upstream HTTP timeout for a given route.

    Routes may set ``upstream_timeout_s: <seconds>`` in routes.yaml to
    override the default. Useful for routes serving short, deterministic
    completions where a fast failure beats a long hang, and for
    thinking-mode routes that legitimately need 20-30 minutes.

    Falls back to the global ``UPSTREAM_TIMEOUT_S`` (1800s) when the
    route doesn't carry the field. The value lives under
    ``RouteEntry.extra`` because it's not part of the core schema.
    """
    raw = route.extra.get("upstream_timeout_s") if route.extra else None
    if raw is None:
        return UPSTREAM_TIMEOUT_S
    try:
        return float(raw)
    except (TypeError, ValueError):
        log.warning(
            "route %r: invalid upstream_timeout_s=%r, using default %ss",
            route.name, raw, UPSTREAM_TIMEOUT_S,
        )
        return UPSTREAM_TIMEOUT_S


class BackendError(Exception):
    """Raised by a backend handler to signal an HTTP error response.

    The proxy server catches this and turns it into a JSON error body
    with ``status_code`` as the HTTP status. ``message`` is what we
    show the caller; keep it actionable.
    """

    def __init__(self, status_code: int, message: str, *, retriable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.retriable = retriable


# --------------------------------------------------------------------------- #
# Dispatch                                                                    #
# --------------------------------------------------------------------------- #


async def dispatch(
    route: RouteEntry,
    request_body: Dict[str, Any],
    *,
    http_client: Optional[httpx.AsyncClient] = None,
) -> Union[Dict[str, Any], Tuple[AsyncIterator[bytes], str]]:
    """Run the backend that matches ``route.backend``.

    Returns either:
      - a dict (the upstream JSON body) for non-streaming requests
      - a (async-iterator, content_type) tuple for ``stream: true``
    Raises :class:`BackendError` for upstream failures.
    """
    if route.backend == "managed_vllm":
        return await _managed_vllm(route, request_body, http_client=http_client)
    if route.backend == "openai_compat":
        return await _openai_compat(route, request_body, http_client=http_client)
    if route.backend in ("openai", "anthropic", "together", "bedrock"):
        return _stub_litellm(route)
    raise BackendError(
        status_code=500,
        message=f"unknown backend {route.backend!r} for route {route.name!r}",
    )


# --------------------------------------------------------------------------- #
# managed_vllm — the only backend fully wired for MVP                         #
# --------------------------------------------------------------------------- #


async def _managed_vllm(
    route: RouteEntry,
    body: Dict[str, Any],
    *,
    http_client: Optional[httpx.AsyncClient] = None,
) -> Union[Dict[str, Any], Tuple[AsyncIterator[bytes], str]]:
    if route.endpoint_json is None:
        raise BackendError(
            500,
            f"route {route.name!r} backend=managed_vllm requires endpoint_json",
        )
    base_url = get_managed_vllm_url(route.endpoint_json)
    if base_url is None:
        raise BackendError(
            503,
            (
                f"managed vLLM endpoint not ready for route {route.name!r}; "
                f"check the SLURM job + {route.endpoint_json} "
                "(expecting ready=true and base_url=...)"
            ),
            retriable=True,
        )

    forwarded = dict(body)
    forwarded["model"] = route.model_id

    # Pass-through the agent's `thinking=True` for reasoning-capable vLLM
    # models. vLLM expects this nested under chat_template_kwargs.
    if route.supports_thinking and forwarded.pop("thinking", False):
        forwarded.setdefault("chat_template_kwargs", {})["enable_thinking"] = True

    url = f"{base_url.rstrip('/')}/chat/completions"
    streaming = bool(forwarded.get("stream"))
    timeout_s = _route_timeout(route)

    if streaming:
        return (_stream_managed_vllm(url, forwarded, timeout_s),
                "text/event-stream")

    owned_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=timeout_s)
    try:
        resp = await client.post(url, json=forwarded)
    except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
        raise BackendError(
            502,
            f"managed vLLM unreachable at {url}: {e}",
            retriable=True,
        ) from e
    except httpx.HTTPError as e:
        raise BackendError(502, f"managed vLLM request failed: {e}") from e
    finally:
        if owned_client:
            await client.aclose()

    if resp.status_code >= 400:
        # Forward upstream errors with their original body so callers
        # see vLLM's exact diagnostic.
        try:
            payload = resp.json()
            message = json.dumps(payload)
        except ValueError:
            message = resp.text[:1000]
        raise BackendError(resp.status_code, f"managed vLLM error: {message}")
    return resp.json()


async def _stream_managed_vllm(
    url: str, payload: Dict[str, Any], timeout_s: float = UPSTREAM_TIMEOUT_S,
) -> AsyncIterator[bytes]:
    """Forward an SSE stream from vLLM byte-for-byte.

    ``timeout_s`` is the per-request total timeout, defaulting to the
    global ``UPSTREAM_TIMEOUT_S`` (1800s) but overridable per-route via
    ``routes.yaml``'s ``upstream_timeout_s``.
    """
    timeout = httpx.Timeout(timeout_s, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, json=payload) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise BackendError(
                    resp.status_code,
                    f"managed vLLM stream error: {body.decode(errors='replace')[:1000]}",
                )
            async for chunk in resp.aiter_raw():
                if chunk:
                    yield chunk


# --------------------------------------------------------------------------- #
# openai_compat — generic OpenAI-compatible upstream (real OpenAI, or any      #
# OpenAI-shaped gateway, e.g. ppapi.ai → Gemini). The API key stays in the     #
# host-side proxy (read on demand from .env), NEVER in the agent container.    #
# --------------------------------------------------------------------------- #

# Upstream statuses worth a retry (transient): rate-limit, gateway, overload.
_OPENAI_COMPAT_RETRY = {429, 500, 502, 503, 529}


def _resolve_api_key(route: "RouteEntry") -> str:
    if not route.api_key_env:
        raise BackendError(
            500, f"route {route.name!r} backend=openai_compat requires api_key_env"
        )
    # .env is read on demand (CLAUDE.md §1.14), not auto-loaded into os.environ;
    # fall back to the process env for explicit exports.
    key = None
    try:
        from archbench.core.env_file import read_env
        key = read_env(route.api_key_env)
    except Exception:
        key = None
    key = key or os.environ.get(route.api_key_env)
    if not key:
        raise BackendError(
            500,
            f"route {route.name!r}: API key env {route.api_key_env!r} is not set "
            "(.env or proxy process environment)",
        )
    return key


def _openai_compat_url(route: "RouteEntry") -> str:
    base = (route.extra or {}).get("base_url")
    if not base:
        raise BackendError(
            500, f"route {route.name!r} backend=openai_compat requires base_url"
        )
    return f"{str(base).rstrip('/')}/chat/completions"


async def _openai_compat(
    route: RouteEntry,
    body: Dict[str, Any],
    *,
    http_client: Optional[httpx.AsyncClient] = None,
) -> Union[Dict[str, Any], Tuple[AsyncIterator[bytes], str]]:
    url = _openai_compat_url(route)
    headers = {
        "Authorization": f"Bearer {_resolve_api_key(route)}",
        "Content-Type": "application/json",
    }
    forwarded = dict(body)
    forwarded["model"] = route.model_id
    # `thinking` is a vLLM-specific extra; OpenAI-shaped upstreams reject
    # unknown fields, so drop it unless the route explicitly opts in.
    if not route.supports_thinking:
        forwarded.pop("thinking", None)
    timeout_s = _route_timeout(route)

    if bool(forwarded.get("stream")):
        return (_stream_openai_compat(url, forwarded, headers, timeout_s),
                "text/event-stream")

    owned_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=timeout_s)
    try:
        resp = await client.post(url, json=forwarded, headers=headers)
    except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
        raise BackendError(
            502, f"openai_compat upstream unreachable at {url}: {e}", retriable=True
        ) from e
    except httpx.HTTPError as e:
        raise BackendError(502, f"openai_compat request failed: {e}") from e
    finally:
        if owned_client:
            await client.aclose()

    if resp.status_code >= 400:
        try:
            message = json.dumps(resp.json())
        except ValueError:
            message = resp.text[:1000]
        raise BackendError(
            resp.status_code, f"openai_compat error: {message}",
            retriable=resp.status_code in _OPENAI_COMPAT_RETRY,
        )
    return resp.json()


async def _stream_openai_compat(
    url: str, payload: Dict[str, Any], headers: Dict[str, str],
    timeout_s: float = UPSTREAM_TIMEOUT_S,
) -> AsyncIterator[bytes]:
    timeout = httpx.Timeout(timeout_s, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise BackendError(
                    resp.status_code,
                    f"openai_compat stream error: {body.decode(errors='replace')[:1000]}",
                )
            async for chunk in resp.aiter_raw():
                if chunk:
                    yield chunk


# --------------------------------------------------------------------------- #
# litellm-backed stubs — uncomment + implement when wiring real backends      #
# --------------------------------------------------------------------------- #


def _stub_litellm(route: RouteEntry) -> Dict[str, Any]:
    """Return-shape: raise a 501 so the proxy reports a clean error.

    TODO(litellm): replace the body of this with a litellm.acompletion
    call. The wiring should look roughly like::

        import litellm
        api_key = _require_api_key(route)
        resp = await litellm.acompletion(
            model=_litellm_model_string(route),  # e.g. "anthropic/claude-..."
            messages=body["messages"],
            api_key=api_key,
            **{k: v for k, v in body.items() if k in PASSTHROUGH_KEYS},
        )
        return resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)

    Until then we surface 501 with a clear reason so agents fail fast.
    """
    raise BackendError(
        501,
        (
            f"backend {route.backend!r} for route {route.name!r} is not yet "
            "implemented in archbench.serving.backends — MVP only wires managed_vllm. "
            "Stub: add a litellm.acompletion call in _stub_litellm."
        ),
    )


def _require_api_key(route: RouteEntry) -> str:
    """Helper for the litellm stubs once they are filled in."""
    if not route.api_key_env:
        raise BackendError(
            500,
            f"route {route.name!r} backend={route.backend!r} requires api_key_env",
        )
    key = os.environ.get(route.api_key_env)
    if not key:
        raise BackendError(
            500,
            (
                f"route {route.name!r}: env var {route.api_key_env!r} is not set "
                "in the proxy's process environment"
            ),
        )
    return key
