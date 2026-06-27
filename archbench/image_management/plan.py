"""plan.py — the PURE image resolver (docs/docker_management.md §1, §3, §7-K4).

One function, one frozen ``ImagePlan``. ``resolve_images(challenge, plugin,
runtime)`` makes **no docker calls and has no side effects** — it is a
read-only function of ``(challenge, plugin, runtime)``, trivially
unit-testable, and cannot regress a live run by itself (§3).

The three logical images it derives (§1):

  - ``simulator_image``       — the sim container (preflight + run); always
                                ``plugin.docker_image``.
  - ``agent_image``           — the agent sandbox. Resolved by
                                ``agent_image_mode``:
                                  * ``agent_centric``     -> ``runtime.docker_image``
                                  * ``simulator_centric`` -> ``_l2agent_image(sim, runtime.name)``
                                  * ``challenge_centric``    -> ``None`` (interface-only;
                                    session.py raises NotImplementedError, §1.3)
  - ``evaluation_sim_image``  — the pristine scorer + baseline provenance.
                                ``challenge.evaluation_sim_image`` (a pseudo-path)
                                if set, else DEFAULT to ``plugin.docker_image``
                                (the non-breaking anchor — §1.1, §3).

DEFAULT-IDENTICAL is the hard requirement: with no new YAML keys, every
existing challenge resolves to today's EXACT images. ``agent_centric`` ->
``runtime.docker_image``; ``simulator_centric`` -> ``_l2agent_image(sim)``;
eval == ``plugin.docker_image``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from archbench.image_management import manifest as images

if TYPE_CHECKING:
    from archbench.core.challenge import Challenge
    from archbench.core.runtime_base import AgentRuntime
    from archbench.core.plugin_base import SimulatorPlugin


# The closed enum of agent_image_mode values. ``challenge_centric`` is
# recognized-but-not-runnable (§1.3): resolve_images returns agent_image=None
# for it; session.py raises NotImplementedError before starting a container.
VALID_AGENT_IMAGE_MODES: tuple[str, ...] = (
    "agent_centric",
    "simulator_centric",
    "challenge_centric",
)

# Map a pseudo-path category alias (the bit before the first "/") onto the
# canonical archbench.image_management.manifest CATEGORIES. Authors write the short, intuitive
# scope ("sim/champsim", "agent/mini"); images.fully_qualified wants the
# plural taxonomy name ("simulators", "agents"). Both the short alias and the
# canonical name resolve, so "sim/champsim" and "simulators/champsim" are
# equivalent.
_CATEGORY_ALIASES: dict[str, str] = {
    "sim": "simulators",
    "simulator": "simulators",
    "simulators": "simulators",
    "agent": "agents",
    "agents": "agents",
    "runtime": "agents",
    "combined": "sim_agents",
    "sim_agent": "sim_agents",
    "sim_agents": "sim_agents",
    "challenge": "challenges",
    "challenges": "challenges",
}


def _l2agent_image(sim_image: str, runtime: str = "mini") -> str:
    """Combined (``simulator_centric``) agent image name — the ``_l2agent``
    convention. RUNTIME-AWARE: the agent loop baked into the sim image differs
    per runtime, so the image tag must too.

    The default ``runtime="mini"`` is the BACK-COMPAT anchor — it MUST keep the
    historic ``<sim>-l2agent`` string byte-for-byte, because (a) the manifest's
    ``sim_agents`` entries + the identity tests in ``tests/test_images_*.py`` and
    ``tests/test_tier_behavior.py`` assert the 1-arg / mini output verbatim, and
    (b) the existing mini L2 image + its docker tar are named that way. Any other
    runtime gets a runtime-namespaced suffix so it never silently mis-binds to
    the mini-loop image:

      mini  : ``localhost/archbench-champsim:v6`` -> ``localhost/archbench-champsim-l2agent:v6``
              (built by ``scripts/build_l2agent_image.sh``)
      codex : ``localhost/archbench-champsim:v6`` -> ``localhost/archbench-champsim-codex-l2agent:v6``
              (built by ``scripts/build_l2agent_image_codex.sh``)

    Each combined image is the simulator image (source + toolchain) PLUS that
    runtime's agent loop/CLI + agent user, so dependencies and sim source are
    present and the agent can build the real simulator itself.

    NB: this is the canonical home of the symbol; ``archbench.runtimes.session``
    re-exports it so ``tests/test_tier_behavior.py``'s
    ``from archbench.runtimes.session import _l2agent_image`` keeps working and its
    exact string output is preserved (docs §11). ``resolve_images`` passes the
    active runtime's ``.name`` (defaulting to "mini" for any object without one,
    e.g. the unit-test stub plugin/runtime).
    """
    # mini is the unsuffixed back-compat anchor; every other runtime is
    # namespaced ``-<runtime>-l2agent`` so a missing image fails loud at
    # ensure_image rather than silently running the mini loop.
    suffix = "l2agent" if runtime == "mini" else f"{runtime}-l2agent"
    if ":" in sim_image:
        repo, tag = sim_image.rsplit(":", 1)
        return f"{repo}-{suffix}:{tag}"
    return f"{sim_image}-{suffix}"


def _resolve_pseudo_path(value: str, default: str) -> str:
    """Resolve an ``evaluation_sim_image`` pseudo-path to a concrete image tag.

    Vocabulary (a tiny, robust scheme):
      - empty / None              -> ``default`` (the caller's anchor).
      - ``plugin:default``        -> ``default`` (explicit form of the default).
      - a LITERAL tag             -> used verbatim. A string is a literal when
        it contains a ``:`` (a registry/port or tag colon, e.g.
        ``localhost/archbench-champsim:v6``) OR it has no ``/`` to split on.
      - ``<cat>/<key>``           -> ``images.fully_qualified(<cat>, <key>)``,
        split on the FIRST ``/`` (so ``sim/champsim`` -> simulators/champsim).
        ``<cat>`` is normalized via the category-alias map.

    The literal-vs-pseudo discrimination is deliberately conservative: a tag
    that carries a colon (``localhost/...:v6``) is NEVER mistaken for a
    pseudo-path, so an expert override pinning a non-default scorer (docs §4
    "Rare — pin a non-default scorer") passes through untouched.
    """
    v = (value or "").strip()
    if not v:
        return default
    if v == "plugin:default":
        return default
    # A colon means a concrete tag (registry/port or :tag) -> literal verbatim.
    if ":" in v:
        return v
    # No "/" to split on -> nothing to resolve; treat as a literal name.
    if "/" not in v:
        return v
    cat_raw, key = v.split("/", 1)
    cat = _CATEGORY_ALIASES.get(cat_raw.strip(), cat_raw.strip())
    return images.fully_qualified(cat, key)


@dataclass(frozen=True)
class ImagePlan:
    """The resolved 3-image set for one (challenge, plugin, runtime) triple.

    Frozen + pure: produced by :func:`resolve_images` with no docker calls.
    """

    simulator_image: str            # the sim container (preflight + run)
    agent_image: Optional[str]      # the agent sandbox (None for challenge_centric)
    evaluation_sim_image: str       # the pristine scorer + baseline provenance
    agent_image_mode: str           # agent_centric|simulator_centric|challenge_centric
    source: str                     # provenance: how agent_image was derived


def resolve_images(
    challenge: "Challenge",
    plugin: "SimulatorPlugin",
    runtime: "AgentRuntime",
) -> ImagePlan:
    """Resolve the three logical images for a session. PURE — no side effects.

    See module docstring + docs §1/§3 for the full contract. The default
    (no new YAML keys) is byte-for-byte identical to the pre-K4 code:
    ``agent_centric`` -> ``runtime.docker_image``; ``simulator_centric`` ->
    ``_l2agent_image(plugin.docker_image, runtime.name)`` (mini keeps the
    historic unsuffixed ``<sim>-l2agent``); eval == ``plugin.docker_image``.
    """
    simulator_image = plugin.docker_image

    # evaluation_sim_image: the pristine scorer. Default to the sim's own image
    # (THE non-breaking anchor — §1.1) when the challenge doesn't pin one.
    eval_pseudo = getattr(challenge, "evaluation_sim_image", None)
    evaluation_sim_image = _resolve_pseudo_path(eval_pseudo, simulator_image)

    # agent_image_mode is already resolved + validated by the challenge loader
    # (challenge.py); read it off the dataclass. Default agent_centric for any
    # object that predates the field (defensive).
    mode = getattr(challenge, "agent_image_mode", "agent_centric")

    if mode == "agent_centric":
        agent_image: Optional[str] = runtime.docker_image
        source = "runtime.docker_image"
    elif mode == "simulator_centric":
        # Runtime-aware: the combined image bakes THIS runtime's agent loop, so
        # the tag is runtime-namespaced (mini -> <sim>-l2agent unsuffixed;
        # codex -> <sim>-codex-l2agent). getattr default "mini" keeps the
        # unit-test stub runtime (no .name) on the back-compat string.
        rt_name = getattr(runtime, "name", "mini")
        agent_image = _l2agent_image(simulator_image, rt_name)
        source = f"_l2agent_image(plugin.docker_image, runtime={rt_name!r})"
    elif mode == "challenge_centric":
        # Interface-only (§1.3): no agent image. session.py raises
        # NotImplementedError before starting an agent container.
        agent_image = None
        source = "challenge_centric (no agent image; not runnable)"
    else:
        # Defense in depth: the loader validates the enum and RAISES on an
        # unknown value (challenge.py — a deliberate divergence from
        # session_profile's warn-and-degrade, §1.2). If an unvalidated mode
        # ever reaches here, fail loud rather than silently degrade.
        raise ValueError(
            f"unknown agent_image_mode {mode!r}; expected one of "
            f"{VALID_AGENT_IMAGE_MODES}"
        )

    return ImagePlan(
        simulator_image=simulator_image,
        agent_image=agent_image,
        evaluation_sim_image=evaluation_sim_image,
        agent_image_mode=mode,
        source=source,
    )
