"""Registered simulator plugins.

Add a new plugin by:
  1. Drop `simulators/<name>/plugin.py` with a `SimulatorPlugin` subclass
     (plus Dockerfile, verify.sh, etc. — one folder per image).
  2. Register it in the `_REGISTRY` dict below.

See `docs/adding_a_simulator.md` for the full walkthrough.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from archbench.core.plugin_base import SimulatorPlugin


from simulators.astrasim import AstraSimPlugin
from simulators.champsim import ChampSimPlugin
from simulators.dramsys import DRAMSysPlugin
from simulators.gem5 import GEM5Plugin
from simulators.mnsim import MNSIMPlugin
from simulators.ramulator import RamulatorPlugin
from simulators.scalesim import ScaleSimPlugin
from simulators.timeloop import TimeloopPlugin

_REGISTRY: dict[str, type] = {
    "astrasim": AstraSimPlugin,
    "champsim": ChampSimPlugin,
    "dramsys": DRAMSysPlugin,
    "gem5": GEM5Plugin,
    "mnsim": MNSIMPlugin,
    "ramulator": RamulatorPlugin,
    "scalesim": ScaleSimPlugin,
    "timeloop": TimeloopPlugin,
}


def get_plugin(name: str) -> SimulatorPlugin:
    """Return an instance of the named plugin. Raises KeyError if unknown."""
    if name not in _REGISTRY:
        raise KeyError(
            f"No simulator plugin registered for {name!r}. "
            f"Registered: {sorted(_REGISTRY.keys())}"
        )
    return _REGISTRY[name]()


def list_plugins() -> list[str]:
    """Return the names of all registered simulator plugins."""
    return sorted(_REGISTRY.keys())
