"""Read managed-vLLM ``endpoint.json`` files written by SLURM jobs.

The SLURM ``gemma4_endpoint.json`` flips ``ready: true`` once the vLLM
server inside the job is serving traffic. This module reads that flag
and the ``base_url`` it advertises, with a small in-memory TTL cache so
the proxy does not stat the file on every request.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger("archbench.serving.endpoint_discovery")

DEFAULT_TTL_S = 30.0  # how long to trust a cached endpoint read

# (mtime_ns, last_check_ts, base_url_or_None)
_cache: dict[str, Tuple[int, float, Optional[str]]] = {}
_cache_lock = threading.Lock()


def get_managed_vllm_url(
    endpoint_json_path: Path,
    *,
    ttl_s: float = DEFAULT_TTL_S,
    _now: Optional[float] = None,
) -> Optional[str]:
    """Return the base_url advertised by an endpoint.json iff ``ready: true``.

    Returns ``None`` for: missing file, malformed JSON, ``ready: false``,
    or any I/O error. Logs at DEBUG so callers can see why.

    Caches the result for ``ttl_s`` seconds keyed on the absolute path
    AND the file's mtime — so an mtime bump (the SLURM job rewriting
    the file) invalidates the cached value even within the TTL window.
    """
    path = Path(endpoint_json_path).expanduser().resolve()
    key = str(path)
    now = _now if _now is not None else time.time()

    try:
        mtime_ns = path.stat().st_mtime_ns
    except FileNotFoundError:
        log.debug("endpoint.json missing: %s", path)
        return None
    except OSError as e:
        log.debug("endpoint.json stat failed (%s): %s", path, e)
        return None

    with _cache_lock:
        cached = _cache.get(key)
        if (
            cached is not None
            and cached[0] == mtime_ns
            and (now - cached[1]) < ttl_s
        ):
            return cached[2]

    base_url = _read_base_url(path)

    with _cache_lock:
        _cache[key] = (mtime_ns, now, base_url)
    return base_url


def _read_base_url(path: Path) -> Optional[str]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        log.debug("endpoint.json read failed (%s): %s", path, e)
        return None
    if not isinstance(data, dict):
        log.debug("endpoint.json (%s) is not an object", path)
        return None
    if not data.get("ready"):
        log.debug("endpoint.json (%s) ready=false", path)
        return None
    url = data.get("base_url")
    if not isinstance(url, str) or not url:
        log.debug("endpoint.json (%s) missing base_url", path)
        return None
    return url


def clear_cache() -> None:
    """Drop all cached endpoint reads. Tests use this between cases."""
    with _cache_lock:
        _cache.clear()
