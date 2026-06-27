"""Tests for the archbench model proxy (archbench/serving).

Strategy: use ``httpx.ASGITransport`` against the FastAPI app in-process
so we never bind a real port. Backends are exercised by writing temp
``endpoint.json`` files (the managed_vllm path needs a real on-disk file
to read), then asserting on status codes + error shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from archbench.serving import endpoint_discovery
from archbench.serving.proxy import create_app
from archbench.serving.routes import Routes, load_routes


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_endpoint_cache():
    """Each test gets a fresh endpoint-discovery cache."""
    endpoint_discovery.clear_cache()
    yield
    endpoint_discovery.clear_cache()


@pytest.fixture
def tmp_endpoint_not_ready(tmp_path: Path) -> Path:
    """endpoint.json with ready=false — proxy should answer 503."""
    p = tmp_path / "gemma4_endpoint.json"
    p.write_text(
        json.dumps(
            {
                "model": "google/gemma-4-31B-it",
                "base_url": "http://nonexistent.example.com:8000/v1",
                "ready": False,
            }
        )
    )
    return p


@pytest.fixture
def tmp_endpoint_ready(tmp_path: Path) -> Path:
    """endpoint.json with ready=true pointing at a closed local port.

    Useful for asserting the proxy *attempts* the upstream call (we
    expect a 502 BackendError, not a 503 "not ready"). We use a port
    we just opened-and-closed on 127.0.0.1 so the connect fails fast
    rather than waiting on the network stack.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
    # socket closed here; the port is now free and a connect will RST.

    p = tmp_path / "gemma4_endpoint.json"
    p.write_text(
        json.dumps(
            {
                "model": "google/gemma-4-31B-it",
                "base_url": f"http://127.0.0.1:{closed_port}/v1",
                "ready": True,
            }
        )
    )
    return p


def _routes_with_gemma(endpoint_json: Path) -> Routes:
    return Routes.from_mapping(
        {
            "gemma4": {
                "backend": "managed_vllm",
                "endpoint_json": str(endpoint_json),
                "model_id": "google/gemma-4-31B-it",
                "supports_thinking": True,
            },
        }
    )


def _routes_with_stub() -> Routes:
    return Routes.from_mapping(
        {
            "gpt-5": {
                "backend": "openai",
                "api_key_env": "OPENAI_API_KEY",
                "model_id": "gpt-5",
            },
        }
    )


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --------------------------------------------------------------------------- #
# routes.yaml parsing                                                         #
# --------------------------------------------------------------------------- #


def test_bundled_routes_yaml_parses(monkeypatch):
    """The shipped routes.yaml must always parse — it's loaded on startup — and
    must carry NO per-user path: gemma4's ``endpoint_json`` resolves from
    ``${ARCHBENCH_GEMMA4_ENDPOINT_JSON}`` at load time so the file is portable across
    checkouts."""
    from archbench.serving.routes import default_routes_path

    # Env set → endpoint_json expands to it.
    monkeypatch.setenv("ARCHBENCH_GEMMA4_ENDPOINT_JSON", "/tmp/x/gemma4_endpoint.json")
    routes = load_routes(default_routes_path())
    assert "gemma4" in routes
    entry = routes.get("gemma4")
    assert entry is not None
    assert entry.backend == "managed_vllm"
    assert entry.model_id == "google/gemma-4-31B-it"
    assert entry.supports_thinking is True
    assert entry.endpoint_json is not None
    assert str(entry.endpoint_json) == "/tmp/x/gemma4_endpoint.json"

    # Env unset → endpoint_json is None (clean "unconfigured", NOT a literal
    # "${ARCHBENCH_...}" path), and the file still parses.
    monkeypatch.delenv("ARCHBENCH_GEMMA4_ENDPOINT_JSON", raising=False)
    entry2 = load_routes(default_routes_path()).get("gemma4")
    assert entry2 is not None
    assert entry2.endpoint_json is None


def test_routes_from_mapping_rejects_missing_fields():
    with pytest.raises(ValueError, match="missing required field 'backend'"):
        Routes.from_mapping({"x": {"model_id": "foo"}})
    with pytest.raises(ValueError, match="missing required field 'model_id'"):
        Routes.from_mapping({"x": {"backend": "openai"}})


def test_load_routes_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_routes(tmp_path / "does-not-exist.yaml")


def test_route_timeout_default():
    """Phase B fix: upstream timeout default bumped to 1800s so
    thinking-mode generations don't trip httpx's 5-min default."""
    from archbench.serving.backends import UPSTREAM_TIMEOUT_S, _route_timeout
    routes = Routes.from_mapping({
        "x": {"backend": "openai", "model_id": "gpt-x"},
    })
    entry = routes.get("x")
    assert entry is not None
    assert _route_timeout(entry) == UPSTREAM_TIMEOUT_S
    assert UPSTREAM_TIMEOUT_S >= 1200.0, (
        "upstream timeout default is too tight for thinking-mode "
        f"generations (got {UPSTREAM_TIMEOUT_S}s)"
    )


def test_route_timeout_per_route_override():
    """A route may override upstream_timeout_s in routes.yaml; the
    backend dispatcher must honor it."""
    from archbench.serving.backends import _route_timeout
    routes = Routes.from_mapping({
        "fast": {
            "backend": "openai", "model_id": "gpt-fast",
            "upstream_timeout_s": 30,
        },
        "slow_thinking": {
            "backend": "managed_vllm", "model_id": "thinkmodel",
            "upstream_timeout_s": 3600,
        },
    })
    assert _route_timeout(routes.get("fast")) == 30.0
    assert _route_timeout(routes.get("slow_thinking")) == 3600.0


def test_route_timeout_invalid_falls_back():
    """Bad upstream_timeout_s (string, etc.) must NOT crash — fallback to default."""
    from archbench.serving.backends import UPSTREAM_TIMEOUT_S, _route_timeout
    routes = Routes.from_mapping({
        "junk": {
            "backend": "openai", "model_id": "gpt-x",
            "upstream_timeout_s": "not-a-number",
        },
    })
    assert _route_timeout(routes.get("junk")) == UPSTREAM_TIMEOUT_S


# --------------------------------------------------------------------------- #
# /healthz + /v1/models                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_healthz(tmp_endpoint_not_ready: Path):
    app = create_app(routes=_routes_with_gemma(tmp_endpoint_not_ready))
    async with _client(app) as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_list_models_returns_routed_names(tmp_endpoint_not_ready: Path):
    app = create_app(routes=_routes_with_gemma(tmp_endpoint_not_ready))
    async with _client(app) as c:
        r = await c.get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    ids = [m["id"] for m in body["data"]]
    assert ids == ["gemma4"]
    assert body["data"][0]["owned_by"] == "managed_vllm"


# --------------------------------------------------------------------------- #
# /v1/chat/completions dispatch                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_chat_completions_unknown_model_returns_404(tmp_endpoint_not_ready: Path):
    app = create_app(routes=_routes_with_gemma(tmp_endpoint_not_ready))
    async with _client(app) as c:
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "no-such-model", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "no-such-model" in detail
    assert "gemma4" in detail  # lists available


@pytest.mark.asyncio
async def test_chat_completions_missing_model_returns_400(tmp_endpoint_not_ready: Path):
    app = create_app(routes=_routes_with_gemma(tmp_endpoint_not_ready))
    async with _client(app) as c:
        r = await c.post("/v1/chat/completions", json={"messages": []})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_chat_completions_managed_vllm_not_ready_returns_503(
    tmp_endpoint_not_ready: Path,
):
    """The big one: managed_vllm with ready=false must surface as 503.

    This proves the dispatcher honoured endpoint_discovery's None return
    and shaped a sensible error body for the caller.
    """
    app = create_app(routes=_routes_with_gemma(tmp_endpoint_not_ready))
    async with _client(app) as c:
        r = await c.post(
            "/v1/chat/completions",
            json={
                "model": "gemma4",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert r.status_code == 503
    err = r.json()["error"]
    assert err["type"] == "backend_error"
    assert err["route"] == "gemma4"
    assert err["backend"] == "managed_vllm"
    assert err["retriable"] is True
    assert "not ready" in err["message"]


@pytest.mark.asyncio
async def test_chat_completions_managed_vllm_ready_but_unreachable_returns_502(
    tmp_endpoint_ready: Path,
):
    """If endpoint.json says ready=true but the upstream is unreachable,
    the proxy should bubble a 502 (not a 503), so callers can tell the
    difference between 'job not up yet' and 'job up but networking
    broken'."""
    app = create_app(routes=_routes_with_gemma(tmp_endpoint_ready))
    async with _client(app) as c:
        r = await c.post(
            "/v1/chat/completions",
            json={
                "model": "gemma4",
                "messages": [{"role": "user", "content": "hi"}],
            },
            timeout=20.0,
        )
    assert r.status_code == 502
    err = r.json()["error"]
    assert err["backend"] == "managed_vllm"
    assert err["retriable"] is True


@pytest.mark.asyncio
async def test_chat_completions_stub_backend_returns_501():
    """Backends we haven't wired (openai/anthropic/...) must fail loudly
    with 501 + an actionable message, not a 500 stack trace."""
    app = create_app(routes=_routes_with_stub())
    async with _client(app) as c:
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 501
    err = r.json()["error"]
    assert err["backend"] == "openai"
    assert "not yet implemented" in err["message"]


# --------------------------------------------------------------------------- #
# endpoint_discovery                                                          #
# --------------------------------------------------------------------------- #


def test_endpoint_discovery_ready_true(tmp_path: Path):
    p = tmp_path / "ep.json"
    p.write_text(json.dumps({"ready": True, "base_url": "http://x:1/v1"}))
    assert endpoint_discovery.get_managed_vllm_url(p) == "http://x:1/v1"


def test_endpoint_discovery_ready_false_returns_none(tmp_path: Path):
    p = tmp_path / "ep.json"
    p.write_text(json.dumps({"ready": False, "base_url": "http://x:1/v1"}))
    assert endpoint_discovery.get_managed_vllm_url(p) is None


def test_endpoint_discovery_missing_file_returns_none(tmp_path: Path):
    assert endpoint_discovery.get_managed_vllm_url(tmp_path / "nope.json") is None


def test_endpoint_discovery_caches_then_invalidates_on_mtime(tmp_path: Path):
    p = tmp_path / "ep.json"
    p.write_text(json.dumps({"ready": False, "base_url": "http://x:1/v1"}))
    assert endpoint_discovery.get_managed_vllm_url(p) is None

    # Rewrite with ready=true and bump mtime — should re-read despite the cache.
    p.write_text(json.dumps({"ready": True, "base_url": "http://y:2/v1"}))
    import os

    new_time = p.stat().st_mtime + 5
    os.utime(p, (new_time, new_time))
    assert endpoint_discovery.get_managed_vllm_url(p) == "http://y:2/v1"
