"""Edge tests for runner stop helpers in ``sessions.py``."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import HostHelloFrame
from omnigent.server.host_registry import HostConnection
from omnigent.server.routes.sessions import (
    _stop_session_host_runner,
    _stop_session_via_runner,
)

pytestmark = pytest.mark.asyncio


async def test_stop_session_via_runner_returns_false_when_no_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_runner_client",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr("omnigent.runtime.get_runner_client", lambda: None)

    delivered = await _stop_session_via_runner("conv_stop", runner_router=None)

    assert delivered is False


async def test_stop_session_via_runner_returns_true_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AsyncMock()
    response = MagicMock()
    response.status_code = 202
    client.post = AsyncMock(return_value=response)
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_runner_client",
        AsyncMock(return_value=client),
    )

    delivered = await _stop_session_via_runner("conv_stop_ok", runner_router=MagicMock())

    assert delivered is True
    client.post.assert_awaited_once()
    assert client.post.await_args.args[0] == "/v1/sessions/conv_stop_ok/events"


async def test_stop_session_via_runner_raises_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AsyncMock()
    client.post = AsyncMock(side_effect=httpx.ConnectError("tunnel closed"))
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_runner_client",
        AsyncMock(return_value=client),
    )

    with pytest.raises(OmnigentError) as exc:
        await _stop_session_via_runner("conv_stop_down", runner_router=MagicMock())

    assert exc.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert "tunnel closed" in str(exc.value)


async def test_stop_session_via_runner_raises_on_non_2xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AsyncMock()
    response = MagicMock()
    response.status_code = 503
    response.text = "runner busy"
    client.post = AsyncMock(return_value=response)
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_runner_client",
        AsyncMock(return_value=client),
    )

    with pytest.raises(OmnigentError) as exc:
        await _stop_session_via_runner("conv_stop_503", runner_router=MagicMock())

    assert exc.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert "503" in str(exc.value)


async def test_stop_session_host_runner_noops_when_registry_none() -> None:
    await _stop_session_host_runner(
        "conv_host",
        "host_1",
        "runner_1",
        host_registry=None,
    )


async def test_stop_session_host_runner_noops_when_host_offline() -> None:
    registry = MagicMock()
    registry.get.return_value = None

    await _stop_session_host_runner(
        "conv_host",
        "host_offline",
        "runner_1",
        host_registry=registry,
    )

    registry.get.assert_called_once_with("host_offline")


async def test_stop_session_host_runner_sends_stop_frame_and_awaits_result() -> None:
    conn = HostConnection(
        host_id="host_live",
        ws=MagicMock(),
        hello=HostHelloFrame(
            version="0.1.0",
            frame_protocol_version=1,
            name="laptop",
            runners=["runner_live"],
            configured_harnesses={},
        ),
        owner="alice@example.com",
        outbound_queue=asyncio.Queue(),
        connected_at=0.0,
        last_frame_at=0.0,
    )
    registry = MagicMock()
    registry.get.return_value = conn

    def _send_and_resolve(_connection: HostConnection, _frame: str) -> None:
        pending = next(iter(_connection.pending_stops.values()))
        pending.set_result({"status": "stopped", "error": None})

    registry.send_text.side_effect = _send_and_resolve

    await _stop_session_host_runner(
        "conv_host_live",
        "host_live",
        "runner_live",
        host_registry=registry,
    )

    registry.send_text.assert_called_once()
    pending = next(iter(conn.pending_stops.values()))
    assert pending.done()
    assert pending.result() == {"status": "stopped", "error": None}


async def test_stop_session_host_runner_handles_connection_replaced() -> None:
    conn = HostConnection(
        host_id="host_replaced",
        ws=MagicMock(),
        hello=HostHelloFrame(
            version="0.1.0",
            frame_protocol_version=1,
            name="laptop",
            runners=[],
            configured_harnesses={},
        ),
        owner=None,
        outbound_queue=asyncio.Queue(),
        connected_at=0.0,
        last_frame_at=0.0,
    )
    registry = MagicMock()
    registry.get.return_value = conn
    registry.send_text.side_effect = ConnectionError("replaced")

    await _stop_session_host_runner(
        "conv_host",
        "host_replaced",
        "runner_1",
        host_registry=registry,
    )

    assert conn.pending_stops == {}


async def test_stop_session_host_runner_logs_failed_stop_status() -> None:
    conn = HostConnection(
        host_id="host_failed",
        ws=MagicMock(),
        hello=HostHelloFrame(
            version="0.1.0",
            frame_protocol_version=1,
            name="laptop",
            runners=[],
            configured_harnesses={},
        ),
        owner=None,
        outbound_queue=asyncio.Queue(),
        connected_at=0.0,
        last_frame_at=0.0,
    )
    registry = MagicMock()
    registry.get.return_value = conn

    def _send_and_fail(_connection: HostConnection, _frame: str) -> None:
        pending = next(iter(_connection.pending_stops.values()))
        pending.set_result({"status": "failed", "error": "process still alive"})

    registry.send_text.side_effect = _send_and_fail

    await _stop_session_host_runner(
        "conv_host_fail_stop",
        "host_failed",
        "runner_failed",
        host_registry=registry,
    )

    registry.send_text.assert_called_once()


async def test_stop_session_host_runner_handles_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = HostConnection(
        host_id="host_slow",
        ws=MagicMock(),
        hello=HostHelloFrame(
            version="0.1.0",
            frame_protocol_version=1,
            name="laptop",
            runners=[],
            configured_harnesses={},
        ),
        owner=None,
        outbound_queue=asyncio.Queue(),
        connected_at=0.0,
        last_frame_at=0.0,
    )
    registry = MagicMock()
    registry.get.return_value = conn
    registry.send_text = MagicMock()
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._STOP_RUNNER_RESULT_TIMEOUT_S",
        0.01,
    )

    await _stop_session_host_runner(
        "conv_host",
        "host_slow",
        "runner_slow",
        host_registry=registry,
    )

    assert conn.pending_stops == {}
