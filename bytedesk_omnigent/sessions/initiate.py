"""The ``sys_session_initiate`` seam (BDP-2279 α3b, ADR-0142). See package docstring."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from bytedesk_omnigent.scheduler.scheduler import CronTrigger

if TYPE_CHECKING:
    import httpx

_logger = logging.getLogger(__name__)

#: Base URL of this server's own loopback listener, e.g.
#: ``"http://127.0.0.1:8123"``. When set, the live self-call initiator is built.
_SELF_BASE_URL_ENV = "OMNIGENT_SELF_BASE_URL"
#: Trusted identity the self-call asserts in header-auth mode (the default
#: deployed ``AuthProvider`` reads ``X-Forwarded-Email`` from a trusted upstream).
#: The cron loop IS that trusted upstream, so it asserts the dispatch identity.
_DISPATCH_IDENTITY_ENV = "OMNIGENT_CRON_DISPATCH_IDENTITY"
_DEFAULT_DISPATCH_IDENTITY = "local"
#: Per-call timeout for the self-call. Create + event-post both return promptly
#: (the event POST is 202 fire-and-forget — it does not block on turn completion).
_SELF_CALL_TIMEOUT_S = 30.0


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


class HttpSelfCallInitiator:
    """A live :class:`SessionInitiator` that re-enters the runtime via the server's
    own sessions HTTP API (BDP-2347).

    Detached / boot code (the cron loop, a delivered durable signal) cannot use
    the request-scoped ``api.runtime.*``, and starting an agent *turn* requires
    the runner-acquisition orchestration that lives entirely in the
    ``POST /v1/sessions/{id}/events`` route handler (managed-sandbox rendezvous /
    host launch, bound to ``request.app.state``). There is no clean in-process
    turn-start outside a request — so this initiator does what the
    :class:`SessionInitiator` docstring prescribes: re-enter through the trusted
    HTTP route, driving the EXISTING create→bind-runner→dispatch path unchanged.

    Two sync calls (``initiate`` is sync by Protocol; the cron loop already runs
    ``dispatch`` under ``asyncio.to_thread``):

    1. ``POST /v1/sessions`` — create the root session bound to ``agent_id``.
    2. ``POST /v1/sessions/{id}/events`` — post the seed message, which starts the
       turn (202, fire-and-forget).

    In header-auth mode (the deployed default) the cron loop is the trusted
    upstream, so it asserts the dispatch identity via ``X-Forwarded-Email``.
    """

    def __init__(
        self,
        *,
        base_url: str,
        identity_email: str = _DEFAULT_DISPATCH_IDENTITY,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._identity_email = identity_email
        self._transport = transport

    def _client(self) -> httpx.Client:
        import httpx

        return httpx.Client(
            base_url=self._base_url,
            timeout=_SELF_CALL_TIMEOUT_S,
            headers={"X-Forwarded-Email": self._identity_email},
            transport=self._transport,
        )

    def initiate(
        self,
        *,
        agent_id: str,
        prompt: str,
        source: str,
        metadata: dict | None = None,
    ) -> str:
        labels = {"source": source}
        if metadata:
            labels.update({k: str(v) for k, v in metadata.items()})
        with self._client() as client:
            create = client.post(
                "/v1/sessions",
                json={"agent_id": agent_id, "labels": labels},
            )
            create.raise_for_status()
            session_id = create.json()["id"]
            event = client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": prompt}],
                    },
                },
            )
            event.raise_for_status()
        return session_id


def build_self_call_initiator_from_env() -> HttpSelfCallInitiator | None:
    """Build the live self-call initiator from the environment, or ``None``.

    Fail-closed: without ``OMNIGENT_SELF_BASE_URL`` set, returns ``None`` so the
    cron loop keeps its explicit logged fallback (``_log_only_dispatch``) — tests
    and headless runs stay no-op rather than crashing on a missing listener.
    """
    base_url = os.environ.get(_SELF_BASE_URL_ENV, "").strip()
    if not base_url:
        return None
    identity = (
        os.environ.get(_DISPATCH_IDENTITY_ENV, "").strip() or _DEFAULT_DISPATCH_IDENTITY
    )
    return HttpSelfCallInitiator(base_url=base_url, identity_email=identity)
