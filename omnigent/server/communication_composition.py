"""Server composition for agent communication services."""

from __future__ import annotations

from dataclasses import dataclass, field

from omnigent.communications import ChatApplicationService
from omnigent.server.routes.sessions._projector import ChatEventProjector
from omnigent.server.routes.sessions._runner._dispatch_strategies import (
    DefaultRunnerEventDispatchStrategy,
    NativeTerminalMessageDispatchStrategy,
    SessionEventDispatcher,
)


@dataclass(frozen=True)
class ServerCommunicationServices:
    """Factory surface for server-owned chat, projection, and dispatch services."""

    chat_application_service: ChatApplicationService = field(
        default_factory=lambda: _build_chat_application_service()
    )

    def chat_event_projector(self) -> ChatEventProjector:
        """Build the stream projector with current session caches and push service."""
        from omnigent.runtime import session_stream
        from omnigent.server.push.service import get_push_service
        from omnigent.server.routes import sessions

        return ChatEventProjector(
            publish=session_stream.publish,
            status_cache=sessions._session_status_cache,
            sandbox_status_cache=sessions._session_sandbox_status_cache,
            push_service_factory=get_push_service,
        )

    def session_event_dispatcher(self) -> SessionEventDispatcher:
        """Build the runner event dispatcher with route-boundary dependencies."""
        from omnigent.runtime import pending_inputs
        from omnigent.server.routes import sessions

        return SessionEventDispatcher(
            strategies=(
                NativeTerminalMessageDispatchStrategy(
                    is_native_terminal_session=sessions._is_native_terminal_session,
                    build_native_terminal_message_event=(
                        sessions._build_native_terminal_message_event
                    ),
                    ensure_native_terminal_ready=sessions._ensure_native_terminal_ready,
                    persist_native_terminal_failure=sessions._persist_native_terminal_failure,
                    persist_native_policy_notice=sessions._persist_native_policy_notice,
                    record_pending_input=pending_inputs.record,
                    resolve_pending_input=pending_inputs.resolve,
                    forward_native_terminal_message=sessions._forward_native_terminal_message,
                ),
                DefaultRunnerEventDispatchStrategy(
                    forward_event=sessions._forward_event_to_runner,
                ),
            )
        )


_server_communication_services: ServerCommunicationServices | None = None


def build_server_communication_services() -> ServerCommunicationServices:
    """Build the default server communication service composition."""
    return ServerCommunicationServices()


def set_server_communication_services(services: ServerCommunicationServices | None) -> None:
    """Install the process-local server communication composition."""
    global _server_communication_services
    _server_communication_services = services


def get_server_communication_services() -> ServerCommunicationServices:
    """Return installed communication services, or a fallback for isolated tests."""
    if _server_communication_services is None:
        return build_server_communication_services()
    return _server_communication_services


def _build_chat_application_service() -> ChatApplicationService:
    from omnigent.entities.conversation import parse_item_data
    from omnigent.server.routes.sessions._constants import (
        _ALLOWED_EVENT_TYPES,
        _APPROVAL_TYPE,
        _COMPACT_TYPE,
        _EXTERNAL_ASSISTANT_MESSAGE_TYPE,
        _EXTERNAL_CODEX_SUBAGENT_START_TYPE,
        _EXTERNAL_COMPACTION_STATUS_TYPE,
        _EXTERNAL_CONVERSATION_ITEM_TYPE,
        _EXTERNAL_ELICITATION_RESOLVED_TYPE,
        _EXTERNAL_MODEL_CHANGE_TYPE,
        _EXTERNAL_OUTPUT_TEXT_DELTA_TYPE,
        _EXTERNAL_SESSION_INTERRUPTED_TYPE,
        _EXTERNAL_SESSION_STATUS_TYPE,
        _EXTERNAL_SESSION_TODOS_TYPE,
        _EXTERNAL_SESSION_USAGE_TYPE,
        _EXTERNAL_SUBAGENT_START_TYPE,
        _INTERRUPT_TYPE,
        _MCP_ELICITATION_TYPE,
        _SLASH_COMMAND_TYPE,
        _STOP_SESSION_TYPE,
    )
    from omnigent.tools.client_specified import parse_client_side_tool_specs

    return ChatApplicationService(
        allowed_event_types=_ALLOWED_EVENT_TYPES,
        payload_validation_exempt_event_types=frozenset(
            {
                _INTERRUPT_TYPE,
                _APPROVAL_TYPE,
                _MCP_ELICITATION_TYPE,
                _COMPACT_TYPE,
                _SLASH_COMMAND_TYPE,
                _STOP_SESSION_TYPE,
                _EXTERNAL_ASSISTANT_MESSAGE_TYPE,
                _EXTERNAL_CONVERSATION_ITEM_TYPE,
                _EXTERNAL_OUTPUT_TEXT_DELTA_TYPE,
                _EXTERNAL_SESSION_INTERRUPTED_TYPE,
                _EXTERNAL_ELICITATION_RESOLVED_TYPE,
                _EXTERNAL_SESSION_STATUS_TYPE,
                _EXTERNAL_SESSION_USAGE_TYPE,
                _EXTERNAL_COMPACTION_STATUS_TYPE,
                _EXTERNAL_MODEL_CHANGE_TYPE,
                _EXTERNAL_SESSION_TODOS_TYPE,
                _EXTERNAL_SUBAGENT_START_TYPE,
                _EXTERNAL_CODEX_SUBAGENT_START_TYPE,
            }
        ),
        item_payload_validator=parse_item_data,
        tool_spec_validator=parse_client_side_tool_specs,
    )


__all__ = [
    "ServerCommunicationServices",
    "build_server_communication_services",
    "get_server_communication_services",
    "set_server_communication_services",
]
