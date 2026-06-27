"""Store-neutral AgentStore mutation events."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

AgentStoreAction = Literal["created", "updated", "deleted"]
AgentStoreListener = Callable[["AgentStoreEvent"], None]


@dataclass(frozen=True)
class AgentStoreEvent:
    """A compact AgentStore mutation event."""

    action: AgentStoreAction
    agent_id: str


_LISTENERS: list[AgentStoreListener] = []


def subscribe(listener: AgentStoreListener) -> bool:
    """Register *listener* once. Returns ``True`` when newly registered."""
    if listener in _LISTENERS:
        return False
    _LISTENERS.append(listener)
    return True


def unsubscribe(listener: AgentStoreListener) -> None:
    """Remove *listener* if present."""
    try:
        _LISTENERS.remove(listener)
    except ValueError:
        return


def emit(action: AgentStoreAction, agent_id: str) -> None:
    """Fan out an AgentStore mutation to best-effort in-process listeners."""
    event = AgentStoreEvent(action=action, agent_id=agent_id)
    for listener in tuple(_LISTENERS):
        try:
            listener(event)
        except Exception:  # noqa: BLE001 - listeners must not break writes
            logger.warning(
                "agent store event listener failed for %s %s",
                action,
                agent_id,
                exc_info=True,
            )


def reset_for_test() -> None:
    """Clear listeners for tests."""
    _LISTENERS.clear()
