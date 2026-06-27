"""Read individual keys from a .env file without polluting os.environ.

Design intent (per user): explicit per-call file read, no startup
injection. Each caller asks for the specific key it needs.
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional


def read_env(key: str, env_file: Optional[Path] = None) -> Optional[str]:
    """Read a single key from a .env file. Returns None if not found.

    By default looks at <repo_root>/.env. Pass an explicit Path to override.

    Does NOT inject into os.environ. Does NOT cache. Each call is a fresh
    read - cheap (.env files are small) and means changes are picked up
    without restarting anything.
    """
    if env_file is None:
        env_file = Path(__file__).resolve().parents[2] / ".env"
    if not env_file.exists():
        return None
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(f"{key}="):
            continue
        value = line.split("=", 1)[1].strip()
        # strip surrounding quotes if present
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        return value
    return None
