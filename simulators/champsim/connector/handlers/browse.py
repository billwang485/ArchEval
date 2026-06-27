"""Browse handlers — agent's read-only view of the simulator container.

Tools implemented here:

    browse_simulator(path)        -- list files under a path
    read_simulator_file(path)     -- read a file's contents

Both honour the challenge's ``source_blocklist`` plus the plugin's
``default_source_blocklist``. Every inbound path is run through
``anonymizer.translate_inbound`` and every outbound string through
``anonymizer.scrub_outbound`` so the agent's view stays anonymized.

Note: ``get_challenge_info`` was intentionally removed in P6 (the prompt
is the source of truth; agents that need the task description read
prompt.md or have it injected as the first user message by their
runtime), so this module does NOT implement it.
"""
from __future__ import annotations

import fnmatch

from simulators.champsim.connector.handlers.submit import SubmitContext


_REL_PATH_HINT = (
    "Use an absolute path beginning with '/'. ChampSim source lives at "
    "'/work/runtimes/champsim/' inside the simulator container "
    "(e.g. '/work/runtimes/champsim/inc/modules.h'). Workload traces live "
    "at '/work/workload_pools/champsim/'."
)


def handle_browse(ctx: SubmitContext, path: str) -> str:
    """List files in the simulator container, with blocklist enforcement.

    Relative paths are rejected with an explicit hint about ChampSim's
    source location. The Phase H e2e runs found agents burning ~90/200
    turns hitting ``cat: inc/modules.h: No such file`` because the
    container's cwd resolves relative paths against ``/`` (per
    ``ContainerManager.list_files``), not the source root.
    """
    path = ctx.anonymizer.translate_inbound(path)
    if not path.startswith("/"):
        return ctx.anonymizer.scrub_outbound(
            f"ERROR: relative path {path!r} not supported. {_REL_PATH_HINT}"
        )
    if _is_blocked(path, ctx):
        return ctx.anonymizer.scrub_outbound(
            f"ERROR: {path} is blocked by source_blocklist (challenge rule)."
        )
    try:
        out = ctx.sim.list_files(path)
    except Exception as e:
        return ctx.anonymizer.scrub_outbound(f"ERROR: {e!s}")
    return ctx.anonymizer.scrub_outbound(out)


def handle_read(ctx: SubmitContext, path: str) -> str:
    """Read a file from the simulator container, with blocklist.

    Same absolute-path requirement as ``handle_browse``; see that
    docstring for the bug-class rationale.
    """
    path = ctx.anonymizer.translate_inbound(path)
    if not path.startswith("/"):
        return ctx.anonymizer.scrub_outbound(
            f"ERROR: relative path {path!r} not supported. {_REL_PATH_HINT}"
        )
    if _is_blocked(path, ctx):
        return ctx.anonymizer.scrub_outbound(
            f"ERROR: {path} is blocked by source_blocklist (challenge rule)."
        )
    try:
        out = ctx.sim.read_file(path)
    except Exception as e:
        return ctx.anonymizer.scrub_outbound(f"ERROR: {e!s}")
    return ctx.anonymizer.scrub_outbound(out)


def _is_blocked(path: str, ctx: SubmitContext) -> bool:
    patterns = list(ctx.challenge.source_blocklist)
    patterns += ctx.plugin.default_source_blocklist(ctx.challenge)
    return any(fnmatch.fnmatch(path, p) for p in patterns)


__all__ = ["handle_browse", "handle_read"]
