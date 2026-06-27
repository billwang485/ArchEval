"""Route registry — parses ``routes.yaml`` into typed ``RouteEntry`` objects.

A single source of truth for backend dispatch metadata. The proxy looks
up the request's ``model`` field via ``Routes.get(name)``; missing keys
yield ``None`` so callers can raise a 404 with a helpful list of valid
routes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Backends we know how to dispatch to. Anything else parses but will
# produce a 501 ("backend not yet implemented") at call time so the
# error surface is the proxy server, not the YAML loader.
KNOWN_BACKENDS = {"managed_vllm", "openai_compat", "openai", "anthropic", "together", "bedrock"}


@dataclass(frozen=True)
class RouteEntry:
    """One row of ``routes.yaml``. ``name`` is the routing key."""

    name: str
    backend: str
    model_id: str
    endpoint_json: Optional[Path] = None
    api_key_env: Optional[str] = None
    supports_thinking: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, name: str, body: Dict[str, Any]) -> "RouteEntry":
        if "backend" not in body:
            raise ValueError(f"route {name!r}: missing required field 'backend'")
        if "model_id" not in body:
            raise ValueError(f"route {name!r}: missing required field 'model_id'")
        ep_path = body.get("endpoint_json")
        if ep_path:
            # Expand ${ENV_VAR} / ~ so the shipped routes.yaml carries no
            # per-user path. An unset var leaves a literal "$" → treat as
            # unconfigured (None) so the backend errors clearly instead of
            # opening a bogus "${ARCHBENCH_...}" file.
            ep_path = os.path.expanduser(os.path.expandvars(str(ep_path)))
            if "$" in ep_path:
                ep_path = None
        return cls(
            name=name,
            backend=body["backend"],
            model_id=body["model_id"],
            endpoint_json=Path(ep_path) if ep_path else None,
            api_key_env=body.get("api_key_env"),
            supports_thinking=bool(body.get("supports_thinking", False)),
            extra={
                k: v
                for k, v in body.items()
                if k
                not in {
                    "backend",
                    "model_id",
                    "endpoint_json",
                    "api_key_env",
                    "supports_thinking",
                }
            },
        )


class Routes:
    """Loaded ``routes.yaml``. Construct via ``load_routes`` or
    ``Routes.from_mapping`` (the latter is convenient for tests)."""

    def __init__(self, entries: Dict[str, RouteEntry]):
        self._entries = entries

    def get(self, name: str) -> Optional[RouteEntry]:
        return self._entries.get(name)

    def names(self) -> List[str]:
        return sorted(self._entries.keys())

    def all(self) -> List[RouteEntry]:
        return [self._entries[k] for k in self.names()]

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: str) -> bool:  # type: ignore[override]
        return key in self._entries

    @classmethod
    def from_mapping(cls, mapping: Dict[str, Dict[str, Any]]) -> "Routes":
        entries = {
            name: RouteEntry.from_dict(name, body) for name, body in mapping.items()
        }
        return cls(entries)


def load_routes(path: Path) -> Routes:
    """Parse a routes.yaml file into a :class:`Routes` registry.

    Raises ``FileNotFoundError`` if missing and ``ValueError`` for
    malformed entries (the loader is strict so misconfigurations fail
    at startup, not on first request).
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"routes file not found: {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"routes file {path} must be a mapping of name -> entry, got {type(raw).__name__}"
        )
    return Routes.from_mapping(raw)


def default_routes_path() -> Path:
    """The bundled ``routes.yaml`` next to this module."""
    return Path(__file__).resolve().parent / "routes.yaml"
