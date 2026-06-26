"""Boot-parity tests for the DI composition root (BDP-2368).

The ``dependency-injector`` ``Core`` container is introduced behind
``OMNIGENT_USE_DI_CONTAINER`` (default OFF) as a behavior-neutral capstone:
flag-OFF must build exactly as today, and flag-ON must resolve the *same*
composition-root object graph from the container. These tests are the merge
gate proving OFF == ON so the flag is a true no-op until an explicit later flip.

The two builds use independent store instances (one per ``create_app`` call),
so parity is asserted on the wired object *shapes* and internal wiring (each
build's runner router points at that build's tunnel registry; each build holds
exactly one of every singleton type), plus the flag-driven presence/absence of
``app.state.di_container`` — not cross-build instance identity, which would be a
false signal for distinct stores.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dependency_injector import containers
from fastapi import FastAPI

from omnigent.runner.control_registry import RunnerControlRegistry
from omnigent.runner.routing import RunnerRouter
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.container import Core
from omnigent.server.host_registry import HostRegistry, RunnerExitReports
from omnigent.server.managed_hosts import ManagedLaunchTracker
from omnigent.server.mcp_pool import ServerMcpPool
from omnigent.server.performance_metrics import (
    ServerMetricsOtelPublisher,
    ServerPerformanceMetrics,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

# The composition-root singletons exposed on ``app.state``, with the type each
# must resolve to. The boot must hold exactly one of each, regardless of which
# construction path produced it.
_COMPOSITION_ROOT_STATE: dict[str, type] = {
    "runner_control_registry": RunnerControlRegistry,
    "runner_router": RunnerRouter,
    "host_registry": HostRegistry,
    "server_metrics": ServerPerformanceMetrics,
    "server_metrics_otel": ServerMetricsOtelPublisher,
    "managed_launches": ManagedLaunchTracker,
}


def _build_app(db_uri: str, tmp_path: Path, subdir: str) -> FastAPI:
    """Build a FastAPI app with isolated stores under *subdir*.

    :param db_uri: SQLite URI for the stores.
    :param tmp_path: Per-test temp dir.
    :param subdir: Unique sub-path so the two builds don't share an
        artifact/cache directory.
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


def test_flag_off_builds_without_container(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag OFF: the app builds inline and exposes no DI container."""
    monkeypatch.delenv("OMNIGENT_USE_DI_CONTAINER", raising=False)
    app = _build_app(db_uri, tmp_path, "off")
    assert app.state.di_container is None
    for name, typ in _COMPOSITION_ROOT_STATE.items():
        assert isinstance(getattr(app.state, name), typ), name


def test_flag_on_builds_via_container(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag ON: the app resolves the composition root from the ``Core`` container."""
    monkeypatch.setenv("OMNIGENT_USE_DI_CONTAINER", "1")
    app = _build_app(db_uri, tmp_path, "on")
    # ``Core`` is a DeclarativeContainer; instantiating it yields a
    # ``DynamicContainer`` (dependency-injector's runtime container type), so
    # assert against the common ``Container`` base and the declarative subclass.
    assert issubclass(Core, containers.DeclarativeContainer)
    assert isinstance(app.state.di_container, containers.Container)
    for name, typ in _COMPOSITION_ROOT_STATE.items():
        assert isinstance(getattr(app.state, name), typ), name


def test_boot_parity_off_equals_on(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OFF == ON: both boots expose the same composition-root shape + wiring.

    The merge gate. The only intended difference between the two boots is the
    presence of ``app.state.di_container``; every composition-root singleton
    resolves to the same type and the same internal wiring (the runner router
    points at that boot's tunnel registry) on both paths.
    """
    monkeypatch.delenv("OMNIGENT_USE_DI_CONTAINER", raising=False)
    off = _build_app(db_uri, tmp_path, "p_off")

    monkeypatch.setenv("OMNIGENT_USE_DI_CONTAINER", "1")
    on = _build_app(db_uri, tmp_path, "p_on")

    # Same composition-root types on both paths.
    for name, typ in _COMPOSITION_ROOT_STATE.items():
        assert type(getattr(off.state, name)) is type(getattr(on.state, name)) is typ, name

    # Internal wiring parity: each boot's runner router is wired to that boot's
    # control registry (Singleton resolution must not create a second registry).
    assert off.state.runner_router._registry is off.state.runner_control_registry
    assert on.state.runner_router._registry is on.state.runner_control_registry

    # The container path holds exactly one of each singleton (memoized).
    container = on.state.di_container
    assert container.runner_control_registry() is container.runner_control_registry()
    assert container.runner_router() is on.state.runner_router
    assert container.runner_control_registry() is on.state.runner_control_registry
    assert container.managed_launches() is on.state.managed_launches

    # Mounted route surface is identical (the flag changes construction, not
    # the API surface) — same set of paths on both apps.
    off_paths = {r.path for r in off.routes}
    on_paths = {r.path for r in on.routes}
    assert off_paths == on_paths

    # The only intended app.state difference is the container handle itself.
    assert off.state.di_container is None
    assert isinstance(on.state.di_container, containers.Container)


def test_mcp_pool_and_exit_reports_resolve_on_both_paths(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The non-app.state composition-root singletons resolve on both paths.

    ``ServerMcpPool`` and ``RunnerExitReports`` are held in closures rather
    than on ``app.state``; assert the container produces them (flag ON) as the
    same memoized singletons the inline path constructs (flag OFF builds them
    too, just not observably here).
    """
    monkeypatch.setenv("OMNIGENT_USE_DI_CONTAINER", "1")
    app = _build_app(db_uri, tmp_path, "mcp")
    container = app.state.di_container
    assert isinstance(container.mcp_pool(), ServerMcpPool)
    assert container.mcp_pool() is container.mcp_pool()
    assert isinstance(container.runner_exit_reports(), RunnerExitReports)
    assert container.runner_exit_reports() is container.runner_exit_reports()
