"""Tests for server communication service composition."""

from __future__ import annotations

from omnigent.communications import ChatApplicationService
from omnigent.server.communication_composition import (
    ServerCommunicationServices,
    build_server_communication_services,
    get_server_communication_services,
    set_server_communication_services,
)
from omnigent.server.routes.sessions._projector import ChatEventProjector
from omnigent.server.routes.sessions._runner._dispatch_strategies import (
    DefaultRunnerEventDispatchStrategy,
    NativeTerminalMessageDispatchStrategy,
    SessionEventDispatcher,
)


def test_default_composition_builds_chat_projection_and_dispatch_services() -> None:
    services = build_server_communication_services()

    assert isinstance(services.chat_application_service, ChatApplicationService)
    assert isinstance(services.chat_event_projector(), ChatEventProjector)

    dispatcher = services.session_event_dispatcher()
    assert isinstance(dispatcher, SessionEventDispatcher)
    assert [type(strategy) for strategy in dispatcher.strategies] == [
        NativeTerminalMessageDispatchStrategy,
        DefaultRunnerEventDispatchStrategy,
    ]


def test_process_local_composition_can_be_installed_and_reset() -> None:
    original = get_server_communication_services()
    override = ServerCommunicationServices()

    try:
        set_server_communication_services(override)
        assert get_server_communication_services() is override
    finally:
        set_server_communication_services(None)

    assert get_server_communication_services() is not override
    assert isinstance(original, ServerCommunicationServices)
