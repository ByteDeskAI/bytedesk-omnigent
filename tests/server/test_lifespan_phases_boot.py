"""Boot + shutdown coverage for the canonical phase-DAG server lifespan.

The old ``OMNIGENT_USE_LIFESPAN_PHASES`` selector is retired. ``create_app`` now
always wires the phase-DAG lifespan, so these tests drive the real FastAPI
lifespan context and assert:

1. the phase orchestrator runs even when a stale env file sets the retired flag,
2. the lifespan still exposes the startup-added ``app.state`` surface, and
3. the expected teardown hooks run exactly once.

The phase classes and topological ordering remain unit-tested in
``tests/server/test_lifespan_phases.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI

from omnigent.runtime import get_terminal_registry
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

pytestmark = pytest.mark.asyncio

_RETIRED_FLAG = "OMNIGENT_USE_LIFESPAN_PHASES"

# The one ``app.state`` key the lifespan adds on top of the synchronous
# ``create_app`` body. It is set by the harness-process-manager startup phase.
_LIFESPAN_STARTUP_KEY = "harness_process_manager"


def _build_app(db_uri: str, tmp_path: Path, subdir: str) -> FastAPI:
    """Build a FastAPI app with stores isolated under *subdir*."""
    artifact_store = LocalArtifactStore(str(tmp_path / subdir / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / subdir / "cache",
        ),
    )


async def _state_types_after_startup(app: FastAPI) -> dict[str, type]:
    """Return ``{app.state key: value type}`` captured after lifespan startup."""
    async with app.router.lifespan_context(app):
        return {key: type(value) for key, value in app.state._state.items()}


async def _observe_teardown(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, int]:
    """Enter + exit *app*'s lifespan, counting the teardown steps."""
    calls = {"terminal": 0, "mcp_pool": 0, "harness_pm": 0}

    with monkeypatch.context() as m:
        registry = get_terminal_registry()
        real_registry_shutdown = registry.shutdown

        async def registry_spy() -> None:
            calls["terminal"] += 1
            await real_registry_shutdown()

        m.setattr(registry, "shutdown", registry_spy)

        pool = app.state.server_mcp_pool
        real_pool_shutdown = pool.shutdown_all

        async def pool_spy() -> None:
            calls["mcp_pool"] += 1
            await real_pool_shutdown()

        m.setattr(pool, "shutdown_all", pool_spy)

        async with app.router.lifespan_context(app):
            assert calls == {"terminal": 0, "mcp_pool": 0, "harness_pm": 0}

            harness_pm = app.state.harness_process_manager
            real_pm_shutdown = harness_pm.shutdown

            async def pm_spy() -> None:
                calls["harness_pm"] += 1
                await real_pm_shutdown()

            m.setattr(harness_pm, "shutdown", pm_spy)

    return calls


@pytest.mark.parametrize("retired_flag_value", [None, "0", "1"])
async def test_app_lifespan_uses_phase_orchestrator_by_default(
    retired_flag_value: str | None,
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retired env selector no longer changes the canonical lifespan path."""
    from omnigent.kernel import lifespan_phases as lp

    if retired_flag_value is None:
        monkeypatch.delenv(_RETIRED_FLAG, raising=False)
        subdir = "unset"
    else:
        monkeypatch.setenv(_RETIRED_FLAG, retired_flag_value)
        subdir = f"retired_{retired_flag_value}"

    real_startup = lp.LifespanOrchestrator.startup
    orchestrator_startups = {"count": 0}

    async def spy_startup(self: lp.LifespanOrchestrator, ctx: object) -> None:
        orchestrator_startups["count"] += 1
        await real_startup(self, ctx)

    monkeypatch.setattr(lp.LifespanOrchestrator, "startup", spy_startup)

    app = _build_app(db_uri, tmp_path, subdir)
    async with app.router.lifespan_context(app):
        assert orchestrator_startups["count"] == 1
        assert app.state.harness_process_manager is not None


async def test_lifespan_app_state_after_startup(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical lifespan exposes the same app-visible startup key."""
    monkeypatch.setenv(_RETIRED_FLAG, "0")
    app = _build_app(db_uri, tmp_path, "state")

    state_types = await _state_types_after_startup(app)

    assert _LIFESPAN_STARTUP_KEY in state_types
    assert "runner_router" in state_types
    assert "server_mcp_pool" in state_types
    assert "di_container" in state_types


async def test_lifespan_shutdown_runs_expected_teardown_steps(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical lifespan tears down harness, terminal, and MCP resources."""
    monkeypatch.setenv(_RETIRED_FLAG, "0")
    app = _build_app(db_uri, tmp_path, "shutdown")

    calls = await _observe_teardown(app, monkeypatch)

    assert calls == {"terminal": 1, "mcp_pool": 1, "harness_pm": 1}
