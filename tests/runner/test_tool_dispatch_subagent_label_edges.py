"""Edge tests for sub-agent label and child-session lookup helpers."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omnigent.runner.tool_dispatch import (
    _SESSION_WRAPPER_LABEL_KEY,
    _find_existing_child_session,
    _has_subagent,
    _list_child_sessions,
    _publish_child_launching_update,
    _session_wrapper_label,
    _subagent_label,
    _SubagentLabel,
)


def test_subagent_label_extracts_string_fields() -> None:
    label = _subagent_label({"tool": "claude", "session_name": "issue-1"})
    assert label == _SubagentLabel(agent="claude", title="issue-1")


@pytest.mark.parametrize(
    ("child", "expected"),
    [
        ({"tool": "", "session_name": "issue-1"}, _SubagentLabel(agent=None, title="issue-1")),
        ({"tool": 42, "session_name": "issue-1"}, _SubagentLabel(agent=None, title="issue-1")),
        ({"tool": "claude", "session_name": None}, _SubagentLabel(agent="claude", title=None)),
        ({}, _SubagentLabel(agent=None, title=None)),
    ],
)
def test_subagent_label_rejects_non_string_identity(
    child: dict[str, object], expected: _SubagentLabel
) -> None:
    assert _subagent_label(child) == expected


def test_session_wrapper_label_reads_native_wrapper_key() -> None:
    payload = {"labels": {_SESSION_WRAPPER_LABEL_KEY: "codex-native-ui"}}
    assert _session_wrapper_label(payload) == "codex-native-ui"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"labels": "bad"},
        {"labels": {_SESSION_WRAPPER_LABEL_KEY: ""}},
    ],
)
def test_session_wrapper_label_returns_none_when_absent(payload: dict[str, object]) -> None:
    assert _session_wrapper_label(payload) is None


@dataclass
class _SubAgent:
    name: str


def test_has_subagent_matches_sub_agents_list() -> None:
    spec = SimpleNamespace(sub_agents=[_SubAgent("researcher")], tools=None)
    assert _has_subagent("researcher", spec) is True
    assert _has_subagent("missing", spec) is False


def test_has_subagent_matches_tools_dict() -> None:
    spec = SimpleNamespace(sub_agents=[], tools={"worker": object()})
    assert _has_subagent("worker", spec) is True
    assert _has_subagent("researcher", spec) is False


def test_has_subagent_returns_false_without_spec() -> None:
    assert _has_subagent("worker", None) is False


def test_publish_child_launching_update_uses_callback_when_provided() -> None:
    captured: list[tuple[str, dict[str, Any]]] = []

    def _capture(session_id: str, event: dict[str, Any]) -> None:
        captured.append((session_id, event))

    _publish_child_launching_update(
        parent_session_id="conv_parent",
        child_session_id="conv_child",
        title="Child title",
        tool="claude",
        session_name="issue-9",
        publish_event=_capture,
    )

    assert len(captured) == 1
    session_id, event = captured[0]
    assert session_id == "conv_parent"
    assert event["type"] == "session.child_session.updated"
    assert event["child"]["current_task_status"] == "launching"
    assert event["child"]["busy"] is False


def test_publish_child_launching_update_falls_back_to_session_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[tuple[str, dict[str, Any]]] = []

    def _capture_publish(session_id: str, event: dict[str, Any]) -> None:
        published.append((session_id, event))

    monkeypatch.setattr(
        "omnigent.runtime.session_stream.publish",
        _capture_publish,
    )
    _publish_child_launching_update(
        parent_session_id="conv_parent",
        child_session_id="conv_child",
        title="Child title",
        tool="claude",
        session_name="issue-9",
        publish_event=None,
    )

    assert published[0][0] == "conv_parent"
    assert published[0][1]["child_session_id"] == "conv_child"


class _ChildSessionsTransport:
    """Mock transport for child-session list endpoints."""

    def __init__(self, payload: dict[str, Any], *, status_code: int = 200) -> None:
        self._payload = payload
        self._status_code = status_code

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            self._status_code,
            json=self._payload,
            request=request,
        )


@pytest.mark.asyncio
async def test_list_child_sessions_returns_data_rows() -> None:
    transport = _ChildSessionsTransport(
        {
            "data": [
                {"id": "conv_child", "tool": "claude", "session_name": "issue-1"},
                "skip-me",
            ],
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), base_url="http://server"
    ) as client:
        rows = await _list_child_sessions(server_client=client, conversation_id="conv_parent")

    assert isinstance(rows, list)
    assert len(rows) == 1
    assert rows[0]["id"] == "conv_child"


@pytest.mark.asyncio
async def test_list_child_sessions_surfaces_http_errors() -> None:
    transport = _ChildSessionsTransport({"error": "nope"}, status_code=503)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), base_url="http://server"
    ) as client:
        result = await _list_child_sessions(server_client=client, conversation_id="conv_parent")

    assert isinstance(result, str)
    assert "failed to list child sessions" in result
    assert "503" in result


@pytest.mark.asyncio
async def test_list_child_sessions_errors_when_data_missing() -> None:
    transport = _ChildSessionsTransport({"unexpected": True})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), base_url="http://server"
    ) as client:
        result = await _list_child_sessions(server_client=client, conversation_id="conv_parent")

    assert result == "Error: server child_sessions response missing data list"


@pytest.mark.asyncio
async def test_find_existing_child_session_matches_open_child() -> None:
    payload = {
        "data": [
            {
                "id": "conv_child",
                "tool": "claude",
                "session_name": "issue-1",
                "labels": {},
                "title": "claude:issue-1",
            },
            {
                "id": "conv_closed",
                "tool": "claude",
                "session_name": "issue-1",
                "labels": {"omnigent.session.closed": "true"},
                "title": "claude:issue-1 (closed)",
            },
        ],
    }
    transport = _ChildSessionsTransport(payload)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), base_url="http://server"
    ) as client:
        found = await _find_existing_child_session(
            server_client=client,
            conversation_id="conv_parent",
            agent="claude",
            title="issue-1",
        )

    assert isinstance(found, dict)
    assert found["id"] == "conv_child"


@pytest.mark.asyncio
async def test_find_existing_child_session_returns_none_when_absent() -> None:
    transport = _ChildSessionsTransport({"data": []})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), base_url="http://server"
    ) as client:
        found = await _find_existing_child_session(
            server_client=client,
            conversation_id="conv_parent",
            agent="claude",
            title="missing",
        )

    assert found is None


@pytest.mark.asyncio
async def test_find_existing_child_session_propagates_list_errors() -> None:
    transport = _ChildSessionsTransport({"error": "down"}, status_code=500)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), base_url="http://server"
    ) as client:
        found = await _find_existing_child_session(
            server_client=client,
            conversation_id="conv_parent",
            agent="claude",
            title="issue-1",
        )

    assert isinstance(found, str)
    assert "failed to list child sessions" in found
