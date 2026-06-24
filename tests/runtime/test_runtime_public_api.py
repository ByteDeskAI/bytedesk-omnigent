"""Public API coverage for :mod:`omnigent.runtime` and :mod:`omnigent.runtime._globals`."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import omnigent.runtime as runtime
import omnigent.runtime._globals as _globals
import sqlalchemy as sa

from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.caps import RuntimeCaps
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.tools import ToolManager
from omnigent.tools.base import ToolContext


def _snapshot_globals() -> dict[str, object]:
    return {
        "_conversation_store": _globals._conversation_store,
        "_agent_store": _globals._agent_store,
        "_agent_cache": _globals._agent_cache,
        "_file_store": _globals._file_store,
        "_artifact_store": _globals._artifact_store,
        "_comment_store": _globals._comment_store,
        "_policy_store": _globals._policy_store,
        "_caps": _globals._caps,
        "_terminal_registry": _globals._terminal_registry,
        "_resource_registry": _globals._resource_registry,
        "_harness_process_manager": _globals._harness_process_manager,
        "_runner_client": _globals._runner_client,
        "_runner_router": _globals._runner_router,
        "_runner_ws_factory": _globals._runner_ws_factory,
        "_runner_id": _globals._runner_id,
        "_dispatch_capabilities": dict(_globals._dispatch_capabilities),
    }


def _pristine_globals() -> dict[str, object]:
    return {
        "_conversation_store": None,
        "_agent_store": None,
        "_agent_cache": None,
        "_file_store": None,
        "_artifact_store": None,
        "_comment_store": None,
        "_policy_store": None,
        "_caps": RuntimeCaps(),
        "_terminal_registry": None,
        "_resource_registry": None,
        "_harness_process_manager": None,
        "_runner_client": None,
        "_runner_router": None,
        "_runner_ws_factory": None,
        "_runner_id": None,
        "_dispatch_capabilities": {},
    }


def _restore_globals(saved: dict[str, object]) -> None:
    _globals._conversation_store = saved["_conversation_store"]  # type: ignore[assignment]
    _globals._agent_store = saved["_agent_store"]  # type: ignore[assignment]
    _globals._agent_cache = saved["_agent_cache"]  # type: ignore[assignment]
    _globals._file_store = saved["_file_store"]  # type: ignore[assignment]
    _globals._artifact_store = saved["_artifact_store"]  # type: ignore[assignment]
    _globals._comment_store = saved["_comment_store"]  # type: ignore[assignment]
    _globals._policy_store = saved["_policy_store"]  # type: ignore[assignment]
    _globals._caps = saved["_caps"]  # type: ignore[assignment]
    _globals._terminal_registry = saved["_terminal_registry"]  # type: ignore[assignment]
    _globals._resource_registry = saved["_resource_registry"]  # type: ignore[assignment]
    _globals._harness_process_manager = saved["_harness_process_manager"]  # type: ignore[assignment]
    _globals._runner_client = saved["_runner_client"]  # type: ignore[assignment]
    _globals._runner_router = saved["_runner_router"]  # type: ignore[assignment]
    _globals._runner_ws_factory = saved["_runner_ws_factory"]  # type: ignore[assignment]
    _globals._runner_id = saved["_runner_id"]  # type: ignore[assignment]
    _globals._dispatch_capabilities.clear()
    _globals._dispatch_capabilities.update(saved["_dispatch_capabilities"])  # type: ignore[arg-type]
    runtime._memory_provider_cache.clear()
    runtime.set_tool_manager(None)


@pytest.fixture(autouse=True)
def _isolate_runtime_globals() -> None:
    saved = _snapshot_globals()
    _restore_globals(_pristine_globals())
    yield
    _restore_globals(saved)


def _init_runtime(db_uri: str, tmp_path: Path, *, caps: RuntimeCaps | None = None) -> None:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    runtime.init(
        conversation_store=SqlAlchemyConversationStore(db_uri),
        agent_store=SqlAlchemyAgentStore(db_uri),
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / ".cache",
        ),
        file_store=None,
        artifact_store=artifact_store,
        comment_store=None,
        policy_store=None,
        caps=caps,
    )


def test_getters_raise_before_init() -> None:
    with pytest.raises(RuntimeError, match="runtime not initialized"):
        runtime.get_conversation_store()
    with pytest.raises(RuntimeError, match="runtime not initialized"):
        runtime.get_agent_store()
    with pytest.raises(RuntimeError, match="runtime not initialized"):
        runtime.get_agent_cache()
    with pytest.raises(RuntimeError, match="runtime not initialized"):
        runtime.get_terminal_registry()
    with pytest.raises(RuntimeError, match="runtime not initialized"):
        runtime.get_memory_provider()
    with pytest.raises(RuntimeError, match="HarnessProcessManager not initialized"):
        runtime.get_harness_process_manager()


def test_optional_getters_return_none_before_init() -> None:
    assert runtime.get_file_store() is None
    assert runtime.get_artifact_store() is None
    assert runtime.get_comment_store() is None
    assert runtime.get_policy_store() is None
    assert runtime.get_resource_registry() is None


def test_init_wires_stores_and_terminal_registry(db_uri: str, tmp_path: Path) -> None:
    custom_caps = RuntimeCaps(execution_timeout=42)
    _init_runtime(db_uri, tmp_path, caps=custom_caps)

    assert runtime.get_conversation_store() is _globals._conversation_store
    assert runtime.get_agent_store() is _globals._agent_store
    assert runtime.get_agent_cache() is _globals._agent_cache
    assert runtime.get_artifact_store() is _globals._artifact_store
    assert runtime.get_caps() is custom_caps
    assert runtime.get_terminal_registry() is _globals._terminal_registry


def test_get_memory_provider_caches_per_storage_location(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_runtime(db_uri, tmp_path)
    created: list[object] = []

    def _fake_create(location: str) -> object:
        provider = SimpleNamespace(store=object())
        created.append(provider)
        return provider

    monkeypatch.setattr(
        "omnigent.stores.memory_store.create_agent_memory_provider",
        _fake_create,
    )
    first = runtime.get_memory_provider()
    second = runtime.get_memory_provider()
    assert first is second
    assert len(created) == 1


def test_get_memory_store_returns_underlying_sqlalchemy_store(
    db_uri: str,
    tmp_path: Path,
) -> None:
    _init_runtime(db_uri, tmp_path)
    store = runtime.get_memory_store()
    assert store is runtime.get_memory_provider().store


def test_get_memory_store_raises_for_external_provider(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_runtime(db_uri, tmp_path)
    monkeypatch.setattr(
        "omnigent.runtime.get_memory_provider",
        lambda: SimpleNamespace(store=None),
    )
    with pytest.raises(RuntimeError, match="no SQLAlchemy store"):
        runtime.get_memory_store()


def test_tool_manager_contextvar_round_trip() -> None:
    mgr = MagicMock(spec=ToolManager)
    runtime.set_tool_manager(mgr)
    assert runtime.get_tool_manager() is mgr
    runtime.set_tool_manager(None)
    with pytest.raises(RuntimeError, match="no ToolManager"):
        runtime.get_tool_manager()


def test_dispatch_capability_register_lookup_and_unregister() -> None:
    cap = _globals.DispatchCapability(
        tool_mgr=MagicMock(spec=ToolManager),
        tool_ctx=ToolContext(task_id="task_1", agent_id="ag_1", conversation_id="conv_1"),
        policy_engine=None,
        conversation_id="conv_1",
        root_task_id=None,
        agent_name="demo",
    )
    runtime.register_dispatch_capability("task_parent", cap)
    assert runtime.get_dispatch_capability("task_parent") is cap
    assert runtime.get_dispatch_capability("missing") is None
    runtime.unregister_dispatch_capability("task_parent")
    assert runtime.get_dispatch_capability("task_parent") is None
    runtime.unregister_dispatch_capability("task_parent")


def test_runner_and_resource_registry_hooks() -> None:
    client = object()
    router = object()
    ws_factory = object()
    registry = object()
    pm = object()

    runtime.set_runner_client(client)
    runtime.set_runner_router(router)
    runtime.set_runner_ws_factory(ws_factory)
    runtime.set_runner_id("runner-uuid")
    runtime.set_resource_registry(registry)
    runtime.set_harness_process_manager(pm)

    assert runtime.get_runner_client() is client
    assert runtime.get_runner_router() is router
    assert runtime.get_runner_ws_factory() is ws_factory
    assert runtime.get_runner_id() == "runner-uuid"
    assert runtime.get_resource_registry() is registry
    assert runtime.get_harness_process_manager() is pm

    runtime.set_runner_client(None)
    runtime.set_runner_router(None)
    runtime.set_runner_ws_factory(None)
    runtime.set_runner_id(None)
    runtime.set_resource_registry(None)
    runtime.set_harness_process_manager(None)


def test_select_memory_embedder_none_on_sqlite(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    assert runtime._select_memory_embedder(engine) is None


def test_select_memory_embedder_none_on_pg_without_pgvector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "_pgvector_installed", lambda _conn: False)

    class _FakePGEngine:
        dialect = type("D", (), {"name": "postgresql"})

        def connect(self) -> object:
            class _Ctx:
                def __enter__(self_inner) -> object:
                    return object()

                def __exit__(self_inner, *_args: object) -> bool:
                    return False

            return _Ctx()

    assert runtime._select_memory_embedder(_FakePGEngine()) is None


def test_select_memory_embedder_resolves_default_on_pg_with_pgvector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    monkeypatch.setattr(runtime, "_pgvector_installed", lambda _conn: True)
    registry = MagicMock()
    registry.resolve_default.return_value = sentinel
    monkeypatch.setattr(
        "omnigent.stores.memory_store.build_embedder_registry",
        lambda: registry,
    )

    class _FakePGEngine:
        dialect = type("D", (), {"name": "postgresql"})

        def connect(self) -> object:
            class _Ctx:
                def __enter__(self_inner) -> object:
                    return object()

                def __exit__(self_inner, *_args: object) -> bool:
                    return False

            return _Ctx()

    assert runtime._select_memory_embedder(_FakePGEngine()) is sentinel