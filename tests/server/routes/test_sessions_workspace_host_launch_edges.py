"""Edge tests for workspace validation and host-runner launch helpers in sessions.py."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.requests import Request

from omnigent.entities import Agent, Conversation, LoadedAgent
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import HostHelloFrame
from omnigent.inner.datamodel import OSEnvSpec
from omnigent.server.host_registry import HostConnection
from omnigent.server.routes import sessions as sessions_mod
from omnigent.server.routes.sessions import (
    _HostLaunchAttempt,
    _launch_runner_on_host,
    _validate_session_workspace,
    _wait_for_runner_client,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec

pytestmark = pytest.mark.asyncio


def _make_request(*, host_registry: object | None, host_store: object | None) -> Request:
    app = Starlette()
    app.state.host_registry = host_registry
    app.state.host_store = host_store
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/sessions",
        "headers": [],
        "app": app,
    }
    return Request(scope)


def _host_conv(conv_id: str) -> Conversation:
    return Conversation(
        id=conv_id,
        created_at=0,
        updated_at=0,
        root_conversation_id=conv_id,
        host_id="host_test",
        workspace="/work/repo",
    )


def _agent(*, agent_id: str = "ag_test") -> Agent:
    return Agent(
        id=agent_id,
        created_at=1,
        name="test-agent",
        bundle_location="test:///bundle",
    )


async def test_validate_session_workspace_requires_workspace_when_host_set() -> None:
    with pytest.raises(OmnigentError) as exc:
        await _validate_session_workspace(
            user_id="alice",
            host_id="host_1",
            workspace=None,
            agent=_agent(),
            agent_cache=MagicMock(),
            request=_make_request(host_registry=MagicMock(), host_store=None),
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert "workspace required" in str(exc.value)


async def test_validate_session_workspace_requires_absolute_path() -> None:
    with pytest.raises(OmnigentError) as exc:
        await _validate_session_workspace(
            user_id="alice",
            host_id="host_1",
            workspace="relative/path",
            agent=_agent(),
            agent_cache=MagicMock(),
            request=_make_request(host_registry=MagicMock(), host_store=None),
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert "absolute path" in str(exc.value)


async def test_validate_session_workspace_requires_agent_cache() -> None:
    with pytest.raises(OmnigentError) as exc:
        await _validate_session_workspace(
            user_id="alice",
            host_id="host_1",
            workspace="/work/repo",
            agent=_agent(),
            agent_cache=None,
            request=_make_request(host_registry=MagicMock(), host_store=None),
        )
    assert exc.value.code == ErrorCode.INTERNAL_ERROR
    assert "agent cache" in str(exc.value)


async def test_validate_session_workspace_requires_host_registry() -> None:
    with pytest.raises(OmnigentError) as exc:
        await _validate_session_workspace(
            user_id="alice",
            host_id="host_1",
            workspace="/work/repo",
            agent=_agent(),
            agent_cache=MagicMock(),
            request=_make_request(host_registry=None, host_store=None),
        )
    assert exc.value.code == ErrorCode.INTERNAL_ERROR
    assert "host registry" in str(exc.value)


async def test_validate_session_workspace_resolves_host_owner_when_store_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server.routes import _workspace_validation

    seen: dict[str, Any] = {}

    def _fake_resolve_host_owner(
        *,
        user_id: str | None,
        host_id: str,
        host_store: object,
    ) -> MagicMock:
        seen["user_id"] = user_id
        seen["host_id"] = host_id
        seen["host_store"] = host_store
        host = MagicMock()
        host.name = "laptop"
        return host

    async def _fake_validate_workspace(**kwargs: Any) -> str:
        seen.update(kwargs)
        return "/canonical/work"

    monkeypatch.setattr(
        "omnigent.server.routes._host_launch.resolve_host_owner",
        _fake_resolve_host_owner,
    )
    monkeypatch.setattr(_workspace_validation, "validate_workspace", _fake_validate_workspace)

    cache = MagicMock()
    spec = AgentSpec(
        spec_version=1,
        name="agent",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        os_env=OSEnvSpec(type="caller_process", cwd="/work"),
    )
    cache.load.return_value = LoadedAgent(spec=spec, workdir=Path("/tmp/work"))

    host_store = MagicMock()
    result = await _validate_session_workspace(
        user_id="alice@example.com",
        host_id="host_abc",
        workspace="/work/repo",
        agent=_agent(),
        agent_cache=cache,
        request=_make_request(host_registry=MagicMock(), host_store=host_store),
    )

    assert result == "/canonical/work"
    assert seen["user_id"] == "alice@example.com"
    assert seen["host_id"] == "host_abc"
    assert seen["host_store"] is host_store
    assert seen["host_id"] == "host_abc"
    assert seen["workspace"] == "/work/repo"
    assert seen["spec_cwd"] == "/work"
    assert seen["host_name_for_errors"] == "laptop"


async def test_validate_session_workspace_treats_missing_os_env_as_unrestricted_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server.routes import _workspace_validation

    seen: dict[str, Any] = {}

    async def _fake_validate_workspace(**kwargs: Any) -> str:
        seen.update(kwargs)
        return kwargs["workspace"]

    monkeypatch.setattr(_workspace_validation, "validate_workspace", _fake_validate_workspace)

    cache = MagicMock()
    cache.load.return_value = LoadedAgent(
        spec=AgentSpec(
            spec_version=1,
            name="headless",
            executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
        ),
        workdir=Path("/tmp/work"),
    )

    await _validate_session_workspace(
        user_id=None,
        host_id="host_abc",
        workspace="/work/repo",
        agent=_agent(),
        agent_cache=cache,
        request=_make_request(host_registry=MagicMock(), host_store=None),
    )

    assert seen["spec_cwd"] is None


async def test_validate_session_workspace_wraps_bundle_load_failure() -> None:
    cache = MagicMock()
    cache.load.side_effect = RuntimeError("bundle missing")

    with pytest.raises(OmnigentError) as exc:
        await _validate_session_workspace(
            user_id="alice",
            host_id="host_1",
            workspace="/work/repo",
            agent=_agent(),
            agent_cache=cache,
            request=_make_request(host_registry=MagicMock(), host_store=None),
        )
    assert exc.value.code == ErrorCode.INTERNAL_ERROR
    assert "failed to load agent spec" in str(exc.value)


async def test_validate_session_workspace_maps_workspace_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server.routes import _workspace_validation

    async def _fake_validate_workspace(**_kwargs: Any) -> str:
        raise _workspace_validation.WorkspaceValidationError("outside boundary")

    monkeypatch.setattr(_workspace_validation, "validate_workspace", _fake_validate_workspace)

    cache = MagicMock()
    cache.load.return_value = LoadedAgent(
        spec=AgentSpec(
            spec_version=1,
            name="agent",
            executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        ),
        workdir=Path("/tmp/work"),
    )

    with pytest.raises(OmnigentError) as exc:
        await _validate_session_workspace(
            user_id="alice",
            host_id="host_1",
            workspace="/etc/passwd",
            agent=_agent(),
            agent_cache=cache,
            request=_make_request(host_registry=MagicMock(), host_store=None),
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert "outside boundary" in str(exc.value)


def _host_conn() -> HostConnection:
    return HostConnection(
        host_id="host_test",
        ws=MagicMock(),
        hello=HostHelloFrame(version="0.1.0", frame_protocol_version=1, name="laptop"),
        owner="alice",
        outbound_queue=asyncio.Queue(),
        connected_at=0.0,
        last_frame_at=0.0,
    )


async def test_launch_runner_on_host_records_owner_and_returns_success() -> None:
    conv = _host_conv("conv_launch")
    store = MagicMock()
    registry = MagicMock()
    registry.send_text = MagicMock()
    tunnel = MagicMock()
    tunnel.record_launch_owner = MagicMock()
    conn = _host_conn()

    async def _complete_launch() -> None:
        await asyncio.sleep(0.01)
        for request_id, future in list(conn.pending_launches.items()):
            if not future.done():
                future.set_result({"status": "ok"})

    complete_launch_task = asyncio.create_task(_complete_launch())

    attempt = await _launch_runner_on_host(
        conv,
        store,
        registry,
        conn,
        owner="alice@example.com",
        tunnel_registry=tunnel,
    )
    await complete_launch_task

    assert isinstance(attempt, _HostLaunchAttempt)
    assert attempt.runner_id
    assert attempt.error is None
    assert attempt.error_code is None
    store.replace_runner_id.assert_called_once()
    tunnel.record_launch_owner.assert_called_once_with(attempt.runner_id, "alice@example.com")
    registry.send_text.assert_called_once()


async def test_launch_runner_on_host_returns_structured_failure_from_host() -> None:
    conv = _host_conv("conv_fail")
    store = MagicMock()
    registry = MagicMock()
    registry.send_text = MagicMock()
    conn = _host_conn()

    async def _fail_launch() -> None:
        await asyncio.sleep(0.01)
        for _request_id, future in list(conn.pending_launches.items()):
            if not future.done():
                future.set_result(
                    {
                        "status": "failed",
                        "error_code": "harness_not_configured",
                        "error": "harness missing",
                    }
                )

    fail_launch_task = asyncio.create_task(_fail_launch())

    attempt = await _launch_runner_on_host(
        conv,
        store,
        registry,
        conn,
        owner=None,
        tunnel_registry=None,
    )
    await fail_launch_task

    assert attempt.error_code == "harness_not_configured"
    assert attempt.error == "harness missing"


async def test_launch_runner_on_host_survives_connection_error() -> None:
    conv = _host_conv("conv_disconnect")
    store = MagicMock()
    registry = MagicMock()
    registry.send_text = MagicMock(side_effect=ConnectionError("gone"))
    conn = _host_conn()

    attempt = await _launch_runner_on_host(
        conv,
        store,
        registry,
        conn,
        owner=None,
        tunnel_registry=None,
    )

    assert attempt.runner_id
    assert attempt.error is None
    assert conn.pending_launches == {}


async def test_launch_runner_on_host_timeout_returns_runner_id_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv = _host_conv("conv_timeout")
    store = MagicMock()
    registry = MagicMock()
    registry.send_text = MagicMock()
    conn = _host_conn()

    monkeypatch.setattr(sessions_mod, "_HOST_LAUNCH_RESULT_TIMEOUT_S", 0.01)

    attempt = await _launch_runner_on_host(
        conv,
        store,
        registry,
        conn,
        owner=None,
        tunnel_registry=None,
    )

    assert attempt.runner_id
    assert attempt.error is None
    assert conn.pending_launches == {}


async def test_wait_for_runner_client_aborts_when_exit_report_arrives() -> None:
    class _SlowRegistry:
        async def wait_for_runner(self, runner_id: str, *, timeout_s: float) -> str:
            del runner_id, timeout_s
            await asyncio.sleep(5.0)
            return "tunnel-session"

    reports = {"runner_dead": {"exit_code": 1}}

    resolved = await _wait_for_runner_client(
        "conv_dead",
        None,
        _SlowRegistry(),  # type: ignore[arg-type]
        runner_id="runner_dead",
        timeout_s=2.0,
        runner_exit_reports=reports,  # type: ignore[arg-type]
    )

    assert resolved is None


async def test_cancel_managed_launch_tasks_noops_when_empty() -> None:
    from omnigent.server.routes.sessions import _managed_launch_tasks, cancel_managed_launch_tasks

    _managed_launch_tasks.clear()
    await cancel_managed_launch_tasks()
    assert _managed_launch_tasks == set()


async def test_cancel_managed_launch_tasks_cancels_inflight_tasks() -> None:
    from omnigent.server.routes.sessions import _managed_launch_tasks, cancel_managed_launch_tasks

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _slow_task() -> None:
        started.set()
        try:
            await asyncio.sleep(60.0)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(_slow_task())
    _managed_launch_tasks.add(task)
    await asyncio.sleep(0)
    assert started.is_set()

    await cancel_managed_launch_tasks()

    assert cancelled.is_set()
    assert task.cancelled() or task.done()
