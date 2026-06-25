"""Edge tests for agent-list fetch helpers in ``tool_dispatch.py``."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from omnigent.runner.tool_dispatch import _agent_list_fetch


class _FakeResponse:
    def __init__(self, *, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _FakeClient:
    def __init__(self, response: _FakeResponse | Exception) -> None:
        self._response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get(self, path: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((path, kwargs))
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


@pytest.mark.asyncio
async def test_agent_list_fetch_returns_data_on_success() -> None:
    client = _FakeClient(
        _FakeResponse(
            status_code=200,
            payload={"data": [{"id": "ag_1", "name": "polly"}]},
        )
    )
    rows = await _agent_list_fetch("/v1/agents", client)  # type: ignore[arg-type]
    assert rows == [{"id": "ag_1", "name": "polly"}]
    assert client.calls[0][0] == "/v1/agents"
    assert client.calls[0][1]["params"]["order"] == "desc"


@pytest.mark.asyncio
async def test_agent_list_fetch_returns_empty_on_non_200() -> None:
    client = _FakeClient(_FakeResponse(status_code=500, payload={"error": "boom"}))
    assert await _agent_list_fetch("/v1/agents", client) == []  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_agent_list_fetch_returns_empty_on_transport_error() -> None:
    client = _FakeClient(httpx.ConnectError("offline", request=MagicMock()))
    assert await _agent_list_fetch("/v1/sessions", client) == []  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_agent_list_fetch_returns_empty_when_data_is_not_list() -> None:
    client = _FakeClient(_FakeResponse(status_code=200, payload={"data": "bad"}))
    assert await _agent_list_fetch("/v1/agents", client) == []  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_agent_list_fetch_returns_empty_when_json_missing_data() -> None:
    client = _FakeClient(_FakeResponse(status_code=200, payload={}))
    assert await _agent_list_fetch("/v1/agents", client) == []  # type: ignore[arg-type]
