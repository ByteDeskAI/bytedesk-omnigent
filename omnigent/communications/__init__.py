"""Agent communication domain contracts.

This package is the narrow internal seam for session chat, delegation, and
status projection. Public REST/SSE schemas remain in ``omnigent.server``.
"""

from omnigent.communications.commands import (
    ChatActor,
    ChatActorKind,
    DelegateToAgentCommand,
    PostSessionEventCommand,
    StartSessionCommand,
)
from omnigent.communications.events import (
    BlueprintNodeUpdated,
    ChildSessionUpdated,
    InputAccepted,
    SessionStatusChanged,
)
from omnigent.communications.results import (
    ChatCommandResult,
    DelegationResult,
    DispatchDisposition,
)
from omnigent.communications.runner_tools import (
    CommunicationServiceProvider,
    DelegationService,
    InboxService,
    RunnerCommunicationScope,
    SessionQueryToolService,
)
from omnigent.communications.session_query import SessionQueryService
from omnigent.communications.state import (
    InvalidSessionStatusTransition,
    SessionStatus,
    UnknownSessionStatus,
    is_status_transition_allowed,
    parse_session_status,
    should_publish_status,
)

__all__ = [
    "BlueprintNodeUpdated",
    "ChatActor",
    "ChatActorKind",
    "ChatCommandResult",
    "ChildSessionUpdated",
    "CommunicationServiceProvider",
    "DelegateToAgentCommand",
    "DelegationResult",
    "DelegationService",
    "DispatchDisposition",
    "InboxService",
    "InputAccepted",
    "InvalidSessionStatusTransition",
    "PostSessionEventCommand",
    "RunnerCommunicationScope",
    "SessionStatus",
    "SessionStatusChanged",
    "SessionQueryService",
    "SessionQueryToolService",
    "StartSessionCommand",
    "UnknownSessionStatus",
    "is_status_transition_allowed",
    "parse_session_status",
    "should_publish_status",
]
