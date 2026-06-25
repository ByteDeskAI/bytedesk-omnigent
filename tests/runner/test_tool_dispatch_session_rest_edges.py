"""Edge tests for session REST peek/parent helpers in ``tool_dispatch.py``."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from omnigent.runner.tool_dispatch import (
    _fetch_peek_meta,
    _PeekMeta,
    _session_get_history_via_rest,
    _session_parent_id,
)


class _FakeResponse:
    def __init__(self, *, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _RouteClient:
    def __init__(self, routes: dict[tuple[str, str], _FakeResponse | Exception]) -> None:
        self._routes = routes
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def get(self, path: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(("GET", path, kwargs))
        key = ("GET", path)
        result = self._routes.get(key)
        if result is None:
            return _FakeResponse(status_code=404, payload={})
        if isinstance(result, Exception):
            raise result
        return result


@pytest.mark.asyncio
async def test_session_parent_id_returns_parent_from_snapshot() -> None:
    client = _RouteClient(
        {
            ("GET", "/v1/sessions/conv_child"): _FakeResponse(
                status_code=200,
                payload={"parent_session_id": "conv_parent"},
            )
        }
    )
    assert await _session_parent_id("conv_child", client) == "conv_parent"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_session_parent_id_returns_none_on_transport_error() -> None:
    client = _RouteClient(
        {
            ("GET", "/v1/sessions/conv_child"): httpx.ConnectError(
                "offline",
                request=MagicMock(),
            )
        }
    )
    assert await _session_parent_id("conv_child", client) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_session_parent_id_returns_none_for_top_level_or_missing_parent() -> None:
    client = _RouteClient(
        {
            ("GET", "/v1/sessions/conv_root"): _FakeResponse(
                status_code=200,
                payload={"parent_session_id": None},
            ),
            ("GET", "/v1/sessions/conv_missing"): _FakeResponse(status_code=404, payload={}),
        }
    )
    assert await _session_parent_id("conv_root", client) is None  # type: ignore[arg-type]
    assert await _session_parent_id("conv_missing", client) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_peek_meta_parses_title_and_pending_elicitations() -> None:
    client = _RouteClient(
        {
            ("GET", "/v1/sessions/conv_child"): _FakeResponse(
                status_code=200,
                payload={
                    "title": "researcher:auth",
                    "pending_elicitations": [{"elicitation_id": "elicit_1"}],
                },
            )
        }
    )
    meta = await _fetch_peek_meta("conv_child", client)  # type: ignore[arg-type]
    assert meta == _PeekMeta(
        agent="researcher",
        title="auth",
        pending_elicitations=[{"elicitation_id": "elicit_1"}],
    )


@pytest.mark.asyncio
async def test_fetch_peek_meta_degrades_on_snapshot_miss() -> None:
    client = _RouteClient(
        {
            ("GET", "/v1/sessions/conv_child"): _FakeResponse(status_code=500, payload={}),
        }
    )
    meta = await _fetch_peek_meta("conv_child", client)  # type: ignore[arg-type]
    assert meta == _PeekMeta(agent=None, title=None, pending_elicitations=[])


@pytest.mark.asyncio
async def test_session_get_history_via_rest_requires_conversation_id() -> None:
    client = _RouteClient({})
    raw = await _session_get_history_via_rest({}, client)  # type: ignore[arg-type]
    payload = json.loads(raw)
    assert "requires a non-empty" in payload["error"]


@pytest.mark.asyncio
async def test_session_get_history_via_rest_maps_status_errors() -> None:
    client = _RouteClient(
        {
            ("GET", "/v1/sessions/conv_missing/items"): _FakeResponse(status_code=404, payload={}),
            ("GET", "/v1/sessions/conv_denied/items"): _FakeResponse(status_code=403, payload={}),
        }
    )
    missing = json.loads(
        await _session_get_history_via_rest({"conversation_id": "conv_missing"}, client)  # type: ignore[arg-type]
    )
    denied = json.loads(
        await _session_get_history_via_rest({"conversation_id": "conv_denied"}, client)  # type: ignore[arg-type]
    )
    assert missing["error"] == "session_not_found"
    assert denied["error"] == "session_out_of_tree"


@pytest.mark.asyncio
async def test_session_get_history_via_rest_projects_items_chronologically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _RouteClient(
        {
            ("GET", "/v1/sessions/conv_child/items"): _FakeResponse(
                status_code=200,
                payload={
                    "data": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "newer"}],
                        },
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "older"}],
                        },
                    ]
                },
            ),
            ("GET", "/v1/sessions/conv_child"): _FakeResponse(
                status_code=200,
                payload={"title": "researcher:auth", "pending_elicitations": []},
            ),
        }
    )

    async def _fake_meta(_target_id: str, _client: object) -> _PeekMeta:
        return _PeekMeta(agent="researcher", title="auth", pending_elicitations=[])

    monkeypatch.setattr(
        "omnigent.runner.tool_dispatch._fetch_peek_meta",
        _fake_meta,
    )

    raw = await _session_get_history_via_rest({"conversation_id": "conv_child"}, client)  # type: ignore[arg-type]
    payload = json.loads(raw)
    assert payload["conversation_id"] == "conv_child"
    assert payload["agent"] == "researcher"
    assert payload["title"] == "auth"
    assert [item["text"] for item in payload["items"]] == ["older", "newer"]
