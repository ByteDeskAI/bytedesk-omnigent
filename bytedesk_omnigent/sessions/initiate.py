"""The ``sys_session_initiate`` seam (BDP-2279 α3b, ADR-0142). See package docstring."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from bytedesk_omnigent.scheduler.scheduler import CronTrigger

_logger = logging.getLogger(__name__)


@runtime_checkable
class SessionInitiator(Protocol):
    """Starts a fresh root agent session (the self-re-entry spawn seam).

    Implementations re-enter the runtime via a trusted path (detached/boot code
    cannot use the request-scoped ``api.runtime.*`` — it must re-enter through a
    trusted route): resolve ``agent_id`` to its bundle, create the session, bind a
    runner, and post ``prompt`` to start the turn.
    """

    def initiate(
        self,
        *,
        agent_id: str,
        prompt: str,
        source: str,
        metadata: dict | None = None,
    ) -> str:
        """Initiate a root session for ``agent_id`` seeded with ``prompt``.

        :returns: The created session/conversation id.
        """
        ...


# Deploy-time registry. The server registers the live initiator at startup; until
# then the cron loop degrades to log-only (the established degrade posture).
_initiator: SessionInitiator | None = None


def set_session_initiator(initiator: SessionInitiator | None) -> None:
    """Register (or clear) the process-wide live :class:`SessionInitiator`."""
    global _initiator
    _initiator = initiator


def get_session_initiator() -> SessionInitiator | None:
    """Return the registered live :class:`SessionInitiator`, or ``None``."""
    return _initiator


def _trigger_prompt(trigger: CronTrigger) -> str:
    """Derive the seed prompt for a fired trigger.

    Prefers an explicit ``payload.prompt``; falls back to a deterministic
    self-describing line so a misconfigured trigger still produces a meaningful,
    non-empty turn rather than an empty message.
    """
    if trigger.payload and isinstance(trigger.payload.get("prompt"), str):
        prompt = trigger.payload["prompt"].strip()
        if prompt:
            return prompt
    return f"Scheduled trigger fired: {trigger.key}"


def build_cron_dispatch(
    initiator: SessionInitiator,
) -> Callable[[CronTrigger], None]:
    """Adapt a :class:`SessionInitiator` into a cron-scheduler ``dispatch``.

    Returns a ``(CronTrigger) -> None`` callable (the shape
    :func:`~bytedesk_omnigent.scheduler.scheduler.run_cron_scheduler_tick` expects) that
    initiates a root session for the trigger's agent, seeded with the trigger's
    prompt. Pure + injectable so the mapping is unit-provable without a live
    runtime.
    """

    def _dispatch(trigger: CronTrigger) -> None:
        session_id = initiator.initiate(
            agent_id=trigger.agent_id,
            prompt=_trigger_prompt(trigger),
            source=f"cron:{trigger.key}",
            metadata={"trigger_id": trigger.id, "trigger_key": trigger.key},
        )
        _logger.info(
            "cron initiated session %s for agent=%s key=%s",
            session_id,
            trigger.agent_id,
            trigger.key,
        )

    return _dispatch
