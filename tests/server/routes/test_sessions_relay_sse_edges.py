"""Unit tests for runner SSE relay extraction and error dedupe helpers."""

from __future__ import annotations

import pytest

from omnigent.entities.conversation import (
    ConversationItem,
    ErrorData,
    MessageData,
    NewConversationItem,
)
from omnigent.entities.pagination import PagedList
from omnigent.server.routes.sessions import (
    _error_item_from_sse,
    _extract_persistent_item_from_sse,
    _relay_persist_error_once,
    _resource_event_item_from_sse,
)


class _RelayErrorStore:
    """Minimal store for ``_relay_persist_error_once`` tests."""

    def __init__(self, items: list[ConversationItem] | None = None) -> None:
        self._items = list(items or [])
        self.appended: list[tuple[str, list[NewConversationItem]]] = []
        self.raise_on_append = False

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

    def append(
        self,
        session_id: str,
        items: list[NewConversationItem],
    ) -> list[ConversationItem]:
        if self.raise_on_append:
            raise RuntimeError("store down")
        self.appended.append((session_id, items))
        persisted = [
            ConversationItem(
                id=f"item_{len(self._items)}",
                created_at=1,
                type=item.type,
                status="completed",
                response_id=item.response_id,
                data=item.data,
            )
            for item in items
        ]
        self._items.extend(persisted)
        return persisted


def test_extract_persistent_item_returns_none_for_unrelated_events() -> None:
    assert _extract_persistent_item_from_sse({"type": "response.created"}) is None
    assert _extract_persistent_item_from_sse({"type": "response.output_text.delta"}) is None


def test_extract_persistent_item_skips_in_progress_function_call() -> None:
    event = {
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "status": "in_progress",
            "name": "Bash",
            "call_id": "call_1",
            "arguments": "{}",
        },
    }
    assert _extract_persistent_item_from_sse(event, response_id="resp_1") is None


def test_extract_persistent_item_builds_completed_function_call() -> None:
    event = {
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "status": "completed",
            "agent": "claude-opus-4",
            "name": "Bash",
            "call_id": "call_done",
            "arguments": '{"command": "ls"}',
        },
    }
    item = _extract_persistent_item_from_sse(event, response_id="resp_fc")
    assert item is not None
    assert item.type == "function_call"
    assert item.response_id == "resp_fc"


def test_extract_persistent_item_builds_assistant_message() -> None:
    event = {
        "type": "response.output_item.done",
        "item": {
            "type": "message",
            "role": "assistant",
            "agent": "claude-opus-4",
            "content": [{"type": "output_text", "text": "done"}],
        },
    }
    item = _extract_persistent_item_from_sse(event, response_id="resp_msg")
    assert item is not None
    assert item.type == "message"
    assert isinstance(item.data, MessageData)
    assert item.data.role == "assistant"


def test_extract_persistent_item_builds_compaction_summary() -> None:
    event = {
        "type": "compaction",
        "summary": "User asked about auth. Assistant explained OAuth.",
        "last_item_id": "item_2",
        "model": "claude-opus-4",
        "token_count": 42,
    }
    item = _extract_persistent_item_from_sse(event)
    assert item is not None
    assert item.type == "compaction"
    assert item.response_id.startswith("compact_")


def test_resource_event_item_from_sse_created_event() -> None:
    event = {
        "type": "session.resource.created",
        "resource": {"id": "term_main", "type": "terminal"},
    }
    item = _resource_event_item_from_sse("conv_res", event)
    assert item is not None
    assert item.type == "resource_event"
    assert item.data.event_type == "session.resource.created"
    assert item.data.resource_id == "term_main"
    assert item.data.resource_type == "terminal"


def test_resource_event_item_from_sse_deleted_event() -> None:
    event = {
        "type": "session.resource.deleted",
        "resource_id": "term_main",
        "resource_type": "terminal",
    }
    item = _resource_event_item_from_sse("conv_res", event)
    assert item is not None
    assert item.data.event_type == "session.resource.deleted"
    assert item.data.resource is None


def test_resource_event_item_from_sse_rejects_empty_ids() -> None:
    created = {
        "type": "session.resource.created",
        "resource": {"id": "", "type": "terminal"},
    }
    deleted = {
        "type": "session.resource.deleted",
        "resource_id": "term_main",
        "resource_type": "",
    }
    assert _resource_event_item_from_sse("conv_res", created) is None
    assert _resource_event_item_from_sse("conv_res", deleted) is None


def test_error_item_from_sse_requires_turn_for_response_error() -> None:
    event = {
        "type": "response.error",
        "source": "execution",
        "error": {"code": "boot", "message": "runner died"},
    }
    assert _error_item_from_sse(event, response_id=None) is None
    item = _error_item_from_sse(event, response_id="resp_err")
    assert item is not None
    assert item.type == "error"
    assert isinstance(item.data, ErrorData)
    assert item.data.code == "boot"


def test_error_item_from_sse_reads_response_failed_payload() -> None:
    event = {
        "type": "response.failed",
        "response": {
            "id": "resp_fail",
            "error": {"code": "turn_failed", "message": "harness exited"},
        },
    }
    item = _error_item_from_sse(event, response_id=None)
    assert item is not None
    assert item.response_id == "resp_fail"
    assert item.data.code == "turn_failed"
    assert item.data.source == "execution"


def test_error_item_from_sse_rejects_invalid_source() -> None:
    event = {
        "type": "response.error",
        "source": "unknown",
        "error": {"code": "boot", "message": "bad source"},
    }
    assert _error_item_from_sse(event, response_id="resp_bad") is None


def test_error_item_from_sse_reads_top_level_failed_error() -> None:
    event = {
        "type": "response.failed",
        "response": {"id": "resp_top"},
        "error": {"code": "pane_dead", "message": "tmux session ended"},
    }
    item = _error_item_from_sse(event, response_id=None)
    assert item is not None
    assert item.response_id == "resp_top"
    assert item.data.code == "pane_dead"
    assert item.data.source == "execution"


def test_error_item_from_sse_rejects_missing_code_or_message() -> None:
    base = {
        "type": "response.error",
        "source": "execution",
    }
    assert _error_item_from_sse({**base, "error": {"code": "", "message": "x"}}, "r1") is None
    assert _error_item_from_sse({**base, "error": {"code": "x", "message": "  "}}, "r1") is None
    assert _error_item_from_sse({**base, "error": "not-a-dict"}, "r1") is None


def test_error_item_from_sse_accepts_llm_and_tool_sources() -> None:
    for source in ("llm", "tool"):
        event = {
            "type": "response.error",
            "source": source,
            "error": {"code": "rate", "message": "slow down"},
        }
        item = _error_item_from_sse(event, response_id="resp_src")
        assert item is not None
        assert item.data.source == source


def test_resource_event_item_from_sse_rejects_non_dict_resource() -> None:
    event = {"type": "session.resource.created", "resource": "bad"}
    assert _resource_event_item_from_sse("conv_res", event) is None


@pytest.mark.asyncio
async def test_relay_persist_error_once_skips_without_store() -> None:
    item = NewConversationItem(
        type="error",
        response_id="turn_1",
        data=ErrorData(source="execution", code="boot", message="down"),
    )
    assert await _relay_persist_error_once(None, "conv_x", item) == "skipped"


@pytest.mark.asyncio
async def test_relay_persist_error_once_persists_new_error() -> None:
    store = _RelayErrorStore()
    item = NewConversationItem(
        type="error",
        response_id="turn_1",
        data=ErrorData(source="execution", code="boot", message="down"),
    )
    result = await _relay_persist_error_once(store, "conv_new", item)  # type: ignore[arg-type]
    assert result == "persisted"
    assert len(store.appended) == 1


@pytest.mark.asyncio
async def test_relay_persist_error_once_dedupes_matching_recent_error() -> None:
    existing = ConversationItem(
        id="item_existing",
        created_at=1,
        type="error",
        status="completed",
        response_id="turn_old",
        data=ErrorData(source="execution", code="boot", message="down"),
    )
    store = _RelayErrorStore([existing])
    item = NewConversationItem(
        type="error",
        response_id="turn_new",
        data=ErrorData(source="execution", code="boot", message="down"),
    )
    result = await _relay_persist_error_once(store, "conv_dup", item)  # type: ignore[arg-type]
    assert result == "duplicate"
    assert store.appended == []


@pytest.mark.asyncio
async def test_relay_persist_error_once_allows_retry_after_user_message() -> None:
    user = ConversationItem(
        id="item_user",
        created_at=2,
        type="message",
        status="completed",
        response_id="turn_user",
        data=MessageData(role="user", content=[{"type": "input_text", "text": "retry"}]),
    )
    existing = ConversationItem(
        id="item_existing",
        created_at=1,
        type="error",
        status="completed",
        response_id="turn_old",
        data=ErrorData(source="execution", code="boot", message="down"),
    )
    store = _RelayErrorStore([user, existing])
    item = NewConversationItem(
        type="error",
        response_id="turn_retry",
        data=ErrorData(source="execution", code="boot", message="down"),
    )
    result = await _relay_persist_error_once(store, "conv_retry", item)  # type: ignore[arg-type]
    assert result == "persisted"


@pytest.mark.asyncio
async def test_relay_persist_error_once_returns_failed_on_store_error() -> None:
    store = _RelayErrorStore()
    store.raise_on_append = True
    item = NewConversationItem(
        type="error",
        response_id="turn_fail",
        data=ErrorData(source="execution", code="boot", message="down"),
    )
    result = await _relay_persist_error_once(store, "conv_fail", item)  # type: ignore[arg-type]
    assert result == "failed"
