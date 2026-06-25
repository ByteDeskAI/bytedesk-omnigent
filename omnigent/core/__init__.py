"""First-party ("core") plugin assembly — the second tier of the three-tier
microkernel picture (Section 10 of ``docs/EXTENSION_FRAMEWORK_ANALYSIS.md``).

The kernel (``omnigent.kernel.extensions``, ``omnigent.kernel.pluggable``) is domain-free; the
third tier is out-of-core / third-party extensions discovered via entry-points.
This package is the *middle* tier: the set of first-party plugins that ship in
the repo and dogfood the very same ``OmnigentExtension`` Protocol + seam
machinery a third-party extension uses.

Today these first-party capabilities are wired inline by ``create_app`` /
``_lifespan``. :func:`default_extensions` is the additive, flag-guarded path
(``OMNIGENT_USE_FIRSTPARTY_PLUGINS``) that lets the composition root install the
same capabilities *as extensions* — through ``install_extensions`` and the seam
registries — so the inline wiring can eventually be retired without changing the
contract. The flag is OFF by default; when unset, nothing here runs and boot is
byte-for-byte unchanged.

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
_FIRSTPARTY_PLUGINS: tuple[tuple[str, str], ...] = (
    # db/stores
    ("omnigent.stores._plugin", "StoresPlugin"),
    # identity
    ("omnigent.identity._plugin", "IdentityPlugin"),
    # coordination
    ("omnigent.coordination._plugin", "CoordinationExtension"),
    # harnesses (requires omnigent.stores)
    ("omnigent.runtime.harnesses._plugin", "HarnessesPlugin"),
    # tools
    ("omnigent.tools.builtins._plugin", "BuiltinToolsExtension"),
    # policies
    ("omnigent.policies._plugin", "PoliciesPlugin"),
    # spec
    ("omnigent.spec._plugin", "SpecPlugin"),
    # routes (install_extensions → routers())
    ("omnigent.server.routes._plugin", "RoutesPlugin"),
    # secrets
    ("omnigent.onboarding._plugin_secrets", "SecretsPlugin"),
    # metrics + memory_maintenance (background_tasks)
    ("omnigent.server._plugin_metrics", "MetricsExtension"),
    ("omnigent.runtime._plugin_memory_maintenance", "MemoryMaintenanceExtension"),
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

    Note: several seam accessors build a *fresh* registry per call (Section: the
    manifest's per-call registries). Registering into those is harmless but
    transient; the singleton-backed seams (``harness``, ``spec_source``) persist.
    This matches exactly the behaviour ``discover_all_extensions`` already has
    for those same seams, so it introduces no new asymmetry.

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


__all__ = [
    "default_extensions",
    "register_firstparty_seams",
    "firstparty_background_factories",
]
