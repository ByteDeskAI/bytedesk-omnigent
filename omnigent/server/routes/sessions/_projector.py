"""Class-backed projection service for session chat events."""

from __future__ import annotations

import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from typing import Any

from omnigent.communications.state import should_publish_status
from omnigent.entities import ConversationItem, ErrorData, MessageData
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.schemas import (
    ErrorDetail,
    ErrorEvent,
    OutputItemDoneEvent,
    OutputTextDeltaEvent,
    SandboxStatus,
    SessionCreatedEvent,
    SessionInputConsumedEvent,
    SessionInputConsumedPayload,
    SessionInterruptedEvent,
    SessionInterruptedPayload,
    SessionSandboxStatusEvent,
    SessionStatusEvent,
)

_Publish = Callable[[str, dict[str, Any]], None]
_PushServiceFactory = Callable[[], Any | None]
_Clock = Callable[[], float]


@dataclass(frozen=True)
class ChatEventProjector:
    """Project internal session events onto the public session stream."""

    publish: _Publish
    status_cache: MutableMapping[str, str]
    sandbox_status_cache: MutableMapping[str, SandboxStatus]
    push_service_factory: _PushServiceFactory | None = None
    clock: _Clock = time.time

    def publish_input_consumed(
        self,
        session_id: str,
        item: ConversationItem,
        cleared_pending_id: str | None = None,
    ) -> None:
        """Publish a just-persisted input item."""
        if item.type == "message" and isinstance(item.data, MessageData) and item.data.is_meta:
            return
        event = SessionInputConsumedEvent(
            type="session.input.consumed",
            data=SessionInputConsumedPayload(
                item_id=item.id,
                type=item.type,
                data=item.data.model_dump() if item.data is not None else {},
                created_by=item.created_by,
                cleared_pending_id=cleared_pending_id,
            ),
        )
        self.publish(session_id, event.model_dump())

    def publish_compaction_in_progress(self, session_id: str) -> None:
        """Publish the standard compaction progress event."""
        self.publish(session_id, {"type": "response.compaction.in_progress"})

    def publish_compaction_completed(self, session_id: str, total_tokens: int | None) -> None:
        """Publish the standard compaction completed event."""
        payload: dict[str, object] = {"type": "response.compaction.completed"}
        if total_tokens is not None:
            payload["total_tokens"] = total_tokens
        self.publish(session_id, payload)

    def publish_compaction_failed(self, session_id: str) -> None:
        """Publish the standard compaction failed event."""
        self.publish(session_id, {"type": "response.compaction.failed"})

    def publish_external_assistant_message(
        self,
        session_id: str,
        item: ConversationItem,
        *,
        response_id: str,
        agent_name: str,
    ) -> None:
        """Broadcast an assistant message appended outside task runtime."""
        del response_id, agent_name
        event = OutputItemDoneEvent(type="response.output_item.done", item=item.to_api_dict())
        self.publish(session_id, event.model_dump())

    def publish_external_conversation_item(
        self,
        session_id: str,
        item: ConversationItem,
        cleared_pending_id: str | None = None,
    ) -> None:
        """Broadcast a terminal-observed conversation item."""
        if item.type == "message" and isinstance(item.data, MessageData) and item.data.is_meta:
            return
        if (
            item.type == "message"
            and isinstance(item.data, MessageData)
            and item.data.role == "user"
        ):
            self.publish_input_consumed(session_id, item, cleared_pending_id=cleared_pending_id)
            return
        event = OutputItemDoneEvent(type="response.output_item.done", item=item.to_api_dict())
        self.publish(session_id, event.model_dump())

    def publish_external_output_text_delta(self, session_id: str, data: dict[str, Any]) -> None:
        """Broadcast a terminal-observed assistant text delta."""
        delta = data.get("delta")
        if not isinstance(delta, str):
            raise OmnigentError(
                "external_output_text_delta requires string data.delta",
                code=ErrorCode.INVALID_INPUT,
            )
        message_id = data.get("message_id")
        if message_id is not None and not isinstance(message_id, str):
            raise OmnigentError(
                "external_output_text_delta data.message_id must be a string",
                code=ErrorCode.INVALID_INPUT,
            )
        index = data.get("index")
        if index is not None and (not isinstance(index, int) or isinstance(index, bool)):
            raise OmnigentError(
                "external_output_text_delta data.index must be an integer",
                code=ErrorCode.INVALID_INPUT,
            )
        final = data.get("final")
        if final is not None and not isinstance(final, bool):
            raise OmnigentError(
                "external_output_text_delta data.final must be a boolean",
                code=ErrorCode.INVALID_INPUT,
            )
        event = OutputTextDeltaEvent(
            type="response.output_text.delta",
            delta=delta,
            message_id=message_id,
            index=index,
            final=final,
        )
        self.publish(session_id, event.model_dump(exclude_none=True))

    def publish_session_created(
        self,
        parent_id: str,
        child_session_id: str,
        agent_id: str | None,
    ) -> None:
        """Emit a child-session-created event on the parent's stream."""
        event = SessionCreatedEvent(
            type="session.created",
            conversation_id=parent_id,
            child_session_id=child_session_id,
            agent_id=agent_id,
            parent_session_id=parent_id,
        )
        self.publish(parent_id, event.model_dump())

    def publish_status(
        self,
        session_id: str,
        status: str,
        error: ErrorDetail | None = None,
        response_id: str | None = None,
    ) -> bool:
        """Publish session status and update the list-status cache."""
        previous_status = self.status_cache.get(session_id)
        if not should_publish_status(previous_status, status):
            return False
        self.status_cache[session_id] = status
        if self.push_service_factory is not None:
            try:
                push_service = self.push_service_factory()
                if push_service is not None:
                    push_service.on_status_change(session_id, previous_status, status)
            except Exception:  # noqa: BLE001 - push fanout is best-effort.
                pass
        event = SessionStatusEvent(
            type="session.status",
            conversation_id=session_id,
            status=status,  # type: ignore[arg-type]
            response_id=response_id,
            error=error,
        )
        payload = event.model_dump()
        if response_id is None:
            payload.pop("response_id", None)
        self.publish(session_id, payload)
        return True

    def publish_sandbox_status(
        self,
        session_id: str,
        stage: str,
        error: str | None = None,
    ) -> None:
        """Publish managed-sandbox launch status and update its cache."""
        if stage == "ready":
            self.sandbox_status_cache.pop(session_id, None)
        else:
            self.sandbox_status_cache[session_id] = SandboxStatus(stage=stage, error=error)
        event = SessionSandboxStatusEvent(
            type="session.sandbox_status",
            conversation_id=session_id,
            stage=stage,
            error=error,
        )
        self.publish(session_id, event.model_dump())

    def publish_changed_files_invalidated(
        self,
        session_id: str,
        environment_id: str = "default",
    ) -> None:
        """Publish a coarse filesystem-change invalidation event."""
        self.publish(
            session_id,
            {
                "type": "session.changed_files.invalidated",
                "session_id": session_id,
                "environment_id": environment_id,
            },
        )

    def publish_interrupted(self, session_id: str, response_id: str | None = None) -> None:
        """Publish a session-level interruption event."""
        event = SessionInterruptedEvent(
            type="session.interrupted",
            data=SessionInterruptedPayload(
                requested_at=int(self.clock()),
                response_id=response_id,
            ),
        )
        payload = event.model_dump()
        if response_id is None:
            data = payload.get("data")
            if isinstance(data, dict):
                data.pop("response_id", None)
        self.publish(session_id, payload)

    def publish_error_event(self, session_id: str, error: ErrorData) -> None:
        """Publish a live response error event for a persisted error item."""
        event = ErrorEvent(
            type="response.error",
            source=error.source,
            error={"code": error.code, "message": error.message},
        )
        self.publish(session_id, event.model_dump())


__all__ = ["ChatEventProjector"]
