"""Lifespan phases — a depends_on DAG over the server startup/shutdown steps.

Part of the omnigent core-refactor spine (BDP-2327, Phase 3). This module
models server startup/shutdown as a dependency DAG: each step (anyio
thread-limiter bump, log level, harness process manager, runner router,
subagent block notifier, resource registry, runner WS factory, default-agent
seed, policy registry, accounts auto-open, the metrics / memory-maintenance /
extension background tasks) is a :class:`LifespanPhase` (``startup`` /
``shutdown`` + an explicit ``depends_on`` list). :class:`LifespanOrchestrator`
topologically sorts the phases, runs ``startup`` in dependency order, and runs
``shutdown`` in the exact reverse order, making the previously implicit step
ordering an explicit, declared dependency graph. A dependency cycle is a
wiring bug, so the orchestrator **fails loudly** (raises
:class:`LifespanCycleError`) instead of guessing an order.

These phases supersede the prior inline server-lifespan path behind
``OMNIGENT_USE_LIFESPAN_PHASES`` (default OFF, strangler-fig): with the flag
off the legacy ``create_app`` lifespan stays the authoritative live path and
a running server behaves byte-identically to today; with the flag on,
``create_app`` builds an equivalent lifespan from these phases. The phases
mirror the legacy startup/shutdown body exactly — same imports, same calls,
same effective order.

Phases read and write a shared :class:`LifespanContext`: the immutable wiring
captured by ``create_app`` (stores, the runner router, the tunnel registry,
the MCP pool, the metrics trackers, …) plus a mutable ``state`` dict where a
startup step stashes the artifacts its own shutdown step needs (the started
harness process manager, the notifier-uninstall callback, the background
tasks). This keeps each phase self-contained — its shutdown undoes only what
its startup did — without re-deriving anything from ``app.state``.

This module imports omnigent runtime/server helpers (it has to, to wire the
steps), but it does **not** touch the spawn engine and adds no behavior of
its own beyond ordering.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

_logger = logging.getLogger(__name__)


@dataclass
class LifespanContext:
    """Shared context threaded through every :class:`LifespanPhase`.

    The ``app`` and the wiring fields are the immutable inputs captured by
    ``create_app`` (the same closure variables the monolithic ``_lifespan``
    read). ``state`` is the mutable scratch space: a startup step stores the
    artifacts it created (e.g. the started ``HarnessProcessManager``, the
    notifier-uninstall callback, a background ``asyncio.Task``) under a
    well-known key, and the matching shutdown step reads them back. Keeping
    these in ``state`` rather than on ``app.state`` mirrors the original
    closure-local variables and keeps the phases' teardown self-contained.

    :param app: The FastAPI app instance, for ``app.state.*`` writes that
        routes read (e.g. ``harness_process_manager``).
    :param agent_store: Store for agent metadata (default-agent seed).
    :param artifact_store: Store for agent bundles (default-agent seed).
    :param agent_cache: Cache for loaded agent specs (default-agent seed).
    :param conversation_store: Conversation persistence (notifier + resource
        registry liveness).
    :param runner_router: The shared runner router.
    :param runner_control_registry: Runner control registry retained for create_app
        context compatibility.
    :param mcp_pool: The AP-side MCP proxy pool, closed on shutdown.
    :param server_metrics: Process-local server metrics tracker.
    :param server_metrics_otel: OTel publisher for server metrics.
    :param bootstrap_result: Accounts first-run bootstrap result (or
        ``None``), gating the post-bind browser auto-open.
    :param policy_modules: Extra dotted module paths for the policy registry
        (or ``None``).
    """

    app: FastAPI
    agent_store: Any
    artifact_store: Any
    agent_cache: Any
    conversation_store: Any
    runner_router: Any
    runner_control_registry: Any
    mcp_pool: Any
    server_metrics: Any
    server_metrics_otel: Any
    bootstrap_result: Any
    policy_modules: list[str] | None
    state: dict[str, Any] = field(default_factory=dict)


class LifespanPhase(ABC):
    """One ordered unit of the server lifespan: a startup + its teardown.

    A phase declares the phases it depends on by name in
    :attr:`depends_on`; the orchestrator guarantees every dependency's
    ``startup`` has completed before this phase's ``startup`` runs, and that
    this phase's ``shutdown`` runs before any dependency's ``shutdown`` (i.e.
    teardown is the exact reverse of startup). A phase's ``shutdown`` should
    undo only what its own ``startup`` did, reading any artifacts it created
    back out of :attr:`LifespanContext.state`.
    """

    #: Stable identifier used as the dependency-graph key.
    name: str = ""

    #: Names of phases whose ``startup`` must complete before this one's.
    depends_on: tuple[str, ...] = ()

    @abstractmethod
    async def startup(self, ctx: LifespanContext) -> None:
        """Run this phase's startup step.

        :param ctx: The shared lifespan context.
        """

    async def shutdown(self, ctx: LifespanContext) -> None:
        """Run this phase's shutdown step (default: no-op).

        Phases with no teardown leave this as the inherited no-op.

        :param ctx: The shared lifespan context.
        """
        return None


class LifespanCycleError(RuntimeError):
    """Raised when the lifespan phases contain a dependency cycle.

    A cycle (or a ``depends_on`` naming an unknown phase) means there is no
    valid startup order, which is always a wiring bug. The orchestrator
    refuses to guess and raises this instead.
    """


def topological_order(phases: Iterable[LifespanPhase]) -> list[LifespanPhase]:
    """Return *phases* in dependency order (a topological sort).

    A phase appears after every phase named in its ``depends_on``. The sort
    is **registration-stable**: at each step it emits the earliest-registered
    phase whose dependencies are all already emitted, so an independent phase
    keeps its registration position and the order is reproducible across runs.
    That stability is what lets ``build_default_lifespan_phases`` register the
    phases in "reverse of the original ``finally``" order and rely on the
    reverse-topological teardown matching it exactly.

    :param phases: The phases to order.
    :returns: The phases in a valid startup order.
    :raises LifespanCycleError: If a ``depends_on`` names an unknown phase,
        or the dependency graph contains a cycle.
    """
    registration: list[LifespanPhase] = list(phases)
    by_name: dict[str, LifespanPhase] = {}
    for phase in registration:
        if phase.name in by_name:
            raise LifespanCycleError(f"duplicate lifespan phase name {phase.name!r}")
        by_name[phase.name] = phase

    for phase in registration:
        unknown = set(phase.depends_on) - by_name.keys()
        if unknown:
            raise LifespanCycleError(
                f"lifespan phase {phase.name!r} depends on unknown phase(s) "
                f"{sorted(unknown)!r}"
            )

    ordered: list[LifespanPhase] = []
    emitted: set[str] = set()
    # Repeatedly emit the earliest-registered phase whose deps are all
    # already emitted. This preserves registration order among independent
    # phases (unlike a FIFO ready-queue, which would re-append a freshly
    # unblocked phase to the tail and shuffle the order).
    while len(ordered) < len(registration):
        progressed = False
        for phase in registration:
            if phase.name in emitted:
                continue
            if all(dep in emitted for dep in phase.depends_on):
                ordered.append(phase)
                emitted.add(phase.name)
                progressed = True
                break
        if not progressed:
            unresolved = sorted(p.name for p in registration if p.name not in emitted)
            raise LifespanCycleError(
                f"lifespan phases contain a dependency cycle among {unresolved!r}"
            )
    return ordered


class LifespanOrchestrator:
    """Run a set of :class:`LifespanPhase` objects as a depends_on DAG.

    On :meth:`startup` the phases run in topological order; on
    :meth:`shutdown` they run in the exact reverse order. Shutdown is
    best-effort: every phase whose startup completed is torn down even if an
    earlier teardown raised (mirrors the single ``finally`` block in the
    original ``_lifespan``, which never short-circuits its cleanup). The
    topological order is computed once at construction so a cycle fails fast,
    before any startup side effect runs.

    :param phases: The phases to orchestrate.
    :raises LifespanCycleError: If the phases contain a dependency cycle.
    """

    def __init__(self, phases: Iterable[LifespanPhase]) -> None:
        """Order the phases up front so a cycle fails before startup.

        :param phases: The phases to orchestrate.
        """
        self._ordered = topological_order(phases)
        self._started: list[LifespanPhase] = []

    async def startup(self, ctx: LifespanContext) -> None:
        """Run every phase's ``startup`` in topological order.

        Tracks the phases that completed so :meth:`shutdown` only tears down
        what actually started. If a startup raises, the already-started
        phases are torn down (reverse order) before the error propagates, so
        a half-built app never leaks the resources it did acquire.

        :param ctx: The shared lifespan context.
        """
        try:
            for phase in self._ordered:
                await phase.startup(ctx)
                self._started.append(phase)
        except Exception:
            await self.shutdown(ctx)
            raise

    async def shutdown(self, ctx: LifespanContext) -> None:
        """Run ``shutdown`` for every started phase in reverse order.

        Each teardown failure is logged and swallowed so one failing phase
        cannot strand the rest — the same all-cleanup-runs guarantee the
        original monolithic ``finally`` block provided.

        :param ctx: The shared lifespan context.
        """
        while self._started:
            phase = self._started.pop()
            try:
                await phase.shutdown(ctx)
            except Exception:  # one phase's teardown must not block the rest
                _logger.exception(
                    "lifespan phase %r shutdown failed; continuing teardown",
                    phase.name,
                )


# --- concrete phases (mirror the monolithic _lifespan body 1:1) ------------


class AnyioThreadLimiterPhase(LifespanPhase):
    """Bump the AnyIO default thread limiter from 40 → 200 (no teardown)."""

    name = "anyio_thread_limiter"

    async def startup(self, ctx: LifespanContext) -> None:
        """Raise the shared thread limiter so sync routes don't starve.

        :param ctx: The shared lifespan context (unused).
        """
        from anyio import to_thread as _to_thread

        _to_thread.current_default_thread_limiter().total_tokens = 200


class LogLevelPhase(LifespanPhase):
    """Apply ``OMNIGENT_LOG_LEVEL`` to the omnigent namespace (no teardown)."""

    name = "log_level"

    async def startup(self, ctx: LifespanContext) -> None:
        """Set the omnigent logger level after uvicorn's dictConfig runs.

        :param ctx: The shared lifespan context (unused).
        """
        import os as _os

        _log_level_name = _os.environ.get("OMNIGENT_LOG_LEVEL", "INFO").upper()
        logging.getLogger("omnigent").setLevel(
            getattr(logging, _log_level_name, logging.INFO)
        )


class HarnessProcessManagerPhase(LifespanPhase):
    """Start/stop the harness process manager and publish it to runtime."""

    name = "harness_process_manager"

    async def startup(self, ctx: LifespanContext) -> None:
        """Construct + start the manager and stash it for routes/workflows.

        :param ctx: The shared lifespan context.
        """
        from omnigent.runtime import set_harness_process_manager
        from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

        harness_pm = HarnessProcessManager()
        await harness_pm.start()
        ctx.app.state.harness_process_manager = harness_pm
        set_harness_process_manager(harness_pm)
        ctx.state["harness_pm"] = harness_pm

    async def shutdown(self, ctx: LifespanContext) -> None:
        """Clear the runtime global and shut the manager down.

        :param ctx: The shared lifespan context.
        """
        from omnigent.runtime import set_harness_process_manager

        set_harness_process_manager(None)
        harness_pm = ctx.state.get("harness_pm")
        if harness_pm is not None:
            await harness_pm.shutdown()


class RunnerRouterPhase(LifespanPhase):
    """Publish the runner router to the runtime global and close it on exit."""

    name = "runner_router"

    async def startup(self, ctx: LifespanContext) -> None:
        """Install the shared runner router as the runtime global.

        :param ctx: The shared lifespan context.
        """
        from omnigent.runtime import set_runner_router

        set_runner_router(ctx.runner_router)

    async def shutdown(self, ctx: LifespanContext) -> None:
        """Clear the runtime global and close the router.

        :param ctx: The shared lifespan context.
        """
        from omnigent.runtime import set_runner_router

        set_runner_router(None)
        await ctx.runner_router.aclose()


class SubagentBlockNotifierPhase(LifespanPhase):
    """Install (and later uninstall) the sub-agent block notifier."""

    name = "subagent_block_notifier"
    depends_on = ("runner_router",)

    async def startup(self, ctx: LifespanContext) -> None:
        """Hook the parent-notify observer onto publish; stash its uninstall.

        :param ctx: The shared lifespan context.
        """
        from omnigent.server.routes.sessions import configure_subagent_block_notifier

        ctx.state["uninstall_subagent_block_notifier"] = configure_subagent_block_notifier(
            ctx.conversation_store,
            ctx.runner_router,
        )

    async def shutdown(self, ctx: LifespanContext) -> None:
        """Uninstall the observer so a fresh app doesn't inherit it.

        :param ctx: The shared lifespan context.
        """
        uninstall = ctx.state.get("uninstall_subagent_block_notifier")
        if uninstall is not None:
            uninstall()


class ResourceRegistryPhase(LifespanPhase):
    """Build the session resource registry and clear it on shutdown."""

    name = "resource_registry"

    async def startup(self, ctx: LifespanContext) -> None:
        """Construct + install the session resource registry.

        :param ctx: The shared lifespan context.
        """
        from omnigent.runner.resource_registry import SessionResourceRegistry
        from omnigent.runtime import get_terminal_registry, set_resource_registry

        resource_reg = SessionResourceRegistry(
            terminal_registry=get_terminal_registry(),
        )
        set_resource_registry(resource_reg)

    async def shutdown(self, ctx: LifespanContext) -> None:
        """Clear the resource registry runtime global.

        :param ctx: The shared lifespan context (unused).
        """
        from omnigent.runtime import set_resource_registry

        set_resource_registry(None)


class RunnerWsFactoryPhase(LifespanPhase):
    """Ensure no legacy runner WS factory is installed."""

    name = "runner_ws_factory"
    depends_on = ("runner_router",)

    async def startup(self, ctx: LifespanContext) -> None:
        """Clear the runner WS factory.

        :param ctx: The shared lifespan context.
        """
        from omnigent.runtime import set_runner_ws_factory

        del ctx
        set_runner_ws_factory(None)

    async def shutdown(self, ctx: LifespanContext) -> None:
        """Clear the WS factory runtime global.

        :param ctx: The shared lifespan context (unused).
        """
        from omnigent.runtime import set_runner_ws_factory

        set_runner_ws_factory(None)


class DefaultAgentsPhase(LifespanPhase):
    """Seed the built-in default agents (no teardown)."""

    name = "default_agents"

    async def startup(self, ctx: LifespanContext) -> None:
        """Register/refresh the always-available built-in agents.

        :param ctx: The shared lifespan context.
        """
        from omnigent.server.app import _ensure_default_agents

        _ensure_default_agents(ctx.agent_store, ctx.artifact_store, ctx.agent_cache)


class PolicyRegistryPhase(LifespanPhase):
    """Populate the policy registry (builtins + configured modules)."""

    name = "policy_registry"

    async def startup(self, ctx: LifespanContext) -> None:
        """Load the policy registry so GET /v1/policy-registry serves it.

        :param ctx: The shared lifespan context.
        """
        from omnigent.policies.registry import load_registry

        load_registry(extra_modules=ctx.policy_modules)


class AccountsAutoOpenPhase(LifespanPhase):
    """Open the browser on an accounts first-run needs-setup boot (no teardown)."""

    name = "accounts_auto_open"

    async def startup(self, ctx: LifespanContext) -> None:
        """Auto-open the loopback URL when bootstrap asked for it.

        :param ctx: The shared lifespan context.
        """
        if ctx.bootstrap_result is not None and ctx.bootstrap_result.open_url:
            from omnigent.server.auth import env_var_is_truthy

            if env_var_is_truthy("OMNIGENT_ACCOUNTS_AUTO_OPEN", default=True):
                import webbrowser

                try:
                    webbrowser.open(ctx.bootstrap_result.open_url)
                except Exception as exc:  # noqa: BLE001
                    _logger.warning(
                        "accounts: auto-open browser failed (%s) — open the "
                        "server URL in a browser instead",
                        exc,
                    )


class MetricsPublishPhase(LifespanPhase):
    """Run (and cancel) the periodic server-metrics publish task."""

    name = "metrics_publish"

    async def startup(self, ctx: LifespanContext) -> None:
        """Spawn the periodic metrics-publish background task.

        :param ctx: The shared lifespan context.
        """
        from omnigent.server.performance_metrics import (
            publish_server_metrics_periodically,
        )

        ctx.state["metrics_publish_task"] = asyncio.create_task(
            publish_server_metrics_periodically(
                ctx.server_metrics,
                otel_publisher=ctx.server_metrics_otel,
            )
        )

    async def shutdown(self, ctx: LifespanContext) -> None:
        """Cancel and await the metrics-publish task.

        :param ctx: The shared lifespan context.
        """
        task = ctx.state.get("metrics_publish_task")
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


class MemoryMaintenancePhase(LifespanPhase):
    """Run (and cancel) the periodic agent-memory maintenance loop."""

    name = "memory_maintenance"

    async def startup(self, ctx: LifespanContext) -> None:
        """Spawn the memory-maintenance background loop (FU1, ADR-0132).

        :param ctx: The shared lifespan context.
        """
        from omnigent.runtime.memory_maintenance import memory_maintenance_loop

        ctx.state["memory_maintenance_task"] = asyncio.create_task(
            memory_maintenance_loop()
        )

    async def shutdown(self, ctx: LifespanContext) -> None:
        """Cancel and await the memory-maintenance task.

        :param ctx: The shared lifespan context.
        """
        task = ctx.state.get("memory_maintenance_task")
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


class CoordinationPhase(LifespanPhase):
    """Start the cross-replica coordination backplane; stop it on shutdown.

    BDP-2571: the monolithic ``_lifespan`` calls
    :func:`omnigent.coordination.lifecycle.start_coordination` (and stops it in
    its ``finally``), but the **deployed** phase lifespan
    (``OMNIGENT_USE_LIFESPAN_PHASES=1``) was missing it. Without the active
    backplane ``claim_resource`` / ``resolve_resource`` are no-ops, so BDP-2556
    cross-replica host control fails with "host is offline" on any server replica
    that does not own the host tunnel (the "runner didn't come online" symptom at
    2+ replicas). Self-contained: ``start_coordination`` resolves the backplane
    from ``OMNIGENT_NATS_URL`` + the inline coordination registry, so this phase
    has no ``depends_on``. It is ordered before
    :class:`ExtensionBackgroundTasksPhase` / :class:`DefaultAgentsPhase` so the
    backplane is live before any phase creates tasks/agents that use it.
    """

    name = "coordination"

    async def startup(self, ctx: LifespanContext) -> None:
        """Connect the active coordination backplane.

        :param ctx: The shared lifespan context (unused).
        """
        from omnigent.coordination.lifecycle import start_coordination

        await start_coordination()

    async def shutdown(self, ctx: LifespanContext) -> None:
        """Disconnect the coordination backplane.

        :param ctx: The shared lifespan context (unused).
        """
        from omnigent.coordination.lifecycle import stop_coordination

        await stop_coordination()


class ExtensionBackgroundTasksPhase(LifespanPhase):
    """Run (and cancel) the first-party extension background loops."""

    name = "extension_background_tasks"

    async def startup(self, ctx: LifespanContext) -> None:
        """Start every extension background loop via the extensions seam.

        BDP-2516: also starts the AUTHORITATIVE first-party background loops
        (``omnigent.metrics`` + ``omnigent.memory_maintenance``) through the SAME
        task path — unconditionally, no ``OMNIGENT_USE_FIRSTPARTY_PLUGINS`` flag.
        This replaces the former standalone ``MetricsPublishPhase`` /
        ``MemoryMaintenancePhase`` (now removed from
        :func:`build_default_lifespan_phases`), keeping the phase path 1:1 with
        the monolithic ``_lifespan``. The metrics loop is injected with the live
        ``ctx.server_metrics`` / ``ctx.server_metrics_otel`` for exact parity.

        :param ctx: The shared lifespan context.
        """
        from omnigent.core import (
            firstparty_background_factories,
            firstparty_background_task_extensions,
        )
        from omnigent.kernel.extensions import extension_background_factories

        tasks = [
            asyncio.create_task(factory())
            for factory in extension_background_factories()
        ]
        _bg_task_extensions = firstparty_background_task_extensions(
            server_metrics=ctx.server_metrics,
            server_metrics_otel=ctx.server_metrics_otel,
        )
        tasks.extend(
            asyncio.create_task(factory())
            for factory in firstparty_background_factories(_bg_task_extensions)
        )
        ctx.state["ext_bg_tasks"] = tasks

    async def shutdown(self, ctx: LifespanContext) -> None:
        """Cancel and await every extension background task.

        :param ctx: The shared lifespan context.
        """
        for task in ctx.state.get("ext_bg_tasks", []):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


class ManagedLaunchCancelPhase(LifespanPhase):
    """Cancel in-flight background managed-sandbox launches on shutdown only."""

    name = "managed_launch_cancel"

    async def startup(self, ctx: LifespanContext) -> None:
        """No startup work — this phase only contributes teardown.

        :param ctx: The shared lifespan context (unused).
        """
        return None

    async def shutdown(self, ctx: LifespanContext) -> None:
        """Stop pending managed-sandbox launch tasks.

        :param ctx: The shared lifespan context (unused).
        """
        from omnigent.server.routes.sessions import cancel_managed_launch_tasks

        await cancel_managed_launch_tasks()


class TerminalRegistryPhase(LifespanPhase):
    """Shut down all live tmux terminals on shutdown only."""

    name = "terminal_registry"

    async def startup(self, ctx: LifespanContext) -> None:
        """No startup work — this phase only contributes teardown.

        :param ctx: The shared lifespan context (unused).
        """
        return None

    async def shutdown(self, ctx: LifespanContext) -> None:
        """Close every live terminal in the registry.

        :param ctx: The shared lifespan context (unused).
        """
        from omnigent.runtime import get_terminal_registry

        await get_terminal_registry().shutdown()


class McpPoolPhase(LifespanPhase):
    """Close all AP-side MCP proxy connections on shutdown only."""

    name = "mcp_pool"

    async def startup(self, ctx: LifespanContext) -> None:
        """No startup work — this phase only contributes teardown.

        :param ctx: The shared lifespan context (unused).
        """
        return None

    async def shutdown(self, ctx: LifespanContext) -> None:
        """Shut down every AP-side MCP connection opened by the proxy.

        :param ctx: The shared lifespan context.
        """
        await ctx.mcp_pool.shutdown_all()


def build_default_lifespan_phases() -> list[LifespanPhase]:
    """Return the concrete phases mirroring the monolithic ``_lifespan``.

    The phases are registered in the order whose **reverse is the original
    ``_lifespan`` ``finally`` block** — so the orchestrator's reverse-of-
    startup teardown reproduces the hand-written shutdown sequence
    statement-for-statement (ext-bg [which since BDP-2516 also covers the
    metrics + memory-maintenance loops] → managed-launch → notifier →
    resource → ws-factory → router → harness → terminal → mcp-pool).
    ``depends_on`` then re-imposes the real startup data
    constraints on top of that order: the subagent notifier and the WS
    factory both require the runner router installed first, and the runner
    router requires the harness process manager. With those edges the
    topological startup order keeps every original startup dependency while
    its reverse stays byte-for-byte the original teardown order; the
    remaining reorderings are only among independent phases (their startup /
    shutdown steps touch disjoint state).

    The teardown-only phases (managed-launch cancel, terminal registry, MCP
    pool) carry no startup work and exist purely to slot their cleanup into
    the reverse order at the right point.

    :returns: The phases registered so reverse-topological teardown matches
        the original ``finally`` order.
    """
    # Listed reverse-of-finally so reverse-topo teardown == original finally.
    return [
        AnyioThreadLimiterPhase(),
        LogLevelPhase(),
        McpPoolPhase(),
        TerminalRegistryPhase(),
        HarnessProcessManagerPhase(),
        RunnerRouterPhase(),
        RunnerWsFactoryPhase(),
        ResourceRegistryPhase(),
        SubagentBlockNotifierPhase(),
        ManagedLaunchCancelPhase(),
        # BDP-2516: ExtensionBackgroundTasksPhase now also starts the
        # authoritative omnigent.metrics + omnigent.memory_maintenance loops, so
        # the standalone MetricsPublishPhase / MemoryMaintenancePhase are no
        # longer in the default DAG (they remain as classes for back-compat /
        # direct use but are unwired). Mirrors the monolithic _lifespan which now
        # folds those two loops into the single _ext_bg_tasks list.
        # BDP-2571: start the coordination backplane before the task/agent
        # phases that use it (mirrors start_coordination in the monolithic
        # _lifespan). Its reverse-order teardown lands right after
        # extension_background_tasks rather than first-in-finally as _lifespan
        # does — a best-effort shutdown ordering, not a correctness requirement.
        CoordinationPhase(),
        ExtensionBackgroundTasksPhase(),
        DefaultAgentsPhase(),
        PolicyRegistryPhase(),
        AccountsAutoOpenPhase(),
    ]


__all__ = [
    "AccountsAutoOpenPhase",
    "AnyioThreadLimiterPhase",
    "CoordinationPhase",
    "DefaultAgentsPhase",
    "ExtensionBackgroundTasksPhase",
    "HarnessProcessManagerPhase",
    "LifespanContext",
    "LifespanCycleError",
    "LifespanOrchestrator",
    "LifespanPhase",
    "LogLevelPhase",
    "ManagedLaunchCancelPhase",
    "McpPoolPhase",
    "MemoryMaintenancePhase",
    "MetricsPublishPhase",
    "PolicyRegistryPhase",
    "ResourceRegistryPhase",
    "RunnerRouterPhase",
    "RunnerWsFactoryPhase",
    "SubagentBlockNotifierPhase",
    "TerminalRegistryPhase",
    "build_default_lifespan_phases",
    "topological_order",
]
