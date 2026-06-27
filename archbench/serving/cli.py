"""``archbench-proxy`` entry point — starts the FastAPI proxy on a fixed port.

The default port (4001) matches the legacy ARCHEVAL proxy so agent
containers can keep their hard-coded ``http://host.podman.internal:4001/v1``
URLs. Pass ``--port 0`` to let the OS pick a free port (the chosen
port is printed to stdout).
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
from pathlib import Path

import uvicorn

from archbench.serving.proxy import create_app
from archbench.serving.routes import default_routes_path

DEFAULT_PORT = 4001  # matches the legacy ARCHEVAL proxy

log = logging.getLogger("archbench.serving.cli")


def _find_free_port() -> int:
    """Ask the OS for an unused TCP port (only used when --port 0)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="archbench-proxy",
        description=(
            "Start the archbench model proxy: a single OpenAI-compatible Chat "
            "Completions endpoint over many model backends (managed vLLM, "
            "OpenAI, Anthropic, ...)."
        ),
    )
    parser.add_argument(
        "--routes",
        type=Path,
        default=default_routes_path(),
        help=(
            "Path to routes.yaml (default: the bundled "
            "archbench/serving/routes.yaml inside the installed package)"
        ),
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host/interface to bind (default: 0.0.0.0 — reachable from podman)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=(
            f"TCP port (default: {DEFAULT_PORT}, matches legacy ARCHEVAL proxy; "
            "pass 0 to pick a free port)"
        ),
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
        help="uvicorn log level (default: info)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.routes.is_file():
        log.error("routes file not found: %s", args.routes)
        return 2

    port = _find_free_port() if args.port == 0 else args.port

    # Build the app eagerly so we surface routes.yaml parse errors before
    # uvicorn starts (less confusing than uvicorn's import-time traceback).
    app = create_app(routes_path=args.routes)
    log.info("archbench-proxy starting: host=%s port=%d routes=%s",
             args.host, port, args.routes)

    uvicorn.run(app, host=args.host, port=port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
