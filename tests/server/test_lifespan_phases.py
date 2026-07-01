"""Tests for omnigent.kernel.lifespan_phases (BDP-2327, Phase 3)."""

from __future__ import annotations

import asyncio

import pytest

from omnigent.kernel.lifespan_phases import (
    LifespanContext,
    LifespanCycleError,
    LifespanOrchestrator,
    LifespanPhase,
    build_default_lifespan_phases,
    topological_order,
)


class _RecordingPhase(LifespanPhase):
    """A phase that appends ``(name, "up"|"down")`` to a shared log."""

    def __init__(
        self,
        name: str,
        depends_on: tuple[str, ...],
        log: list[tuple[str, str]],
    ) -> None:
        """Capture identity, dependencies, and the shared event log.

        :param name: This phase's graph key.
        :param depends_on: Names of phases that must start first.
        :param log: Shared list every phase appends its events to.
        """
        self.name = name
        self.depends_on = depends_on
        self._log = log

    async def startup(self, ctx: LifespanContext) -> None:
        """Record a startup event.

        :param ctx: The shared lifespan context (unused).
        """
        self._log.append((self.name, "up"))

    async def shutdown(self, ctx: LifespanContext) -> None:
        """Record a shutdown event.

        :param ctx: The shared lifespan context (unused).
        """
        self._log.append((self.name, "down"))


def _ctx(*, di_container: object | None = None) -> LifespanContext:
    """Return a context with placeholder wiring (phases under test ignore it)."""
    return LifespanContext(
        app=None,
        agent_store=None,
        artifact_store=None,
        agent_cache=None,
        conversation_store=None,
        runner_router=None,
        runner_control_registry=None,
        mcp_pool=None,
        server_metrics=None,
        server_metrics_otel=None,
        bootstrap_result=None,
        policy_modules=None,
        di_container=di_container,
    )


def test_topological_order_respects_depends_on() -> None:
    """A phase is ordered after every phase it depends on."""
    log: list[tuple[str, str]] = []
    phases = [
        _RecordingPhase("c", ("b",), log),
        _RecordingPhase("a", (), log),
        _RecordingPhase("b", ("a",), log),
    ]
    ordered = [p.name for p in topological_order(phases)]
    assert ordered.index("a") < ordered.index("b") < ordered.index("c")


def test_topological_order_is_deterministic_on_ties() -> None:
    """Independent phases keep their registration order (stable sort)."""
    log: list[tuple[str, str]] = []
    phases = [
        _RecordingPhase("x", (), log),
        _RecordingPhase("y", (), log),
        _RecordingPhase("z", (), log),
    ]
    assert [p.name for p in topological_order(phases)] == ["x", "y", "z"]


def test_cycle_fails_loudly() -> None:
    """A dependency cycle raises LifespanCycleError, not a guessed order."""
    log: list[tuple[str, str]] = []
    phases = [
        _RecordingPhase("a", ("b",), log),
        _RecordingPhase("b", ("a",), log),
    ]
    with pytest.raises(LifespanCycleError, match="cycle"):
        topological_order(phases)


def test_unknown_dependency_fails_loudly() -> None:
    """A depends_on naming a missing phase raises LifespanCycleError."""
    log: list[tuple[str, str]] = []
    phases = [_RecordingPhase("a", ("missing",), log)]
    with pytest.raises(LifespanCycleError, match="unknown"):
        topological_order(phases)


def test_duplicate_name_fails_loudly() -> None:
    """Two phases sharing a name raise LifespanCycleError."""
    log: list[tuple[str, str]] = []
    phases = [
        _RecordingPhase("dup", (), log),
        _RecordingPhase("dup", (), log),
    ]
    with pytest.raises(LifespanCycleError, match="duplicate"):
        topological_order(phases)


@pytest.mark.asyncio
async def test_orchestrator_shutdown_is_reverse_of_startup() -> None:
    """Shutdown runs every started phase in the exact reverse order."""
    log: list[tuple[str, str]] = []
    phases = [
        _RecordingPhase("a", (), log),
        _RecordingPhase("b", ("a",), log),
        _RecordingPhase("c", ("b",), log),
    ]
    orchestrator = LifespanOrchestrator(phases)
    ctx = _ctx()
    await orchestrator.startup(ctx)
    await orchestrator.shutdown(ctx)
    assert log == [
        ("a", "up"),
        ("b", "up"),
        ("c", "up"),
        ("c", "down"),
        ("b", "down"),
        ("a", "down"),
    ]


@pytest.mark.asyncio
async def test_orchestrator_only_tears_down_started_phases() -> None:
    """A startup failure tears down only the phases that already started."""
    log: list[tuple[str, str]] = []

    class _Boom(_RecordingPhase):
        async def startup(self, ctx: LifespanContext) -> None:
            self._log.append((self.name, "up"))
            raise RuntimeError("boom")

    phases = [
        _RecordingPhase("a", (), log),
        _Boom("b", ("a",), log),
        _RecordingPhase("c", ("b",), log),
    ]
    orchestrator = LifespanOrchestrator(phases)
    with pytest.raises(RuntimeError, match="boom"):
        await orchestrator.startup(_ctx())
    # a + b started (b mid-startup); both are torn down, c never ran.
    assert log == [("a", "up"), ("b", "up"), ("a", "down")]


@pytest.mark.asyncio
async def test_orchestrator_continues_teardown_after_a_phase_raises() -> None:
    """One phase's shutdown failure does not strand the remaining teardowns."""
    log: list[tuple[str, str]] = []

    class _ShutdownBoom(_RecordingPhase):
        async def shutdown(self, ctx: LifespanContext) -> None:
            self._log.append((self.name, "down"))
            raise RuntimeError("teardown-boom")

    phases = [
        _RecordingPhase("a", (), log),
        _ShutdownBoom("b", ("a",), log),
    ]
    orchestrator = LifespanOrchestrator(phases)
    ctx = _ctx()
    await orchestrator.startup(ctx)
    await orchestrator.shutdown(ctx)  # must not raise
    assert log == [("a", "up"), ("b", "up"), ("b", "down"), ("a", "down")]


def test_default_phases_topo_sort_and_reverse_matches_finally_order() -> None:
    """The default phases reverse-teardown in the original _lifespan order.

    This pins the faithful-refactor contract: the non-no-op phases, taken in
    reverse topological order, must equal the hand-written ``finally`` block's
    teardown sequence in ``create_app._lifespan``.
    """
    ordered = topological_order(build_default_lifespan_phases())
    # Real startup data deps must hold.
    names = [p.name for p in ordered]
    assert names.index("runner_router") < names.index("runner_ws_factory")
    assert names.index("runner_router") < names.index("subagent_block_notifier")
    assert names.index("extension_discovery") < names.index("builtin_tool_registration")
    assert names.index("builtin_tool_registration") < names.index("firstparty_seams")
    assert names.index("firstparty_seams") < names.index("coordination")
    assert names.index("coordination") < names.index("extension_background_tasks")
    assert names.index("coordination") < names.index("default_agents")

    teardown_order = [p.name for p in reversed(ordered)]
    # BDP-2516: the standalone metrics_publish / memory_maintenance phases were
    # folded into extension_background_tasks (those loops are now authoritative
    # first-party plugins started through the same seam path), mirroring the
    # monolithic _lifespan which now folds both into the single _ext_bg_tasks
    # list. They are therefore absent from the default DAG's teardown order.
    expected_finally = [
        "extension_background_tasks",
        # BDP-2571: coordination starts before extension_background_tasks (so the
        # backplane is live before task/agent phases use it), so reverse-order
        # teardown places it right after. _lifespan stops coordination first in
        # its finally; that ordering is best-effort, not a correctness contract.
        "coordination",
        "managed_launch_cancel",
        "subagent_block_notifier",
        "resource_registry",
        "runner_ws_factory",
        "runner_router",
        "harness_process_manager",
        "terminal_registry",
        "mcp_pool",
    ]
    # Strip the no-teardown phases; what remains must be the finally order.
    no_teardown = {
        "anyio_thread_limiter",
        "log_level",
        "default_agents",
        "extension_discovery",
        "builtin_tool_registration",
        "firstparty_seams",
        "policy_registry",
        "accounts_auto_open",
    }
    assert [n for n in teardown_order if n not in no_teardown] == expected_finally


def test_default_phases_include_coordination() -> None:
    """BDP-2571: the deployed phase lifespan must start the coordination
    backplane. Without a coordination phase, ``start_coordination`` is never
    called (it only runs in the monolithic ``_lifespan``), the NATS backplane
    stays inactive, and BDP-2556 cross-replica host control fails ("host is
    offline" / "runner didn't come online" at 2+ server replicas).
    """
    assert "coordination" in {p.name for p in build_default_lifespan_phases()}
    # Must start before the phases that create backplane-using tasks/agents.
    ordered = [p.name for p in topological_order(build_default_lifespan_phases())]
    assert ordered.index("coordination") < ordered.index("extension_background_tasks")
    assert ordered.index("coordination") < ordered.index("default_agents")


def test_default_phases_include_extension_startup_cutover() -> None:
    """Always-on lifespan must include the formerly monolithic extension setup."""
    ordered = [p.name for p in topological_order(build_default_lifespan_phases())]

    for phase in (
        "extension_discovery",
        "builtin_tool_registration",
        "firstparty_seams",
    ):
        assert phase in ordered

    assert ordered.index("extension_discovery") < ordered.index("builtin_tool_registration")
    assert ordered.index("builtin_tool_registration") < ordered.index("firstparty_seams")
    assert ordered.index("firstparty_seams") < ordered.index("coordination")


@pytest.mark.asyncio
async def test_extension_discovery_phase_prefers_di_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup discovery is owned by the DI composition root when available."""
    from omnigent.kernel.lifespan_phases import ExtensionDiscoveryPhase

    calls: list[str] = []

    class _Container:
        def run_startup_discovery(self) -> None:
            calls.append("container")

    monkeypatch.setattr(
        "omnigent.kernel.pluggable.manifest.discover_all_extensions",
        lambda: calls.append("fallback"),
    )

    await ExtensionDiscoveryPhase().startup(_ctx(di_container=_Container()))

    assert calls == ["container"]


@pytest.mark.asyncio
async def test_extension_discovery_phase_falls_back_without_di_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare test contexts still run discovery through the canonical helper."""
    from omnigent.kernel.lifespan_phases import ExtensionDiscoveryPhase

    calls: list[str] = []
    monkeypatch.setattr(
        "omnigent.kernel.pluggable.manifest.discover_all_extensions",
        lambda: calls.append("fallback"),
    )

    await ExtensionDiscoveryPhase().startup(_ctx())

    assert calls == ["fallback"]


@pytest.mark.asyncio
async def test_builtin_tool_registration_phase_registers_extension_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The phase path merges extension builtin tools before first-party seams."""
    from omnigent.kernel.lifespan_phases import BuiltinToolRegistrationPhase

    calls: list[str] = []
    monkeypatch.setattr(
        "omnigent.tools.builtins.register_extension_tools",
        lambda: calls.append("tools"),
    )

    await BuiltinToolRegistrationPhase().startup(_ctx())

    assert calls == ["tools"]


@pytest.mark.asyncio
async def test_firstparty_seams_phase_registers_and_stores_extensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First-party seam registration feeds later first-party background startup."""
    from omnigent.kernel.lifespan_phases import FirstpartySeamsPhase

    extensions = [object(), object()]
    calls: list[object] = []

    monkeypatch.setattr("omnigent.core.default_extensions", lambda: extensions)
    monkeypatch.setattr(
        "omnigent.core.register_firstparty_seams",
        lambda ext: calls.append(ext),
    )

    ctx = _ctx()
    await FirstpartySeamsPhase().startup(ctx)

    assert calls == [extensions]
    assert ctx.state["firstparty_extensions"] == extensions


@pytest.mark.asyncio
async def test_extension_background_tasks_include_firstparty_extension_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Background startup uses first-party extensions captured by the seam phase."""
    from omnigent.kernel.lifespan_phases import ExtensionBackgroundTasksPhase

    calls: list[str | tuple[str, tuple[object, ...]]] = []
    firstparty_extension = object()

    async def _firstparty_loop() -> None:
        calls.append("started")
        await asyncio.Event().wait()

    def _factories(extensions: list[object]) -> list[object]:
        calls.append(("factories", tuple(extensions)))
        if extensions == [firstparty_extension]:
            return [_firstparty_loop]
        return []

    monkeypatch.setattr(
        "omnigent.kernel.extensions.extension_background_factories",
        list,
    )
    monkeypatch.setattr(
        "omnigent.core.firstparty_background_task_extensions",
        lambda **_: [],
    )
    monkeypatch.setattr("omnigent.core.firstparty_background_factories", _factories)

    ctx = _ctx()
    ctx.state["firstparty_extensions"] = [firstparty_extension]
    phase = ExtensionBackgroundTasksPhase()

    await phase.startup(ctx)
    await asyncio.sleep(0)
    await phase.shutdown(ctx)

    assert ("factories", ()) in calls
    assert ("factories", (firstparty_extension,)) in calls
    assert "started" in calls


@pytest.mark.asyncio
async def test_coordination_phase_starts_and_stops_backplane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The coordination phase delegates to start/stop_coordination."""
    from omnigent.kernel.lifespan_phases import CoordinationPhase

    calls: list[str] = []

    async def _fake_start() -> object:
        calls.append("start")
        return object()

    async def _fake_stop() -> None:
        calls.append("stop")

    monkeypatch.setattr("omnigent.coordination.lifecycle.start_coordination", _fake_start)
    monkeypatch.setattr("omnigent.coordination.lifecycle.stop_coordination", _fake_stop)

    phase = CoordinationPhase()
    await phase.startup(_ctx())
    await phase.shutdown(_ctx())

    assert calls == ["start", "stop"]
