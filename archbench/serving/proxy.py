"""FastAPI app exposing a single OpenAI-compatible Chat Completions endpoint.

Endpoints:
  POST /v1/chat/completions  — OpenAI-shaped; routed by the request's model field
  GET  /v1/models            — list of routed model names (for verify.sh)
  GET  /healthz              — liveness probe

The app is constructed by :func:`create_app` so tests can spin up an
isolated instance with a temp ``routes.yaml`` (no real ports needed —
they use ``httpx.ASGITransport``).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from archbench.serving.backends import BackendError, dispatch
from archbench.serving.routes import Routes, default_routes_path, load_routes

log = logging.getLogger("archbench.serving.proxy")


def create_app(routes: Optional[Routes] = None, *, routes_path: Optional[Path] = None) -> FastAPI:
    """Build the FastAPI app.

    Pass either ``routes`` (a pre-built registry, for tests) or
    ``routes_path`` (a YAML file path, for the CLI). When neither is
    given the bundled ``archbench/serving/routes.yaml`` is loaded.
    """
    if routes is None:
        path = routes_path or default_routes_path()
        routes = load_routes(path)
        log.info("loaded %d route(s) from %s: %s", len(routes), path, routes.names())

    app = FastAPI(title="archbench model proxy", version="0.1.0")
    app.state.routes = routes

    # ----- liveness ----- #
    @app.get("/healthz")
    async def healthz() -> Dict[str, str]:
        return {"status": "ok"}

    # ----- model listing (OpenAI shape) ----- #
    @app.get("/v1/models")
    async def list_models() -> Dict[str, Any]:
        now = int(time.time())
        data = [
            {
                "id": entry.name,
                "object": "model",
                "created": now,
                "owned_by": entry.backend,
            }
            for entry in routes.all()
        ]
        return {"object": "list", "data": data}

    # ----- the main event ----- #
    @app.post("/v1/chat/completions")
    async def chat_completions(
        request: Request,
        authorization: Optional[str] = Header(default=None),
    ) -> Any:
        # Trusted-local: we accept any Bearer token but log it at DEBUG
        # so we can spot misrouted traffic in dev. Future: shared secret.
        if authorization:
            log.debug("authorization header present (len=%d)", len(authorization))

        try:
            body: Dict[str, Any] = await request.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"invalid JSON body: {e}")

        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request body must be an object")

        model_name = body.get("model")
        if not model_name or not isinstance(model_name, str):
            raise HTTPException(
                status_code=400,
                detail="request body missing required string field 'model'",
            )

        route = routes.get(model_name)
        if route is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"no route for model {model_name!r}; "
                    f"available: {routes.names()}"
                ),
            )

        log.info(
            "dispatch model=%s -> backend=%s upstream_model=%s stream=%s",
            model_name,
            route.backend,
            route.model_id,
            bool(body.get("stream")),
        )

        try:
            result = await dispatch(route, body)
        except BackendError as e:
            log.warning("backend error (%s/%s): %s", route.name, route.backend, e.message)
            return JSONResponse(
                status_code=e.status_code,
                content={
                    "error": {
                        "message": e.message,
                        "type": "backend_error",
                        "route": route.name,
                        "backend": route.backend,
                        "retriable": e.retriable,
                    }
                },
            )

        if isinstance(result, tuple):
            stream_iter, content_type = result
            return StreamingResponse(stream_iter, media_type=content_type)
        return JSONResponse(content=result)

    return app


# Module-level app for `uvicorn archbench.serving.proxy:app` style invocations.
# Lazy: only built on first attribute access so importing this module
# doesn't require routes.yaml to exist (handy for tests that import
# create_app directly).
_app: Optional[FastAPI] = None


def __getattr__(name: str):  # PEP 562
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
