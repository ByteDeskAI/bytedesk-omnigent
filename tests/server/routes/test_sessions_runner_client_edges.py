"""Edge tests for runner client resolution helpers in ``sessions.py``."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes.sessions import _get_runner_client, _wait_for_runner_client

pytestmark = pytest.mark.asyncio


async def test_get_runner_client_returns_routed_client() -> None:
    client = httpx.AsyncClient()
    router = MagicMock()
    router.aclient_for_session_resources = AsyncMock(return_value=MagicMock(client=client))

    resolved = await _get_runner_client("conv_routed", router)

    assert resolved is client
    router.aclient_for_session_resources.assert_awaited_once_with("conv_routed")
    await client.aclose()


@pytest.mark.parametrize(
    "exc",
    [
        LookupError("missing"),
        httpx.HTTPError("down"),
        OmnigentError("x", code=ErrorCode.NOT_FOUND),
    ],
)
async def test_get_runner_client_returns_none_when_router_fails(exc: BaseException) -> None:
    router = MagicMock()
    router.aclient_for_session_resources = AsyncMock(side_effect=exc)

    assert await _get_runner_client("conv_fail", router) is None


async def test_get_runner_client_falls_back_to_inprocess_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = httpx.AsyncClient()

    monkeypatch.setattr(
        "omnigent.runtime.get_runner_client",
        lambda: sentinel,
    )

    resolved = await _get_runner_client("conv_local", None)

    assert resolved is sentinel
    await sentinel.aclose()


async def test_wait_for_runner_client_returns_none_when_runner_id_missing() -> None:
    assert (
        await _wait_for_runner_client("conv_x", None, None, runner_id=None, timeout_s=1.0) is None
    )


async def test_wait_for_runner_client_without_control_registry_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = httpx.AsyncClient()

    async def _fake_get_runner_client(session_id: str, runner_router: object) -> httpx.AsyncClient:
        assert session_id == "conv_delegate"
        assert runner_router is None
        return client

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_runner_client",
        _fake_get_runner_client,
    )

    resolved = await _wait_for_runner_client(
        "conv_delegate",
        None,
        None,
        runner_id="runner_1",
        timeout_s=1.0,
    )

    assert resolved is client
    await client.aclose()


async def test_wait_for_runner_client_returns_client_when_health_probe_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "ok"}, request=request)
        ),
        base_url="http://runner",
    )

    async def _fake_get_runner_client(session_id: str, runner_router: object) -> httpx.AsyncClient:
        assert session_id == "conv_ok"
        assert runner_router == "router"
        return client

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_runner_client",
        _fake_get_runner_client,
    )

    resolved = await _wait_for_runner_client(
        "conv_ok",
        "router",  # type: ignore[arg-type]
        object(),  # legacy tunnel slot is ignored
        runner_id="runner_ok",
        timeout_s=2.5,
        runner_exit_reports=None,
    )

    assert resolved is client
    await client.aclose()


async def test_wait_for_runner_client_returns_none_when_connect_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, json={"status": "starting"}, request=request)
        ),
        base_url="http://runner",
    )

    async def _should_not_run(session_id: str, runner_router: object) -> httpx.AsyncClient:
        nonlocal calls
        calls += 1
        assert session_id == "conv_timeout"
        assert runner_router == "router"
        return client

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_runner_client",
        _should_not_run,
    )

    resolved = await _wait_for_runner_client(
        "conv_timeout",
        "router",  # type: ignore[arg-type]
        object(),
        runner_id="runner_slow",
        timeout_s=0.01,
        runner_exit_reports=None,
    )

    assert resolved is None
    assert calls >= 1
    await client.aclose()


async def test_wait_for_runner_client_returns_none_when_exit_report_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, json={"status": "starting"}, request=request)
        ),
        base_url="http://runner",
    )

    async def _fake_get_runner_client(session_id: str, runner_router: object) -> httpx.AsyncClient:
        del session_id, runner_router
        return client

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_runner_client",
        _fake_get_runner_client,
    )

    resolved = await _wait_for_runner_client(
        "conv_exit",
        "router",  # type: ignore[arg-type]
        object(),
        runner_id="runner_dead",
        timeout_s=2.0,
        runner_exit_reports=MagicMock(get=lambda _rid: "runner exited"),
    )

    assert resolved is None
    await client.aclose()
