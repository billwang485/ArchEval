"""Model proxy — single OpenAI-compatible endpoint over many backends.

Agent containers always hit `http://host.podman.internal:<port>/v1`. This
proxy reads ``routes.yaml`` to map the request's ``model`` field to a
backend (managed vLLM, OpenAI, Anthropic, Together, Bedrock, ...) and
forwards the call. See ``archbench/serving/proxy.py`` for the FastAPI app and
``archbench/serving/backends.py`` for the per-backend dispatch logic.
"""

from archbench.serving.routes import RouteEntry, load_routes  # noqa: F401

__all__ = ["RouteEntry", "load_routes"]
