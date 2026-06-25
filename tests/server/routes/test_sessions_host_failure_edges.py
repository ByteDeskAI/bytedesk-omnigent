"""Edge tests for host-launch failure and native sub-agent wake helpers."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.entities import Conversation
from omnigent.entities.conversation import (
    ConversationItem,
    ErrorData,
    NewConversationItem,
)
from omnigent.entities.pagination import PagedList
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes.sessions import (
    _NATIVE_POLICY_NOT_ENFORCED_CODE,
    _forward_native_subagent_terminal_failure,
    _persist_host_launch_failure_turn,
    _persist_native_policy_notice,
)
from omnigent.server.schemas import SessionEventInput

pytestmark = pytest.mark.asyncio


def _message_body(text: str = "hello") -> SessionEventInput:
    return SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    )


class _HostFailureStore:
    """Minimal conversation store for host-launch failure helpers."""

    def __init__(self, conv: Conversation) -> None:
        self._conv = conv
        self.appended: list[tuple[str, list[ConversationItem]]] = []
        self._items: list[ConversationItem] = []

    def append(
        self,
        session_id: str,
        items: list[NewConversationItem],
    ) -> list[ConversationItem]:
        persisted = [
            ConversationItem(
                id=f"item_{len(self._items)}",
                created_at=1,
                type=item.type,
                status="completed",
                response_id=item.response_id,
                data=item.data,
                created_by=item.created_by,
            )
            for item in items
        ]
        self._items.extend(persisted)
        self.appended.append((session_id, persisted))
        return persisted

    def list_items(
        self,
        session_id: str,
        *,
        limit: int = 20,
        order: str = "desc",
    ) -> PagedList[ConversationItem]:
        del session_id, limit, order
        return PagedList(
            data=list(self._items),
            first_id=self._items[0].id if self._items else None,
            last_id=self._items[-1].id if self._items else None,
            has_more=False,
        )

    def update_conversation(self, session_id: str, **kwargs: Any) -> None:
        del session_id, kwargs


def _top_level_conv(session_id: str = "conv_host_fail") -> Conversation:
    return Conversation(
        id=session_id,
        created_at=0,
        updated_at=0,
        root_conversation_id=session_id,
        agent_id="ag_test",
    )


def _claude_subagent_conv(session_id: str = "conv_sub_fail") -> Conversation:
    return Conversation(
        id=session_id,
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_parent",
        parent_conversation_id="conv_parent",
        kind="sub_agent",
        agent_id="ag_test",
        labels={"omnigent.wrapper": "claude-native-ui"},
    )


async def test_persist_host_launch_failure_turn_records_error_and_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[tuple[str, str]] = []
    conv = _top_level_conv()
    store = _HostFailureStore(conv)

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._seed_missing_title_from_user_message",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._forward_native_subagent_terminal_failure",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_input_consumed",
        lambda sid, item: published.append((sid, "consumed")),
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_error_event",
        lambda sid, error: published.append((sid, f"error:{error.code}")),
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_terminal_pending",
        lambda sid, pending: published.append((sid, f"pending:{pending}")),
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_status",
        lambda sid, status, detail=None: published.append((sid, f"status:{status}")),
    )

    item_id = await _persist_host_launch_failure_turn(
        "conv_host_fail",
        conv,
        _message_body(),
        store,  # type: ignore[arg-type]
        host_error="harness 'codex' is not configured",
        runner_router=None,
        created_by="alice@example.com",
    )

    assert item_id == "item_0"
    assert len(store.appended) == 2
    assert store.appended[0][1][0].type == "message"
    assert store.appended[1][1][0].type == "error"
    error = store.appended[1][1][0].data
    assert isinstance(error, ErrorData)
    assert error.code == "harness_not_configured"
    assert "codex" in error.message
    assert ("conv_host_fail", "consumed") in published
    assert any(entry[1] == "status:failed" for entry in published)


async def test_persist_host_launch_failure_turn_uses_generic_host_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv = _top_level_conv("conv_generic")
    store = _HostFailureStore(conv)
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._seed_missing_title_from_user_message",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._forward_native_subagent_terminal_failure",
        AsyncMock(),
    )
    monkeypatch.setattr("omnigent.server.routes.sessions._publish_input_consumed", lambda *_: None)
    monkeypatch.setattr("omnigent.server.routes.sessions._publish_error_event", lambda *_: None)
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_terminal_pending", lambda *_: None
    )
    monkeypatch.setattr("omnigent.server.routes.sessions._publish_status", lambda *_: None)

    await _persist_host_launch_failure_turn(
        "conv_generic",
        conv,
        _message_body(),
        store,  # type: ignore[arg-type]
        host_error=None,
        runner_router=None,
        created_by=None,
    )

    error_item = store.appended[1][1][0].data
    assert isinstance(error_item, ErrorData)
    assert "omnigent setup" in error_item.message


async def test_forward_native_subagent_terminal_failure_noops_for_top_level() -> None:
    forward = AsyncMock()
    conv = _top_level_conv()
    await _forward_native_subagent_terminal_failure(
        "conv_host_fail",
        conv,
        ErrorData(source="execution", code="boot", message="failed"),
        runner_router=MagicMock(),
    )
    forward.assert_not_called()


async def test_forward_native_subagent_terminal_failure_noops_for_codex_subagent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv = Conversation(
        id="conv_codex_sub",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_parent",
        parent_conversation_id="conv_parent",
        kind="sub_agent",
        agent_id="ag_codex",
        labels={"omnigent.wrapper": "codex-native-ui-subagent"},
    )
    forward = AsyncMock(return_value=MagicMock(status_code=200, body=""))
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._forward_session_change_to_runner",
        forward,
    )
    await _forward_native_subagent_terminal_failure(
        "conv_codex_sub",
        conv,
        ErrorData(source="execution", code="boot", message="pane dead"),
        runner_router=MagicMock(),
    )
    forward.assert_not_called()


async def test_forward_native_subagent_terminal_failure_forwards_failed_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server.routes.sessions import _RunnerForwardResult

    forward = AsyncMock(
        return_value=_RunnerForwardResult(status_code=200, body=""),
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._forward_session_change_to_runner",
        forward,
    )
    conv = _claude_subagent_conv()
    await _forward_native_subagent_terminal_failure(
        "conv_sub_fail",
        conv,
        ErrorData(source="execution", code="boot", message="tmux missing"),
        runner_router=MagicMock(),
    )
    forward.assert_awaited_once()
    event = forward.await_args.args[2]
    assert event["type"] == "external_session_status"
    assert event["data"]["status"] == "failed"
    assert event["data"]["output"] == "tmux missing"


async def test_forward_native_subagent_terminal_failure_raises_when_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._forward_session_change_to_runner",
        AsyncMock(return_value=None),
    )
    conv = _claude_subagent_conv()
    with pytest.raises(OmnigentError) as exc:
        await _forward_native_subagent_terminal_failure(
            "conv_sub_fail",
            conv,
            ErrorData(source="execution", code="boot", message="down"),
            runner_router=MagicMock(),
        )
    assert exc.value.code == ErrorCode.RUNNER_UNAVAILABLE


async def test_persist_native_policy_notice_persists_and_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[str] = []
    conv = _top_level_conv("conv_policy")
    store = _HostFailureStore(conv)
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_error_event",
        lambda sid, error: published.append(error.code),
    )

    await _persist_native_policy_notice(
        "conv_policy",
        store,  # type: ignore[arg-type]
        "Codex CLI 0.128.0 is older than 0.129.0",
    )

    assert len(store.appended) == 1
    error = store.appended[0][1][0].data
    assert isinstance(error, ErrorData)
    assert error.code == _NATIVE_POLICY_NOT_ENFORCED_CODE
    assert "0.128.0" in error.message
    assert published == [_NATIVE_POLICY_NOT_ENFORCED_CODE]


async def test_persist_native_policy_notice_skips_publish_on_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[str] = []
    conv = _top_level_conv("conv_policy_dup")
    store = _HostFailureStore(conv)
    reason = "policy hook unavailable"
    existing = ErrorData(
        source="execution",
        code=_NATIVE_POLICY_NOT_ENFORCED_CODE,
        message=f"Tool-call policy enforcement is not active for this session: {reason}",
    )
    store._items.append(
        ConversationItem(
            id="item_existing",
            created_at=1,
            type="error",
            status="completed",
            response_id="turn_existing",
            data=existing,
        )
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._publish_error_event",
        lambda sid, error: published.append(error.code),
    )

    await _persist_native_policy_notice(
        "conv_policy_dup",
        store,  # type: ignore[arg-type]
        reason,
    )

    assert published == []
    assert len(store.appended) == 0
