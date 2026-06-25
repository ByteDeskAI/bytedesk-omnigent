"""Edge tests for runner client resolution helpers in ``sessions.py``."""

from __future__ import annotations

import asyncio
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


async def test_wait_for_runner_client_without_tunnel_registry_delegates(
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


async def test_wait_for_runner_client_returns_client_when_runner_connects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = httpx.AsyncClient()

    class _Registry:
        async def wait_for_runner(self, runner_id: str, *, timeout_s: float) -> str:
            assert runner_id == "runner_ok"
            assert timeout_s == 2.5
            return "tunnel-session"

    async def _fake_get_runner_client(session_id: str, runner_router: object) -> httpx.AsyncClient:
        assert session_id == "conv_ok"
        return client

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_runner_client",
        _fake_get_runner_client,
    )

    resolved = await _wait_for_runner_client(
        "conv_ok",
        None,
        _Registry(),  # type: ignore[arg-type]
        runner_id="runner_ok",
        timeout_s=2.5,
        runner_exit_reports=None,
    )

    assert resolved is client
    await client.aclose()


async def test_wait_for_runner_client_returns_none_when_connect_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Registry:
        async def wait_for_runner(self, runner_id: str, *, timeout_s: float) -> None:
            return None

    calls: list[str] = []

    async def _should_not_run(session_id: str, runner_router: object) -> httpx.AsyncClient:
        calls.append(session_id)
        raise AssertionError("runner client must not resolve after a connect timeout")

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_runner_client",
        _should_not_run,
    )

    resolved = await _wait_for_runner_client(
        "conv_timeout",
        None,
        _Registry(),  # type: ignore[arg-type]
        runner_id="runner_slow",
        timeout_s=0.01,
        runner_exit_reports=None,
    )

    assert resolved is None
    assert calls == []


async def test_wait_for_runner_client_race_returns_client_when_connect_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = httpx.AsyncClient()

    class _Registry:
        async def wait_for_runner(self, runner_id: str, *, timeout_s: float) -> str:
            await asyncio.sleep(0.05)
            return "tunnel-session"

    async def _fake_get_runner_client(session_id: str, runner_router: object) -> httpx.AsyncClient:
        return client

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_runner_client",
        _fake_get_runner_client,
    )

    resolved = await _wait_for_runner_client(
        "conv_race",
        None,
        _Registry(),  # type: ignore[arg-type]
        runner_id="runner_race",
        timeout_s=2.0,
        runner_exit_reports=MagicMock(get=lambda _rid: None),
    )

    assert resolved is client
    await client.aclose()
