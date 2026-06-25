"""Composition-root DI container for the omnigent server (BDP-2368).

The architectural capstone of the core-refactor spine. Today
:func:`omnigent.server.create_app` constructs its process-singleton services
inline — the tunnel registry, runner router, host registry, runner-exit
reports, MCP proxy pool, the two server-metrics publishers, and the managed-
launch tracker — then scatters them across ``app.state.<name> = ...`` writes
and route-factory arguments. BDP-2327 introduced :class:`ServiceRegistry` as a
typed *record* of the wired instances; this module goes one step further and
makes the container the **producer** of those instances with real lifetimes.

:class:`Core` is a ``dependency-injector`` :class:`DeclarativeContainer`. It is
introduced behind ``OMNIGENT_USE_DI_CONTAINER`` (default OFF). When the flag is
OFF, ``create_app`` builds every singleton inline exactly as it does today and
this module is never imported — the running server is byte-identical. When the
flag is ON, ``create_app`` resolves the *same* singletons from the container;
because each provider is a ``Singleton`` resolved once per built app, the
resulting ``app.state`` holds the identical object graph (same shapes, same
wiring) — only the construction site moves.

Lifetimes
---------
* **Singleton** — the process-singleton services above. ``providers.Singleton``
  memoizes the first build for the lifetime of the container, so a built app
  sees exactly one of each (matching the inline ``X()`` calls today).
* **Scoped (request-bound)** — request-lifetime dependencies (the per-request
  MCP manager, the tool-execution context) are *not* constructed by
  ``create_app`` today; they are created per request inside route handlers. The
  scoped seam here is therefore a deliberately minimal stub: a
  :func:`request_scope` context manager that resets ``providers.Resource`` /
  ``ContextLocalSingleton`` providers per request. No request-bound provider is
  wired yet (none would change behavior), so full request-scope is tracked as a
  follow-up; the must-have — the singleton composition root with clean
  startup/shutdown — is complete.
* **Transient** — not needed by the current composition root; ``providers.Factory``
  is available for future per-resolution objects.

Startup hook
------------
:meth:`Core.run_startup_discovery` calls the EXISTING
:func:`omnigent.kernel.pluggable.manifest.discover_all_extensions` — discovery is not
duplicated here. ``create_app`` already invokes that function from its lifespan;
when the DI path is active the call is simply routed through the container so
the container owns the one composition-root startup concern.

This module imports ``dependency_injector`` (a Cython C-extension) and the
server service modules; it is therefore a **server-only** import, gated at the
``create_app`` call site, and never reached on the runner hot path.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from dependency_injector import containers, providers

from omnigent.runner.routing import RunnerRouter
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.server.host_registry import HostRegistry, RunnerExitReports
from omnigent.server.managed_hosts import ManagedLaunchTracker
from omnigent.server.mcp_pool import ServerMcpPool
from omnigent.server.performance_metrics import (
    ServerMetricsOtelPublisher,
    ServerPerformanceMetrics,
)


class Core(containers.DeclarativeContainer):
    """The omnigent server composition root.

    Wires the process-singleton services that :func:`create_app` builds
    inline. The single configuration input is ``conversation_store`` — the
    one constructor dependency among these services (``RunnerRouter`` needs
    it) — provided via :attr:`config` so the caller supplies the store it
    already constructed without the container reaching into persistence.

    Resolve a built app's singletons with, e.g., ``container.runner_router()``;
    each ``Singleton`` provider memoizes its instance for the container's
    lifetime, so repeated calls return the same object (parity with the inline
    ``runner_router = RunnerRouter(...)`` today).
    """

    # Caller-supplied dependencies. ``conversation_store`` is set by
    # ``create_app`` from the store it already built; nothing else in the
    # composition root takes a constructor argument.
    config = providers.Configuration()

    # ── Singletons (process-lifetime services) ──────────────────────────
    # Order mirrors the inline construction in ``create_app`` so the object
    # graph is identical when resolved.

    tunnel_registry = providers.Singleton(TunnelRegistry)

    runner_router = providers.Singleton(
        RunnerRouter,
        registry=tunnel_registry,
        conversation_store=config.conversation_store,
    )

    host_registry = providers.Singleton(HostRegistry)

    runner_exit_reports = providers.Singleton(RunnerExitReports)

    mcp_pool = providers.Singleton(ServerMcpPool)

    server_metrics = providers.Singleton(ServerPerformanceMetrics)

    server_metrics_otel = providers.Singleton(ServerMetricsOtelPublisher)

    managed_launches = providers.Singleton(ManagedLaunchTracker)

    # ── Scoped seam (request-bound) ─────────────────────────────────────
    # Deliberately empty stub. No request-lifetime dependency is constructed
    # by ``create_app`` today (the per-request MCP manager / tool-exec context
    # are built inside route handlers), so wiring one here would not be
    # behavior-neutral. :func:`request_scope` provides the reset mechanism a
    # future ``providers.Resource`` / ``ContextLocalSingleton`` request-bound
    # provider would hook into; see the module docstring "Scoped" note.

    def run_startup_discovery(self) -> None:
        """Run the composition-root startup hook: extension discovery.

        Delegates to the EXISTING
        :func:`omnigent.kernel.pluggable.manifest.discover_all_extensions` — the one
        place discovery is triggered — rather than duplicating it. Called from
        ``create_app``'s lifespan when the DI path is active.
        """
        from omnigent.kernel.pluggable.manifest import discover_all_extensions

        discover_all_extensions()


@contextmanager
def request_scope(container: Core) -> Iterator[Core]:
    """Enter a per-request scope for *container* (scoped-lifetime stub).

    Resets the container's request-bound (``Resource`` /
    ``ContextLocalSingleton``) providers on exit so the next request gets a
    fresh instance. No such provider is wired yet — full request-scope is a
    tracked follow-up — so this currently no-ops the reset; it exists so the
    scoped seam has a single, greppable entry point for when a request-bound
    dependency is migrated into the container.

    :param container: The :class:`Core` container for the built app.
    :yields: The same container, scoped to the request.
    """
    try:
        yield container
    finally:
        # ``reset_singletons`` would clobber the process singletons, so a
        # real implementation resets only the registered request-scoped
        # providers. None are wired yet, so nothing to reset.
        pass


def build_core_container(conversation_store: Any) -> Core:
    """Build and configure the :class:`Core` container for one app.

    :param conversation_store: The conversation store ``create_app`` already
        constructed; the container injects it into ``RunnerRouter`` rather than
        building its own (the store is the only constructor dependency among the
        composition-root singletons).
    :returns: A configured :class:`Core` container whose singleton providers
        resolve the same object graph ``create_app`` builds inline.
    """
    container = Core()
    container.config.conversation_store.from_value(conversation_store)
    return container


__all__ = ["Core", "build_core_container", "request_scope"]
