"""Batch-25 coverage for lifespan phase implementations and related seams."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnigent.kernel.lifespan_phases import (
    AccountsAutoOpenPhase,
    AnyioThreadLimiterPhase,
    DefaultAgentsPhase,
    ExtensionBackgroundTasksPhase,
    HarnessProcessManagerPhase,
    LifespanContext,
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
)


def _lifespan_ctx(**overrides: object) -> LifespanContext:
    """Build a :class:`LifespanContext` with MagicMock wiring."""
    app = MagicMock()
    app.state = SimpleNamespace()
    defaults = {
        "app": app,
        "agent_store": MagicMock(),
        "artifact_store": MagicMock(),
        "agent_cache": MagicMock(),
        "conversation_store": MagicMock(),
        "runner_router": AsyncMock(),
        "tunnel_registry": MagicMock(),
        "mcp_pool": AsyncMock(),
        "server_metrics": MagicMock(),
        "server_metrics_otel": MagicMock(),
        "bootstrap_result": None,
        "policy_modules": ["omnigent.policies.builtins.github"],
    }
    defaults.update(overrides)
    return LifespanContext(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_anyio_thread_limiter_phase_bumps_tokens() -> None:
    from anyio import to_thread as _to_thread

    before = _to_thread.current_default_thread_limiter().total_tokens
    await AnyioThreadLimiterPhase().startup(_lifespan_ctx())
    assert _to_thread.current_default_thread_limiter().total_tokens == 200
    _to_thread.current_default_thread_limiter().total_tokens = before


@pytest.mark.asyncio
async def test_log_level_phase_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import logging

    monkeypatch.setenv("OMNIGENT_LOG_LEVEL", "DEBUG")
    await LogLevelPhase().startup(_lifespan_ctx())
    assert logging.getLogger("omnigent").level == logging.DEBUG


@pytest.mark.asyncio
async def test_harness_process_manager_phase_startup_and_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pm = AsyncMock()
    pm.start = AsyncMock()
    pm.shutdown = AsyncMock()
    monkeypatch.setattr(
        "omnigent.runtime.harnesses.process_manager.HarnessProcessManager",
        lambda: pm,
    )
    set_calls: list[object | None] = []
    monkeypatch.setattr(
        "omnigent.runtime.set_harness_process_manager",
        lambda value: set_calls.append(value),
    )
    ctx = _lifespan_ctx()
    phase = HarnessProcessManagerPhase()
    await phase.startup(ctx)
    assert ctx.app.state.harness_process_manager is pm
    assert set_calls == [pm]
    assert ctx.state["harness_pm"] is pm
    await phase.shutdown(ctx)
    assert set_calls == [pm, None]
    pm.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_runner_router_phase_startup_and_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = AsyncMock()
    router.aclose = AsyncMock()
    set_calls: list[object | None] = []
    monkeypatch.setattr(
        "omnigent.runtime.set_runner_router",
        lambda value: set_calls.append(value),
    )
    ctx = _lifespan_ctx(runner_router=router)
    phase = RunnerRouterPhase()
    await phase.startup(ctx)
    await phase.shutdown(ctx)
    assert set_calls == [router, None]
    router.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_subagent_block_notifier_phase_installs_and_uninstalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uninstall = MagicMock()
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.configure_subagent_block_notifier",
        lambda store, router: uninstall,
    )
    ctx = _lifespan_ctx()
    phase = SubagentBlockNotifierPhase()
    await phase.startup(ctx)
    assert ctx.state["uninstall_subagent_block_notifier"] is uninstall
    await phase.shutdown(ctx)
    uninstall.assert_called_once()


@pytest.mark.asyncio
async def test_resource_registry_phase_sets_runtime_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_calls: list[object | None] = []
    monkeypatch.setattr(
        "omnigent.runtime.get_terminal_registry",
        lambda: MagicMock(),
    )
    monkeypatch.setattr(
        "omnigent.runtime.set_resource_registry",
        lambda value: set_calls.append(value),
    )
    phase = ResourceRegistryPhase()
    await phase.startup(_lifespan_ctx())
    await phase.shutdown(_lifespan_ctx())
    assert len(set_calls) == 2
    assert set_calls[0] is not None
    assert set_calls[1] is None


@pytest.mark.asyncio
async def test_runner_ws_factory_phase_installs_tunnel_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = MagicMock()
    set_calls: list[object | None] = []
    monkeypatch.setattr(
        "omnigent.server._runner_ws_tunnel.make_tunnel_ws_factory",
        lambda router, registry: factory,
    )
    monkeypatch.setattr(
        "omnigent.runtime.set_runner_ws_factory",
        lambda value: set_calls.append(value),
    )
    phase = RunnerWsFactoryPhase()
    await phase.startup(_lifespan_ctx())
    await phase.shutdown(_lifespan_ctx())
    assert set_calls == [factory, None]


@pytest.mark.asyncio
async def test_default_agents_phase_delegates_to_app_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[tuple[object, object, object]] = []

    def _ensure(agent_store, artifact_store, agent_cache) -> None:
        called.append((agent_store, artifact_store, agent_cache))

    monkeypatch.setattr("omnigent.server.app._ensure_default_agents", _ensure)
    ctx = _lifespan_ctx()
    await DefaultAgentsPhase().startup(ctx)
    assert called == [(ctx.agent_store, ctx.artifact_store, ctx.agent_cache)]


@pytest.mark.asyncio
async def test_policy_registry_phase_loads_extra_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[list[str] | None] = []

    def _load(*, extra_modules: list[str] | None) -> None:
        captured.append(extra_modules)

    monkeypatch.setattr("omnigent.policies.registry.load_registry", _load)
    ctx = _lifespan_ctx(policy_modules=["custom.policy"])
    await PolicyRegistryPhase().startup(ctx)
    assert captured == [["custom.policy"]]


@pytest.mark.asyncio
async def test_accounts_auto_open_phase_opens_browser_when_bootstrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        "omnigent.server.auth.env_var_is_truthy",
        lambda _name, default=True: True,
    )
    monkeypatch.setattr(
        "webbrowser.open",
        lambda url: opened.append(url) or True,
    )
    bootstrap = SimpleNamespace(open_url="http://127.0.0.1:8000/setup", needs_setup=True)
    await AccountsAutoOpenPhase().startup(_lifespan_ctx(bootstrap_result=bootstrap))
    assert opened == ["http://127.0.0.1:8000/setup"]


@pytest.mark.asyncio
async def test_accounts_auto_open_phase_logs_browser_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.server.auth.env_var_is_truthy",
        lambda _name, default=True: True,
    )

    def _boom(_url: str) -> bool:
        raise OSError("no desktop")

    monkeypatch.setattr("webbrowser.open", _boom)
    bootstrap = SimpleNamespace(open_url="http://127.0.0.1:8000/setup", needs_setup=True)
    await AccountsAutoOpenPhase().startup(_lifespan_ctx(bootstrap_result=bootstrap))


@pytest.mark.asyncio
async def test_metrics_publish_phase_cancels_background_task() -> None:
    ctx = _lifespan_ctx()

    async def _noop_publish(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(3600)

    with patch(
        "omnigent.server.performance_metrics.publish_server_metrics_periodically",
        side_effect=_noop_publish,
    ):
        phase = MetricsPublishPhase()
        await phase.startup(ctx)
        task = ctx.state["metrics_publish_task"]
        assert isinstance(task, asyncio.Task)
        await phase.shutdown(ctx)
        assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_memory_maintenance_phase_cancels_background_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _lifespan_ctx()

    async def _noop_loop() -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(
        "omnigent.runtime.memory_maintenance.memory_maintenance_loop",
        _noop_loop,
    )
    phase = MemoryMaintenancePhase()
    await phase.startup(ctx)
    task = ctx.state["memory_maintenance_task"]
    await phase.shutdown(ctx)
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_extension_background_tasks_phase_cancels_all_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _factory() -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(
        "omnigent.kernel.extensions.extension_background_factories",
        lambda: [_factory],
    )
    ctx = _lifespan_ctx()
    phase = ExtensionBackgroundTasksPhase()
    await phase.startup(ctx)
    tasks = ctx.state["ext_bg_tasks"]
    assert len(tasks) == 1
    await phase.shutdown(ctx)
    assert tasks[0].cancelled() or tasks[0].done()


@pytest.mark.asyncio
async def test_teardown_only_phases_startup_is_noop() -> None:
    """Phases that only contribute teardown leave ``startup`` as a no-op."""
    ctx = _lifespan_ctx()
    assert await ManagedLaunchCancelPhase().startup(ctx) is None
    assert await TerminalRegistryPhase().startup(ctx) is None
    assert await McpPoolPhase().startup(ctx) is None


@pytest.mark.asyncio
async def test_managed_launch_cancel_phase_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancel = AsyncMock()
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.cancel_managed_launch_tasks",
        cancel,
    )
    await ManagedLaunchCancelPhase().shutdown(_lifespan_ctx())
    cancel.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_registry_phase_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = AsyncMock()
    registry.shutdown = AsyncMock()
    monkeypatch.setattr(
        "omnigent.runtime.get_terminal_registry",
        lambda: registry,
    )
    await TerminalRegistryPhase().shutdown(_lifespan_ctx())
    registry.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_pool_phase_shutdown() -> None:
    pool = AsyncMock()
    pool.shutdown_all = AsyncMock()
    ctx = _lifespan_ctx(mcp_pool=pool)
    await McpPoolPhase().shutdown(ctx)
    pool.shutdown_all.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_phase_default_shutdown_is_noop() -> None:
    class _NoShutdownOverride(LifespanPhase):
        name = "noop"

        async def startup(self, ctx: LifespanContext) -> None:
            return None

    assert await _NoShutdownOverride().shutdown(_lifespan_ctx()) is None


def test_build_default_lifespan_phases_returns_all_concrete_phases() -> None:
    names = {phase.name for phase in build_default_lifespan_phases()}
    assert "harness_process_manager" in names
    assert "mcp_pool" in names
    assert len(names) == len(build_default_lifespan_phases())