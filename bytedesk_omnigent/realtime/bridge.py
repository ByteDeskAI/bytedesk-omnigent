"""Agent realtime bridge (BDP-2301): fan out roster changes to the platform
org chart via ``office:agents``.

The "monkeypatch publish at import" choice (no omnigent core edits): we wrap
``SqlAlchemyAgentStore.{create,update,delete}`` in place so every RUNTIME roster
mutation — a hire, a fire, or a live config edit (``apply_bundle_update`` →
``agent_store.update``, BDP-2287) — emits a ``roster.changed`` delta. The org
chart reacts by refetching its cached snapshot (omnigent = SoT).

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
from collections.abc import Callable
from typing import Any

from bytedesk_omnigent.realtime import config
from bytedesk_omnigent.realtime.channel import (
    goal_changed,
    office_agents_channel,
    office_goals_channel,
    presence_changed,
    roster_changed,
)
from bytedesk_omnigent.realtime.publisher import publish

logger = logging.getLogger(__name__)

_INSTALLED = False


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


# ── store-method wrappers (factored out so they're unit-testable without a DB) ──


def wrap_create(orig: Callable[..., Any]) -> Callable[..., Any]:
    def create(self, agent_id, name, bundle_location, description=None):
        result = orig(self, agent_id, name, bundle_location, description)
        emit_roster("created", agent_id)
        return result

    return create


def wrap_update(orig: Callable[..., Any]) -> Callable[..., Any]:
    def update(self, agent_id, bundle_location, *args, **kwargs):
        result = orig(self, agent_id, bundle_location, *args, **kwargs)
        if result is not None:  # only emit when the update hit a real row
            emit_roster("updated", agent_id)
        return result

    return update


def wrap_delete(orig: Callable[..., Any]) -> Callable[..., Any]:
    def delete(self, agent_id):
        result = orig(self, agent_id)
        if result:  # True == the agent existed and was deleted
            emit_roster("deleted", agent_id)
        return result

    return delete


def install() -> bool:
    """Idempotently wrap the concrete agent store. Returns True if it patched,
    False if already installed."""
    global _INSTALLED
    if _INSTALLED:
        return False
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore

    SqlAlchemyAgentStore.create = wrap_create(SqlAlchemyAgentStore.create)
    SqlAlchemyAgentStore.update = wrap_update(SqlAlchemyAgentStore.update)
    SqlAlchemyAgentStore.delete = wrap_delete(SqlAlchemyAgentStore.delete)
    _INSTALLED = True
    logger.info(
        "office:agents roster bridge installed (tenant=%s)",
        config.tenant_id() or "UNSET",
    )
    return True
