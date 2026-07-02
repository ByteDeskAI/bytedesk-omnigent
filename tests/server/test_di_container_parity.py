"""Tests for the always-on server DI composition root."""

from __future__ import annotations

from pathlib import Path

import pytest
from dependency_injector import containers
from fastapi import FastAPI

from omnigent.runner.control_registry import RunnerControlRegistry
from omnigent.runner.routing import RunnerRouter
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.app_context import LEGACY_APP_STATE_KEYS, ServerAppContext
from omnigent.server.communication_composition import ServerCommunicationServices
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

_COMPOSITION_ROOT_STATE: dict[str, type] = {
    "runner_control_registry": RunnerControlRegistry,
    "runner_router": RunnerRouter,
    "host_registry": HostRegistry,
    "server_metrics": ServerPerformanceMetrics,
    "server_metrics_otel": ServerMetricsOtelPublisher,
    "managed_launches": ManagedLaunchTracker,
}


def _build_app(db_uri: str, tmp_path: Path, subdir: str) -> FastAPI:
    """Build a FastAPI app with isolated stores under *subdir*."""
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


def test_app_builds_with_container_by_default(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server always resolves its composition root through ``Core``."""
    monkeypatch.delenv("OMNIGENT_USE_DI_CONTAINER", raising=False)

    app = _build_app(db_uri, tmp_path, "default")

    assert issubclass(Core, containers.DeclarativeContainer)
    assert isinstance(app.state.di_container, containers.Container)
    assert isinstance(app.state.server_app_context, ServerAppContext)
    for name, typ in _COMPOSITION_ROOT_STATE.items():
        assert isinstance(getattr(app.state, name), typ), name


def test_legacy_di_env_var_does_not_change_composition(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The removed DI gate is ignored for compatibility with old env files."""
    monkeypatch.delenv("OMNIGENT_USE_DI_CONTAINER", raising=False)
    default_app = _build_app(db_uri, tmp_path, "unset")

    monkeypatch.setenv("OMNIGENT_USE_DI_CONTAINER", "0")
    disabled_app = _build_app(db_uri, tmp_path, "disabled")

    monkeypatch.setenv("OMNIGENT_USE_DI_CONTAINER", "1")
    enabled_app = _build_app(db_uri, tmp_path, "enabled")

    for app in (default_app, disabled_app, enabled_app):
        assert isinstance(app.state.di_container, containers.Container)
        assert app.state.runner_router._registry is app.state.runner_control_registry

    assert {r.path for r in default_app.routes} == {r.path for r in disabled_app.routes}
    assert {r.path for r in default_app.routes} == {r.path for r in enabled_app.routes}


def test_container_singletons_back_app_state(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Container providers are memoized and supply the app-visible objects."""
    monkeypatch.delenv("OMNIGENT_USE_DI_CONTAINER", raising=False)
    app = _build_app(db_uri, tmp_path, "singletons")

    container = app.state.di_container
    context = app.state.server_app_context
    assert container.runner_control_registry() is container.runner_control_registry()
    assert container.runner_router() is app.state.runner_router
    assert container.runner_control_registry() is app.state.runner_control_registry
    assert container.managed_launches() is app.state.managed_launches
    assert context.runner_router is app.state.runner_router
    assert context.managed_launches is app.state.managed_launches


def test_server_context_projects_legacy_app_state_keys(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The typed server context remains source of truth for legacy state keys."""
    monkeypatch.delenv("OMNIGENT_USE_DI_CONTAINER", raising=False)
    app = _build_app(db_uri, tmp_path, "context_projection")

    context = app.state.server_app_context
    for key in LEGACY_APP_STATE_KEYS:
        assert getattr(app.state, key) is getattr(context, key), key


def test_non_app_state_services_resolve_from_container(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closure-held services and communication composition are Core-owned."""
    monkeypatch.delenv("OMNIGENT_USE_DI_CONTAINER", raising=False)
    app = _build_app(db_uri, tmp_path, "services")

    container = app.state.di_container
    assert isinstance(container.mcp_pool(), ServerMcpPool)
    assert container.mcp_pool() is container.mcp_pool()
    assert isinstance(container.runner_exit_reports(), RunnerExitReports)
    assert container.runner_exit_reports() is container.runner_exit_reports()
    assert isinstance(container.communication_services(), ServerCommunicationServices)
    assert container.communication_services() is container.communication_services()
    assert (
        app.state.server_app_context.communication_services is container.communication_services()
    )
