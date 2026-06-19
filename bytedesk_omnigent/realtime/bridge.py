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
from typing import Any, Callable

from bytedesk_omnigent.realtime import config
from bytedesk_omnigent.realtime.channel import (
    office_agents_channel,
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


# ── store-method wrappers (factored out so they're unit-testable without a DB) ──

def wrap_create(orig: Callable[..., Any]) -> Callable[..., Any]:
    def create(self, agent_id, name, bundle_location, description=None):  # noqa: ANN001
        result = orig(self, agent_id, name, bundle_location, description)
        emit_roster("created", agent_id)
        return result

    return create


def wrap_update(orig: Callable[..., Any]) -> Callable[..., Any]:
    def update(self, agent_id, bundle_location):  # noqa: ANN001
        result = orig(self, agent_id, bundle_location)
        if result is not None:  # only emit when the update hit a real row
            emit_roster("updated", agent_id)
        return result

    return update


def wrap_delete(orig: Callable[..., Any]) -> Callable[..., Any]:
    def delete(self, agent_id):  # noqa: ANN001
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
