"""Edge tests for native dispatch success and rollback paths."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from omnigent.entities import Conversation, ConversationItem, PagedList
from omnigent.entities.conversation import ErrorData
from omnigent.runtime import pending_inputs
from omnigent.server.routes.sessions import (
    _NATIVE_POLICY_NOT_ENFORCED_CODE,
    _dispatch_session_event_to_runner,
)
from omnigent.server.schemas import SessionEventInput


class _DispatchStore:
    """Minimal store for native dispatch edge tests."""

    def __init__(self, conv: Conversation) -> None:
        self._conv = conv
        self.appended: list[ConversationItem] = []

    def append(
        self,
        session_id: str,
        items: list[Any],
    ) -> list[ConversationItem]:
        del session_id
        import time

        persisted = [
            ConversationItem(
                id=f"item_{len(self.appended)}",
                type=item.type,
                status="completed",
                response_id=item.response_id,
                created_at=int(time.time()),
                data=item.data,
                created_by=getattr(item, "created_by", None),
            )
            for item in items
        ]
        self.appended.extend(persisted)
        return persisted

    def list_items(
        self,
        session_id: str,
        *,
        limit: int = 20,
        order: str = "desc",
    ) -> PagedList[ConversationItem]:
        del session_id, limit, order
        items = list(self.appended)
        return PagedList(
            data=items,
            first_id=items[0].id if items else None,
            last_id=items[-1].id if items else None,
            has_more=False,
        )


class _NativeRunnerClient:
    """Runner client with per-path canned responses."""

    def __init__(
        self,
        *,
        ensure_status: int = 200,
        ensure_body: dict[str, Any] | None = None,
        events_status: int = 202,
        events_exc: BaseException | None = None,
    ) -> None:
        self.ensure_status = ensure_status
        self.ensure_body = ensure_body or {}
        self.events_status = events_status
        self.events_exc = events_exc
        self.post_calls: list[tuple[str, dict[str, Any] | None]] = []

    async def post(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        del timeout
        self.post_calls.append((path, json))
        if path.endswith("/resources/terminals"):
            return httpx.Response(self.ensure_status, json=self.ensure_body)
        if path.endswith("/events"):
            if self.events_exc is not None:
                raise self.events_exc
            return httpx.Response(self.events_status, json={"queued": True})
        raise AssertionError(f"unexpected path {path}")


def _claude_conv() -> Conversation:
    return Conversation(
        id="conv_native_ok",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_native_ok",
        agent_id="ag_test",
        labels={
            "omnigent.ui": "terminal",
            "omnigent.wrapper": "claude-code-native-ui",
        },
    )


def _user_body(text: str = "hello claude") -> SessionEventInput:
    return SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    )


@pytest.mark.asyncio
async def test_dispatch_native_success_returns_pending_id() -> None:
    conv = _claude_conv()
    store = _DispatchStore(conv)
    client = _NativeRunnerClient()
    pending_inputs.reset_for_tests()

    result = await _dispatch_session_event_to_runner(
        "conv_native_ok",
        conv,
        _user_body(),
        store,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        agent_name="claude-native-ui",
        file_store=None,
        artifact_store=None,
        created_by="alice@example.com",
    )

    assert result.item_id is None
    assert result.pending_id is not None
    assert result.pending_id.startswith("pending_")
    assert store.appended == []
    assert [path for path, _ in client.post_calls] == [
        "/v1/sessions/conv_native_ok/resources/terminals",
        "/v1/sessions/conv_native_ok/events",
    ]


@pytest.mark.asyncio
async def test_dispatch_native_policy_notice_persists_banner() -> None:
    conv = _claude_conv()
    store = _DispatchStore(conv)
    reason = "Codex CLI 0.128.0 is older than 0.129.0"
    client = _NativeRunnerClient(
        ensure_body={"policy_hook_disabled_reason": reason},
    )
    pending_inputs.reset_for_tests()

    result = await _dispatch_session_event_to_runner(
        "conv_native_ok",
        conv,
        _user_body("with policy notice"),
        store,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        agent_name="claude-native-ui",
        file_store=None,
        artifact_store=None,
        created_by=None,
    )

    assert result.pending_id is not None
    errors = [item for item in store.appended if item.type == "error"]
    assert len(errors) == 1
    assert isinstance(errors[0].data, ErrorData)
    assert errors[0].data.code == _NATIVE_POLICY_NOT_ENFORCED_CODE
    assert reason in errors[0].data.message


@pytest.mark.asyncio
async def test_dispatch_native_rolls_back_pending_on_forward_failure() -> None:
    conv = _claude_conv()
    store = _DispatchStore(conv)
    client = _NativeRunnerClient(events_exc=httpx.ConnectError("tunnel down"))
    pending_inputs.reset_for_tests()

    with pytest.raises(HTTPException):
        await _dispatch_session_event_to_runner(
            "conv_native_ok",
            conv,
            _user_body("ghost message"),
            store,  # type: ignore[arg-type]
            client,  # type: ignore[arg-type]
            agent_name="claude-native-ui",
            file_store=None,
            artifact_store=None,
            created_by=None,
        )

    assert pending_inputs.snapshot_for("conv_native_ok") == []
