"""Edge tests for runner session-init and resource-access helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from omnigent.entities import Conversation
from omnigent.server.routes.sessions import (
    _ensure_runner_session_initialized,
    _get_runner_client_for_resource_access,
)

pytestmark = pytest.mark.asyncio


def _conv(conv_id: str = "conv_init") -> Conversation:
    return Conversation(
        id=conv_id,
        created_at=0,
        updated_at=0,
        root_conversation_id=conv_id,
        agent_id="agent_1",
        sub_agent_name="worker",
    )


async def test_ensure_runner_session_initialized_posts_handshake_and_publishes_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[str] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

    client = AsyncMock()
    client.post = AsyncMock(return_value=_Response())

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_runner_recovered_status",
        lambda sid: published.append(sid),
    )

    await _ensure_runner_session_initialized("conv_init", _conv(), client)

    client.post.assert_awaited_once()
    body = client.post.await_args.kwargs["json"]
    assert body["session_id"] == "conv_init"
    assert body["agent_id"] == "agent_1"
    assert body["sub_agent_name"] == "worker"
    assert published == ["conv_init"]


async def test_ensure_runner_session_initialized_swallows_transport_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[str] = []
    client = AsyncMock()
    client.post = AsyncMock(side_effect=httpx.ConnectError("runner down"))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_runner_recovered_status",
        lambda sid: published.append(sid),
    )

    await _ensure_runner_session_initialized("conv_fail", _conv("conv_fail"), client)

    assert published == []


async def test_ensure_runner_session_initialized_swallows_http_status_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("POST", "http://runner/v1/sessions")
    response = httpx.Response(503, request=request)
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_runner_recovered_status",
        lambda _sid: None,
    )

    await _ensure_runner_session_initialized("conv_503", _conv("conv_503"), client)


async def test_get_runner_client_for_resource_access_uses_router_when_configured() -> None:
    routed_client = MagicMock()
    router = MagicMock()
    router.aclient_for_session_resources = AsyncMock(
        return_value=SimpleNamespace(client=routed_client),
    )

    with patch("omnigent.runtime.get_runner_router", return_value=router):
        result = await _get_runner_client_for_resource_access("conv_route")

    assert result is routed_client
    router.aclient_for_session_resources.assert_awaited_once_with("conv_route")


async def test_get_runner_client_for_resource_access_falls_back_to_globals() -> None:
    fallback = MagicMock()

    with (
        patch("omnigent.runtime.get_runner_router", return_value=None),
        patch("omnigent.runtime.get_runner_client", return_value=fallback),
    ):
        result = await _get_runner_client_for_resource_access("conv_global")

    assert result is fallback
