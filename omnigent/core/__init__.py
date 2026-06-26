"""First-party ("core") plugin assembly — the second tier of the three-tier
microkernel picture (Section 10 of ``docs/EXTENSION_FRAMEWORK_ANALYSIS.md``).

The kernel (``omnigent.kernel.extensions``, ``omnigent.kernel.pluggable``) is domain-free; the
third tier is out-of-core / third-party extensions discovered via entry-points.
This package is the *middle* tier: the set of first-party plugins that ship in
the repo and dogfood the very same ``OmnigentExtension`` Protocol + seam
machinery a third-party extension uses.

First-party seam contributions are registered unconditionally at server startup
through :func:`default_extensions` + :func:`register_firstparty_seams`, so core
dogfoods the same registries third-party extensions use. The documented core
route group is mounted through :func:`firstparty_route_extensions`, whose
``RoutesExtension`` reads the built store context from ``app.state`` during the
extension ``post_init`` phase.

Boot dependency order (Section 9.3): the plugins are instantiated in the order
the boot sequence requires — stores → identity → coordination → harnesses →
tools → policies → spec → routes → secrets → metrics/memory_maintenance →
skills/terminals. The order matters because later plugins' seam contributions
assume earlier seams are present (e.g. ``omnigent.harnesses`` ``requires``
``omnigent.stores``; ``omnigent.skills`` ``requires`` ``omnigent.spec``).

**Kept domain-free at import time.** Importing ``omnigent.core`` must not drag
the FastAPI / store stack onto the runner hot path, so every plugin module is
imported *inside* :func:`default_extensions`, exactly like the deferred-import
pattern the kernel uses (``omnigent.kernel.pluggable.registry.discover_extensions``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — typing only, never imported at runtime
    from omnigent.kernel.extensions import OmnigentExtension

logger = logging.getLogger(__name__)


# (module_path, class_attr) in Section 9.3 boot dependency order. Each plugin
# class is zero-arg constructible and carries its ``name`` (set by the SDK
# ``@extension`` decorator). The order is load-bearing — see module docstring.
#
# BDP-2516: ``omnigent.metrics`` and ``omnigent.memory_maintenance`` are NO
# LONGER in this first-party seam/plugin list — they were cut over to the
# *authoritative* boot path (``create_app`` starts their background loops
# unconditionally via :func:`firstparty_background_task_extensions`). Keeping
# them here too would double-register / double-start them. The other 11 plugins
# contribute seams unconditionally; only ``RoutesExtension`` is installed
# synchronously for route mounting to avoid legacy router hooks in other plugins
# double-mounting slices it now owns.
_FIRSTPARTY_PLUGINS: tuple[tuple[str, str], ...] = (
    # db/stores
    ("omnigent.stores._plugin", "StoresExtension"),
    # identity
    ("omnigent.identity._plugin", "IdentityExtension"),
    # coordination
    ("omnigent.coordination._plugin", "CoordinationExtension"),
    # harnesses (requires omnigent.stores)
    ("omnigent.runtime.harnesses._plugin", "HarnessesExtension"),
    # tools
    ("omnigent.tools.builtins._plugin", "BuiltinToolsExtension"),
    # policies
    ("omnigent.policies._plugin", "PoliciesExtension"),
    # spec
    ("omnigent.spec._plugin", "SpecExtension"),
    # routes (install_extensions → routers())
    ("omnigent.server.routes._plugin", "RoutesExtension"),
    # secrets
    ("omnigent.onboarding._plugin_secrets", "SecretsExtension"),
    # skills (requires omnigent.spec) + terminals
    ("omnigent.skills._plugin", "SkillsExtension"),
    ("omnigent.terminals._plugin", "TerminalsExtension"),
)


def default_extensions() -> list[OmnigentExtension]:
    """Instantiate the first-party plugins in Section 9.3 boot dependency order.

    Each plugin module is imported lazily (deferred domain import) and its class
    instantiated with zero args. A plugin whose module fails to import — or whose
    class is missing or won't construct — is **logged and skipped**, never fatal:
    the same best-effort isolation ``omnigent.kernel.extensions.discover_extensions``
    applies to third-party extensions, so a broken first-party plugin can never
    break server boot.

    :returns: the healthy first-party extension instances, in boot order. The
        list is suitable to pass straight to
        :func:`omnigent.kernel.extensions.install_extensions` (routers) and to
        :func:`register_firstparty_seams` (seam contributions); it conforms to
        the same ``OmnigentExtension`` Protocol the kernel already dispatches.
    """
    import importlib

    extensions: list[OmnigentExtension] = []
    for module_path, class_attr in _FIRSTPARTY_PLUGINS:
        try:
            module = importlib.import_module(module_path)
            cls = getattr(module, class_attr)
            extensions.append(cls())
        except Exception:  # noqa: BLE001 — a broken plugin must not break boot
            logger.warning(
                "first-party plugin %s:%s failed to load — skipping",
                module_path,
                class_attr,
                exc_info=True,
            )
    return extensions


def firstparty_route_extensions() -> list[OmnigentExtension]:
    """Build first-party extensions that own synchronous route mounting.

    ``default_extensions()`` includes non-route seam plugins and legacy shadow
    plugins such as ``omnigent.skills`` that still expose a narrow router hook
    for compatibility tests. Installing that whole set would double-mount route
    slices now owned by ``RoutesExtension``. Keep the authoritative route phase
    explicit until every legacy router hook is retired.
    """
    import importlib

    try:
        module = importlib.import_module("omnigent.server.routes._plugin")
        return [module.RoutesExtension()]
    except Exception:  # noqa: BLE001 — a broken plugin must not break boot
        logger.warning(
            "first-party route plugin omnigent.routes failed to load — skipping",
            exc_info=True,
        )
        return []


def register_firstparty_seams(extensions: list[OmnigentExtension]) -> None:
    """Register *extensions*' seam contributions through the SAME seam registries.

    Mirrors :meth:`omnigent.kernel.pluggable.registry.PluggableRegistry.discover_extensions`
    — but for an explicit, in-process list of first-party extensions instead of
    entry-point discovery. It reuses :data:`omnigent.kernel.pluggable.manifest.SEAMS`
    (the single seam declaration) and each seam's own
    :meth:`~omnigent.kernel.pluggable.registry.PluggableRegistry.register`, so no
    parallel registry or hook table is created here.

    For every ``(seam, accessor, hook)`` row it ``hasattr``-probes each extension
    for *hook*, calls it for its ``{name: factory}`` mapping, and registers each
    into the seam registry the accessor returns. Per-extension and per-seam
    errors are logged and skipped (a provider that is already registered — e.g.
    via the inline wiring or a re-run — is caught by the registry's conflict
    guard), so this is safe to run alongside ``discover_all_extensions``.

    The manifest accessors return stable per-process registries, so
    first-party contributions persist on the same seam plane that
    ``discover_all_extensions`` and ``capability_manifest`` use.

    :param extensions: the first-party extensions (typically
        :func:`default_extensions`'s result).
    """
    from omnigent.kernel.pluggable.errors import RegistryConflict
    from omnigent.kernel.pluggable.manifest import SEAMS

    for seam, accessor, hook in SEAMS:
        try:
            registry = accessor()
        except Exception:  # noqa: BLE001 — one bad seam must not break the rest
            logger.warning(
                "first-party seam %r registry unavailable (hook %r) — skipping",
                seam,
                hook,
                exc_info=True,
            )
            continue
        for ext in extensions:
            getter = getattr(ext, hook, None)
            if getter is None:
                continue
            try:
                contributed = getter()
            except Exception:  # noqa: BLE001 — extensions are best-effort
                logger.warning(
                    "first-party extension %r failed to contribute %s for seam %r",
                    getattr(ext, "name", ext),
                    hook,
                    seam,
                    exc_info=True,
                )
                continue
            for name, factory in dict(contributed or {}).items():
                try:
                    registry.register(name, factory)
                except RegistryConflict:
                    # Already registered (inline wiring or a prior run) — the
                    # provider exists, which is the desired end state. Skip.
                    continue
                except Exception:  # noqa: BLE001 — best-effort registration
                    logger.warning(
                        "first-party seam %r failed to register provider %r",
                        seam,
                        name,
                        exc_info=True,
                    )


def firstparty_background_factories(
    extensions: list[OmnigentExtension],
) -> list:
    """Collect *extensions*' ``background_tasks()`` factories (``hasattr``-probed).

    Mirrors :func:`omnigent.kernel.extensions.extension_background_factories` but for the
    explicit first-party list, so the composition root can start first-party
    background loops (``omnigent.metrics``, ``omnigent.memory_maintenance``)
    through the same lifespan task-creation path it already uses for third-party
    extensions. An extension that omits the optional ``background_tasks`` hook is
    silently skipped (Protocol back-compat).

    :param extensions: the first-party extensions.
    :returns: a flat list of zero-arg awaitable factories.
    """
    factories: list = []
    for ext in extensions:
        if hasattr(ext, "background_tasks"):
            try:
                factories.extend(ext.background_tasks())
            except Exception:  # noqa: BLE001 — best-effort
                logger.warning(
                    "first-party extension %r background_tasks() failed",
                    getattr(ext, "name", ext),
                    exc_info=True,
                )
    return factories


def firstparty_background_task_extensions(
    *,
    server_metrics: object | None = None,
    server_metrics_otel: object | None = None,
) -> list[OmnigentExtension]:
    """Build the AUTHORITATIVE always-on background-task plugins (BDP-2516).

    The lowest-risk first-party slices —  ``omnigent.metrics`` and
    ``omnigent.memory_maintenance`` — were cut over from the inline
    ``create_app`` / ``_lifespan`` wiring to this core plugin path. ``create_app``
    calls this **unconditionally** and starts the returned extensions'
    ``background_tasks()`` factories through the same lifespan task-creation path
    it already uses for third-party extension loops. Pass the result to
    :func:`firstparty_background_factories` to obtain the flat factory list.

    Only these two slices are built here — deliberately NOT the full
    :func:`default_extensions` set — so the cutover is surgical: no stores /
    identity / routes / etc. plugin is instantiated on the default boot path.

    ``omnigent.metrics`` is constructed with the live ``server_metrics`` /
    ``server_metrics_otel`` so the publish loop snapshots the SAME tracker the
    HTTP middleware records into — preserving exact parity with the removed
    inline ``publish_server_metrics_periodically(server_metrics, ...)`` callsite.
    ``omnigent.memory_maintenance`` takes no injected dependency (its loop
    resolves the memory store at run time), matching the removed zero-arg inline
    ``memory_maintenance_loop()`` callsite byte-for-byte.

    A plugin whose module fails to import or won't construct is logged and
    skipped (never fatal), exactly like :func:`default_extensions`.

    :param server_metrics: the ``create_app``-constructed live request tracker
        (``ServerPerformanceMetrics``) the metrics loop must snapshot.
    :param server_metrics_otel: the ``create_app``-constructed OTel publisher.
    :returns: ``[MetricsExtension, MemoryMaintenanceExtension]`` (healthy ones),
        ready for :func:`firstparty_background_factories`.
    """
    import importlib

    extensions: list[OmnigentExtension] = []

    try:
        metrics_mod = importlib.import_module("omnigent.server._plugin_metrics")
        extensions.append(
            metrics_mod.MetricsExtension(
                server_metrics=server_metrics,
                server_metrics_otel=server_metrics_otel,
            )
        )
    except Exception:  # noqa: BLE001 — a broken plugin must not break boot
        logger.warning(
            "authoritative background plugin omnigent.metrics failed to load — skipping",
            exc_info=True,
        )

    try:
        memmaint_mod = importlib.import_module("omnigent.runtime._plugin_memory_maintenance")
        extensions.append(memmaint_mod.MemoryMaintenanceExtension())
    except Exception:  # noqa: BLE001 — a broken plugin must not break boot
        logger.warning(
            "authoritative background plugin omnigent.memory_maintenance failed "
            "to load — skipping",
            exc_info=True,
        )

    return extensions


__all__ = [
    "default_extensions",
    "firstparty_background_factories",
    "firstparty_background_task_extensions",
    "firstparty_route_extensions",
    "register_firstparty_seams",
]
