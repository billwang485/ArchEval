"""Shared challenge-directory resolution for plugins, runtimes, and the CLI.

Single source of truth for deriving the three load-bearing directories
of a challenge — ``simulator/``, ``evaluation/``, and ``starter/`` —
from a `Challenge` object (and, optionally, a fallback challenge-root
``Path``).

Why this lives in `archbench.core`
----------------------------

Two layouts coexist during the tier rollout:

1. **Legacy 3-subdir layout** (canonical pre-Phase-H):
   ``<challenge_dir>/{simulator,evaluation,challenge/starter}/`` —
   each challenge owns its own simulator + evaluation dir, and the
   starter lives under ``challenge/starter/`` (with a fallback to a
   flat ``starter/`` for some early challenges).

2. **Tier layout** (Phase-H+): challenges in a family share simulator
   + evaluation config under ``<family>/common/`` and only the
   tier-specific starter lives under the tier dir. ``load_challenge``
   populates ``challenge.simulator_dir`` / ``evaluation_dir`` /
   ``starter_dir`` to point at the right resolved paths.

The CLI, the per-sim plugins, and the runtime base all need the SAME
resolution rules so a tier-mode challenge resolves identically to a
legacy-mode one — and so legacy callers that only have a
``challenge_dir`` keep working. Duplicating the rules in each caller
is how lessons-learned §11/§15 happened (multiple sources of truth
silently drift). This module is that one source.

Invariant
---------

Legacy (3-subdir) challenges resolve IDENTICALLY here to how the
pre-Phase-H code resolved them. Tier-mode challenges resolve under
``<family>/common/`` per the fields set on the `Challenge`.
"""

from __future__ import annotations

from typing import Optional


def resolved_dirs(challenge, challenge_dir: Optional["Path"] = None):  # type: ignore[name-defined]
    """Return ``(simulator_dir, evaluation_dir, starter_dir)`` for a challenge.

    Prefers the resolved fields ``load_challenge`` populates on the
    `Challenge` (new tier layout: ``simulator_dir`` / ``evaluation_dir``
    may live under ``<family>/common/``; ``starter_dir`` lives under the
    tier dir). Falls back to the legacy 3-subdir construction relative
    to ``challenge_dir`` so this works during the parallel-agent rollout
    before A1's loader landing — and so any external caller passing a
    bare ``challenge_dir`` still works.

    The fallback matches the contract: simulator + evaluation under the
    challenge root, starter under ``challenge/starter`` then ``starter``.

    Parameters
    ----------
    challenge:
        A `Challenge` (or duck-typed equivalent). May expose
        ``simulator_dir``, ``evaluation_dir``, ``starter_dir``, and/or
        ``challenge_dir`` attributes. Any missing field falls through
        to the legacy construction.
    challenge_dir:
        Optional explicit root for the legacy fallback. When ``None``,
        derived from ``getattr(challenge, "challenge_dir", None)`` and
        finally ``Path('.')``.

    Returns
    -------
    tuple[Path, Path, Path]
        ``(simulator_dir, evaluation_dir, starter_dir)`` as `pathlib.Path`.
    """
    # Local import to avoid any circular-import risk from callers that
    # already have `Path` in scope; keeps this module zero-dep beyond stdlib.
    from pathlib import Path

    if challenge_dir is None:
        challenge_dir = getattr(challenge, "challenge_dir", None) or Path(".")
    challenge_dir = Path(challenge_dir)

    sim_dir = getattr(challenge, "simulator_dir", None) \
        or (challenge_dir / "simulator")
    eval_dir = getattr(challenge, "evaluation_dir", None) \
        or (challenge_dir / "evaluation")
    starter_dir = getattr(challenge, "starter_dir", None)
    if starter_dir is None:
        starter_dir = challenge_dir / "challenge" / "starter"
        if not starter_dir.is_dir():
            starter_dir = challenge_dir / "starter"
    return (Path(sim_dir), Path(eval_dir), Path(starter_dir))


__all__ = ["resolved_dirs"]
