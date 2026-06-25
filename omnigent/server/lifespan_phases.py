"""Strangler re-export shim — moved to ``omnigent.kernel.lifespan_phases`` (BDP-2515).

The lifecycle engine (``LifespanPhase`` ABC, ``LifespanOrchestrator``,
``topological_order``, ``LifespanContext``, ``LifespanCycleError``) and its default
concrete-phase set relocated into the kernel package. This shim keeps every
existing ``from omnigent.server.lifespan_phases import ...`` import working until
call sites migrate to the canonical kernel path (BDP-2516). Same objects, no copies.
"""

from __future__ import annotations

from omnigent.kernel.lifespan_phases import *  # noqa: F401,F403
from omnigent.kernel.lifespan_phases import (  # noqa: F401  explicit public re-exports
    AccountsAutoOpenPhase,
    AnyioThreadLimiterPhase,
    DefaultAgentsPhase,
    ExtensionBackgroundTasksPhase,
    HarnessProcessManagerPhase,
    LifespanContext,
    LifespanCycleError,
    LifespanOrchestrator,
    LifespanPhase,
    LogLevelPhase,
    ManagedLaunchCancelPhase,
    McpPoolPhase,
    MemoryMaintenancePhase,
    MetricsPublishPhase,
    PolicyRegistryPhase,
    ResourceRegistryPhase,
    RunnerRouterPhase,
    RunnerWsFactoryPhase,
    SubagentBlockNotifierPhase,
    TerminalRegistryPhase,
    build_default_lifespan_phases,
    topological_order,
)

__all__ = [
    "AccountsAutoOpenPhase",
    "AnyioThreadLimiterPhase",
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
