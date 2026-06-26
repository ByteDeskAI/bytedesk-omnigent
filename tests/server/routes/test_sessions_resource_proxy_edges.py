"""Edge tests for session resource proxy and post-switch reset helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException

from omnigent.server.routes.sessions import (
    _proxy_get_session_resources_to_runner,
    _reset_runner_resources_after_switch,
)

pytestmark = pytest.mark.asyncio


def _valid_resource_page() -> dict[str, object]:
    return {
        "object": "list",
        "data": [
            {
                "id": "default",
                "object": "session.resource",
                "type": "environment",
                "session_id": "conv_res",
                "name": "workspace",
                "metadata": {},
            }
        ],
        "first_id": "default",
        "last_id": "default",
        "has_more": False,
    }


async def test_proxy_get_session_resources_returns_validated_page() -> None:
    client = AsyncMock()
    response = MagicMock()
    response.status_code = 200
    response.json = MagicMock(return_value=_valid_resource_page())
    client.get = AsyncMock(return_value=response)

    page = await _proxy_get_session_resources_to_runner(
        client,
        "conv_res",
        resource_type="environment",
    )

    assert page.has_more is False
    assert len(page.data) == 1
    assert page.data[0].id == "default"
    client.get.assert_awaited_once()
    assert client.get.await_args.kwargs["params"] == {"type": "environment"}


async def test_proxy_get_session_resources_raises_on_non_200() -> None:
    client = AsyncMock()
    response = MagicMock()
    response.status_code = 503
    client.get = AsyncMock(return_value=response)

    with pytest.raises(HTTPException) as exc:
        await _proxy_get_session_resources_to_runner(client, "conv_bad")

    assert exc.value.status_code == 502


async def test_proxy_get_session_resources_raises_on_malformed_body() -> None:
    client = AsyncMock()
    response = MagicMock()
    response.status_code = 200
    response.json = MagicMock(return_value=["not", "an", "object"])
    client.get = AsyncMock(return_value=response)

    with pytest.raises(HTTPException) as exc:
        await _proxy_get_session_resources_to_runner(client, "conv_malformed")

    assert exc.value.status_code == 502
    assert "malformed" in str(exc.value.detail)


async def test_proxy_get_session_resources_raises_runner_unavailable_on_dead_runner() -> None:
    # A dead runner (ConnectError, the uniform BDP-2579 F2 signal) now surfaces
    # as RUNNER_UNAVAILABLE so the read path can self-heal instead of 502-storm.
    from omnigent.errors import ErrorCode, OmnigentError

    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("runner offline"))

    with pytest.raises(OmnigentError) as exc:
        await _proxy_get_session_resources_to_runner(client, "conv_down")

    assert exc.value.code == ErrorCode.RUNNER_UNAVAILABLE


async def test_proxy_get_session_resources_raises_502_on_other_http_error() -> None:
    # A non-connect HTTP error is still a generic 502 (not a dead runner).
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(HTTPException) as exc:
        await _proxy_get_session_resources_to_runner(client, "conv_down")

    assert exc.value.status_code == 502
    assert "unavailable" in str(exc.value.detail)


async def test_reset_runner_resources_after_switch_publishes_invalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[str] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

    runner = AsyncMock()
    runner.post = AsyncMock(return_value=_Response())

    async def _get_runner(_session_id: str) -> httpx.AsyncClient:
        return runner  # type: ignore[return-value]

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_runner_client_for_resource_access",
        _get_runner,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_changed_files_invalidated",
        lambda sid: published.append(sid),
    )

    await _reset_runner_resources_after_switch("conv_switch")

    runner.post.assert_awaited_once()
    assert published == ["conv_switch"]


async def test_reset_runner_resources_after_switch_noops_without_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[str] = []

    async def _no_runner(_session_id: str) -> None:
        return None

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_runner_client_for_resource_access",
        _no_runner,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_changed_files_invalidated",
        lambda sid: published.append(sid),
    )

    await _reset_runner_resources_after_switch("conv_none")

    assert published == []


async def test_reset_runner_resources_after_switch_swallows_runner_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[str] = []
    runner = AsyncMock()
    runner.post = AsyncMock(side_effect=httpx.ConnectError("reset failed"))

    async def _get_runner(_session_id: str) -> httpx.AsyncClient:
        return runner  # type: ignore[return-value]

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_runner_client_for_resource_access",
        _get_runner,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_changed_files_invalidated",
        lambda sid: published.append(sid),
    )

    await _reset_runner_resources_after_switch("conv_err")

    assert published == []
