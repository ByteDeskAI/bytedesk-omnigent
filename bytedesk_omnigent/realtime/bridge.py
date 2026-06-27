"""Agent realtime bridge (BDP-2301): fan out roster changes to the platform.

The bridge subscribes to store-neutral AgentStore mutation events instead of
patching a concrete store class. This keeps realtime fan-out independent from
the active AgentStore provider (NATS in production after the cutover).

Boot re-seed suppression: :func:`install` is called from the extension's
``background_tasks`` (lifespan), i.e. AFTER omnigent's construction-time
``_ensure_extra_builtin_agents`` seed of the ~74 builtin agents — so those seed
creates are naturally NOT emitted and a cold start can't fire a roster.changed
storm. Only post-boot mutations cross the wire.

Presence (``presence.changed``) is the next surface — it needs the
conversation→agent mapping at the ``session_stream.publish`` turn boundary;
:func:`emit_presence` is ready for that wiring.
"""

from __future__ import annotations

import logging
from typing import Any

from bytedesk_omnigent.realtime import config
from bytedesk_omnigent.realtime.channel import (
    entity_changed,
    goal_changed,
    goal_planning_event,
    inbound_event_changed,
    office_agents_channel,
    office_goals_channel,
    office_inbound_channel,
    presence_changed,
    roster_changed,
)
from bytedesk_omnigent.realtime.publisher import publish
from omnigent.stores.agent_store.events import AgentStoreEvent, subscribe

logger = logging.getLogger(__name__)

_INSTALLED = False

#: event-hub key for the in-process inbound SSE stream (/v1/inbound/events).
INBOUND_EVENT_USER_KEY = "__all__"


def emit_inbound_event(record: Any, inserted: bool = True) -> None:
    """Wire-Tap tee: fan an inbound event onto the live feed (Redis + in-process SSE).

    Mirrors :func:`emit_goal_change`. Dormant until ``BYTEDESK_REALTIME_TENANT_ID``
    is set. The ``emit`` hook the pipeline calls after the wire-tap write — even
    duplicates are emitted so they're observable.
    """
    payload = inbound_event_changed(
        idempotency_key=record.idempotency_key,
        source=record.source,
        event_type=record.type,
        status="duplicate" if not inserted else record.status,
        occurred_at=record.occurred_at,
        received_at=record.received_at,
        duplicate=not inserted,
    )
    tenant = config.tenant_id()
    if tenant:
        publish(office_inbound_channel(tenant), payload)
    try:
        from omnigent.runtime.event_hub import publish as hub_publish

        hub_publish(INBOUND_EVENT_USER_KEY, payload)
    except Exception:  # pragma: no cover - best-effort in-process SSE
        logger.exception("failed to publish inbound event-hub delta")


def emit_roster(action: str, agent_id: str) -> None:
    """Publish a roster.changed delta, unless the tenant is unconfigured."""
    tenant = config.tenant_id()
    if not tenant:
        return  # bridge dormant until BYTEDESK_REALTIME_TENANT_ID is set
    publish(office_agents_channel(tenant), roster_changed(action, agent_id))


def emit_presence(agent_id: str, status: str) -> None:
    """Publish a presence.changed delta (active when working, idle otherwise)."""
    tenant = config.tenant_id()
    if not tenant:
        return
    publish(office_agents_channel(tenant), presence_changed(agent_id, status))


def emit_goal_change(event: dict[str, Any]) -> None:
    """Publish a goal.changed delta for Omnigent-admin and future Platform consumers."""
    tenant = config.tenant_id()
    if not tenant:
        return
    payload = goal_changed(
        change=str(event["change"]),
        goal_id=str(event["goalId"]),
        status=str(event["status"]),
        activation_state=str(event["activationState"]),
        readiness_kind=str(event["readinessKind"]),
        target_kind=str(event["targetKind"]),
        target_id=str(event["targetId"]),
        target_label=event.get("targetLabel"),
        owner_agent_id=event.get("ownerAgentId"),
        priority=int(event["priority"]),
        updated_at=int(event["updatedAt"]),
        occurred_at=int(event["occurredAt"]),
        dependency=event.get("dependency"),
    )
    publish(office_goals_channel(tenant), payload)


def emit_entity_change(event: dict[str, Any]) -> None:
    """Publish an ``entity.changed`` delta for a non-goal-row goal-engine entity.

    Generalizes :func:`emit_goal_change` to condition/budget/template/delete
    mutations (BDP-2588) over the SAME ``office:goals`` channel. Dormant until
    ``BYTEDESK_REALTIME_TENANT_ID`` is set.
    """
    tenant = config.tenant_id()
    if not tenant:
        return
    reserved = {"type", "entity", "op", "id"}
    payload = entity_changed(
        entity=str(event["entity"]),
        op=str(event["op"]),
        entity_id=str(event["id"]),
        extra={k: v for k, v in event.items() if k not in reserved},
    )
    publish(office_goals_channel(tenant), payload)


def emit_goal_planning(event: dict[str, Any]) -> None:
    """Publish a goal-planning lifecycle delta for future Platform consumers."""
    tenant = config.tenant_id()
    if not tenant:
        return
    payload = goal_planning_event(
        event_type=str(event["type"]),
        planning_session_id=str(event["planningSessionId"]),
        target_kind=str(event["targetKind"]),
        target_id=str(event["targetId"]),
        target_label=event.get("targetLabel"),
        source_ids=[str(source) for source in event.get("sourceIds", [])],
        occurred_at=int(event["occurredAt"]),
        goal_id=event.get("goalId"),
        draft_ready=event.get("draftReady"),
    )
    publish(office_goals_channel(tenant), payload)


def _emit_agent_store_event(event: AgentStoreEvent) -> None:
    emit_roster(event.action, event.agent_id)


def install() -> bool:
    """Idempotently subscribe to AgentStore events."""
    global _INSTALLED
    if _INSTALLED:
        return False
    subscribe(_emit_agent_store_event)
    _INSTALLED = True
    logger.info(
        "office:agents roster bridge installed (tenant=%s)",
        config.tenant_id() or "UNSET",
    )
    return True
