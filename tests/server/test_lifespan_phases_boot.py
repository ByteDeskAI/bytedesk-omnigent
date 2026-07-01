"""Boot + shutdown parity for the ``OMNIGENT_USE_LIFESPAN_PHASES`` selector (BDP-2623, Phase 0).

The flag (read once, at ``create_app`` time — omnigent/server/app.py) switches the
FastAPI lifespan between the monolithic ``_lifespan`` and the phase-DAG
``_lifespan_via_phases``. The pieces underneath — :class:`LifespanOrchestrator`, the
concrete phases, the topological sort — are unit-tested in isolation
(``tests/server/test_lifespan_phases.py``), but nothing booted ``create_app`` with the
flag set and drove the *real* lifespan context manager to prove the selector actually
wires an equivalent lifespan. That is the gap this closes: it mirrors the proven
``tests/server/test_di_container_parity.py`` boot-parity pattern, but instead of only
inspecting ``create_app``-time state it enters + exits the lifespan on both the flag-off
and flag-on paths and asserts:

1. the flag-on path boots and runs its lifespan without error (the phase DAG dispatches),
2. the same ``app.state.*`` surface exists after startup on both paths (same keys, same
   value types — including the lifespan-added ``harness_process_manager``), and
3. the same teardown steps run on exit on both paths (harness process-manager shutdown,
   terminal-registry shutdown, MCP-pool shutdown).

Note: ``OMNIGENT_USE_SERVICE_REGISTRY`` (the other core-refactor spine flag) is deleted
in this same change (never flipped in prod, zero consumers), so there is deliberately no
matching boot test for it — it would be dead on arrival.
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

_FLAG = "OMNIGENT_USE_LIFESPAN_PHASES"

# The one ``app.state`` key the LIFESPAN adds on top of the synchronous ``create_app``
# body — set by the harness-process-manager startup step on both paths. Asserting it is
# present after startup proves the lifespan actually ran (not just that create_app
# selected a lifespan callable).
_LIFESPAN_STARTUP_KEY = "harness_process_manager"


def _build_app(db_uri: str, tmp_path: Path, subdir: str) -> FastAPI:
    """Build a FastAPI app with stores isolated under *subdir*.

    Mirrors ``tests/server/test_di_container_parity.py``'s ``_build_app`` so the two
    boots don't share an artifact/cache directory. The lifespan is *not* entered here;
    callers drive it via ``app.router.lifespan_context(app)``.

    :param db_uri: SQLite URI for the stores.
    :param tmp_path: Per-test temp dir.
    :param subdir: Unique sub-path so the two builds get distinct artifact/cache dirs.
    :returns: The built app (lifespan not entered).
    """
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


def _build_app_with_flag(
    *,
    flag_on: bool,
    db_uri: str,
    tmp_path: Path,
    subdir: str,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    """Set/clear the lifespan-phases flag, then build an app.

    The selector reads the env var during ``create_app``, so the var must be set
    immediately before the build.

    :param flag_on: Whether to enable ``OMNIGENT_USE_LIFESPAN_PHASES``.
    :param db_uri: SQLite URI for the stores.
    :param tmp_path: Per-test temp dir.
    :param subdir: Unique artifact/cache sub-path.
    :param monkeypatch: Pytest monkeypatch (env is restored at test teardown).
    :returns: The built app.
    """
    if flag_on:
        monkeypatch.setenv(_FLAG, "1")
    else:
        monkeypatch.delenv(_FLAG, raising=False)
    return _build_app(db_uri, tmp_path, subdir)


async def _state_types_after_startup(app: FastAPI) -> dict[str, type]:
    """Return ``{app.state key: value type}`` captured after lifespan startup.

    Enters + exits the real lifespan; the snapshot is taken inside the running
    context so the lifespan-added ``harness_process_manager`` is present.

    :param app: The app whose lifespan to drive.
    :returns: A mapping of every ``app.state`` key to the type of its value.
    """
    async with app.router.lifespan_context(app):
        return {key: type(value) for key, value in app.state._state.items()}


async def _observe_teardown(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, int]:
    """Enter + exit *app*'s lifespan, counting the three teardown steps.

    Spies (in a self-undoing ``monkeypatch.context``, so a second call on another app
    in the same test doesn't double-wrap the shared terminal registry) on:

    * the process-global terminal registry's ``shutdown``,
    * this app's MCP proxy pool's ``shutdown_all``, and
    * the harness process manager's ``shutdown`` (created during startup, so it is
      wrapped from inside the running context).

    :param app: The app whose lifespan to drive.
    :param monkeypatch: Pytest monkeypatch, used via a nested context.
    :returns: Call counts keyed ``"terminal"`` / ``"mcp_pool"`` / ``"harness_pm"``.
    """
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
            # Nothing has been torn down while the context is still open.
            assert calls == {"terminal": 0, "mcp_pool": 0, "harness_pm": 0}

            harness_pm = app.state.harness_process_manager
            real_pm_shutdown = harness_pm.shutdown

            async def pm_spy() -> None:
                calls["harness_pm"] += 1
                await real_pm_shutdown()

            m.setattr(harness_pm, "shutdown", pm_spy)

    return calls


async def test_flag_on_boots_and_runs_lifespan(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag ON: the app boots and its phase-DAG lifespan runs without error.

    Drives the real ``_lifespan_via_phases`` path end to end. To prove the flag actually
    routed to the phase DAG (and not silently to the legacy ``_lifespan``, which would
    make a parity assertion trivially green), spy on ``LifespanOrchestrator.startup`` —
    a call only the phase path makes — and assert it ran exactly once. The running
    context must also expose the harness process manager the startup DAG installs.
    """
    from omnigent.kernel import lifespan_phases as lp

    real_startup = lp.LifespanOrchestrator.startup
    orchestrator_startups = {"count": 0}

    async def spy_startup(self: lp.LifespanOrchestrator, ctx: object) -> None:
        orchestrator_startups["count"] += 1
        await real_startup(self, ctx)

    monkeypatch.setattr(lp.LifespanOrchestrator, "startup", spy_startup)

    app = _build_app_with_flag(
        flag_on=True,
        db_uri=db_uri,
        tmp_path=tmp_path,
        subdir="on",
        monkeypatch=monkeypatch,
    )
    async with app.router.lifespan_context(app):
        # The phase orchestrator ran — the legacy _lifespan never builds one.
        assert orchestrator_startups["count"] == 1
        assert app.state.harness_process_manager is not None


async def test_lifespan_app_state_parity_off_equals_on(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OFF == ON: both lifespans expose the same ``app.state`` surface after startup.

    The flag changes only *how* the lifespan runs, not *what* it wires, so the post-
    startup ``app.state`` key set and per-key value types must match — including the
    lifespan-added ``harness_process_manager``.
    """
    off_app = _build_app_with_flag(
        flag_on=False,
        db_uri=db_uri,
        tmp_path=tmp_path,
        subdir="p_off",
        monkeypatch=monkeypatch,
    )
    off_types = await _state_types_after_startup(off_app)

    on_app = _build_app_with_flag(
        flag_on=True,
        db_uri=db_uri,
        tmp_path=tmp_path,
        subdir="p_on",
        monkeypatch=monkeypatch,
    )
    on_types = await _state_types_after_startup(on_app)

    # The lifespan actually ran on both paths (it added its one startup key).
    assert _LIFESPAN_STARTUP_KEY in off_types
    assert _LIFESPAN_STARTUP_KEY in on_types

    # Same key set, same per-key value types across both lifespans.
    assert set(off_types) == set(on_types)
    for key in off_types:
        assert off_types[key] is on_types[key], key


async def test_lifespan_shutdown_parity_off_equals_on(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OFF == ON: both lifespans run the same teardown steps on exit.

    Entering + exiting the context on each path must run each of the three teardown
    steps the monolithic ``_lifespan`` ``finally`` performs — harness process-manager
    shutdown, terminal-registry shutdown, MCP-pool shutdown — exactly once, with no
    error, and identically across the two paths.
    """
    off_app = _build_app_with_flag(
        flag_on=False,
        db_uri=db_uri,
        tmp_path=tmp_path,
        subdir="s_off",
        monkeypatch=monkeypatch,
    )
    off_calls = await _observe_teardown(off_app, monkeypatch)

    on_app = _build_app_with_flag(
        flag_on=True,
        db_uri=db_uri,
        tmp_path=tmp_path,
        subdir="s_on",
        monkeypatch=monkeypatch,
    )
    on_calls = await _observe_teardown(on_app, monkeypatch)

    expected = {"terminal": 1, "mcp_pool": 1, "harness_pm": 1}
    assert off_calls == expected
    assert on_calls == expected
    assert off_calls == on_calls
