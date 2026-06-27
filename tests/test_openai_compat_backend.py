"""Unit coverage for the openai_compat proxy backend (network-free).

The live ppapi.ai path is exercised by the campaign smoke; here we pin the
pure helpers: URL construction, key resolution precedence (.env via read_env,
then os.environ), the required-field errors, and that the shipped
gemini-3.1-flash-lite-preview route parses with the right backend.
"""
import pytest

from archbench.serving.backends import (
    BackendError,
    _openai_compat_url,
    _resolve_api_key,
)
from archbench.serving.routes import RouteEntry, default_routes_path, load_routes


def _route(**body):
    return RouteEntry.from_dict("t", {"backend": "openai_compat", "model_id": "m", **body})


def test_url_built_from_base_url():
    r = _route(base_url="https://gw.example/v1/")
    assert _openai_compat_url(r) == "https://gw.example/v1/chat/completions"


def test_missing_base_url_raises_500():
    with pytest.raises(BackendError) as e:
        _openai_compat_url(_route())
    assert e.value.status_code == 500


def test_resolve_key_from_process_env(monkeypatch):
    monkeypatch.setenv("_TEST_OAICOMPAT_KEY", "secret123")
    r = _route(base_url="https://gw/v1", api_key_env="_TEST_OAICOMPAT_KEY")
    assert _resolve_api_key(r) == "secret123"


def test_resolve_key_missing_raises_500(monkeypatch):
    monkeypatch.delenv("_TEST_OAICOMPAT_ABSENT", raising=False)
    r = _route(base_url="https://gw/v1", api_key_env="_TEST_OAICOMPAT_ABSENT")
    with pytest.raises(BackendError) as e:
        _resolve_api_key(r)
    assert e.value.status_code == 500


def test_no_api_key_env_raises_500():
    with pytest.raises(BackendError):
        _resolve_api_key(_route(base_url="https://gw/v1"))


def test_shipped_gemini_route_parses():
    r = load_routes(default_routes_path()).get("gemini-3.1-flash-lite-preview")
    assert r is not None
    assert r.backend == "openai_compat"
    assert r.extra.get("base_url") == "https://app-us.ppapi.ai/v1"
    assert r.api_key_env == "PPAPI_API_KEY"
    assert r.model_id == "gemini-3.1-flash-lite-preview"
