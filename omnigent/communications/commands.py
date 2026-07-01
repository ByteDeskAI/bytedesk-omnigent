"""Typed command objects for agent communication use cases."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class ChatActorKind(StrEnum):
    """Identity class that initiated a communication command."""

    RUNNER = "runner"
    SERVER = "server"
    TOOL = "tool"
    USER = "user"


@dataclass(frozen=True, slots=True)
class ChatActor:
    """Actor metadata carried through chat/delegation commands."""

    kind: ChatActorKind
    user_id: str | None = None
    tenant_id: str | None = None
    runner_id: str | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class StartSessionCommand:
    """Create or resume a session without binding callers to REST payload shape."""

    agent_id: str
    actor: ChatActor
    title: str | None = None
    parent_session_id: str | None = None
    initial_message: str | None = None
    labels: Mapping[str, str] = field(default_factory=dict)
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class PostSessionEventCommand:
    """Post an event into a session using a storage-neutral command shape."""

    session_id: str
    actor: ChatActor
    event_type: str
    payload: Mapping[str, object] = field(default_factory=dict)
    tool_specs: tuple[Mapping[str, object], ...] = ()
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class DelegateToAgentCommand:
    """Start or reuse a child session and send it work on behalf of a parent."""

    parent_session_id: str
    actor: ChatActor
    prompt: str
    agent_id: str | None = None
    agent_name: str | None = None
    title: str | None = None
    reuse_existing: bool = True
    metadata: Mapping[str, object] = field(default_factory=dict)


__all__ = [
    "ChatActor",
    "ChatActorKind",
    "DelegateToAgentCommand",
    "PostSessionEventCommand",
    "StartSessionCommand",
]
