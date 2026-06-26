"""``office:agents`` channel + delta contract (BDP-2301).

The platform C# ``RealtimeTopicRegistry`` resolves the SignalR topic
``office:agents`` to the Redis channel ``office:agents:{tenant}`` with the
tenant as a DASHED guid (BDP-1397). This module is the omnigent-side half of
that hand-mirrored contract; ``test_channel.py`` pins the exact strings, and the
platform's RealtimeTopicRegistryTests pins the C# half. Keep them in lockstep
(BDP-2302 will make the registry a fetched SoT and retire this duplication).

Delta envelopes are intentionally TINY — the plugin never re-projects Office's
models. ``roster.changed`` tells the org chart to refetch the snapshot (cached
reader → omnigent SoT); ``presence.changed`` carries just an agent's new
activity status.
"""

from __future__ import annotations

from typing import Any


def office_agents_channel(tenant: str) -> str:
    """The Redis channel ByteDesk.Realtime fans out to the ``office:agents`` topic."""
    return f"office:agents:{tenant}"


def office_goals_channel(tenant: str) -> str:
    """The Redis channel ByteDesk.Realtime fans out to the ``office:goals`` topic."""
    return f"office:goals:{tenant}"


def office_inbound_channel(tenant: str) -> str:
    """The Redis channel ByteDesk.Realtime fans out to the ``office:inbound`` topic
    (the live inbound-event feed, ADR-0155)."""
    return f"office:inbound:{tenant}"


def inbound_event_changed(
    *,
    idempotency_key: str,
    source: str,
    event_type: str,
    status: str,
    occurred_at: int,
    received_at: int,
    duplicate: bool = False,
) -> dict[str, Any]:
    """A compact delta announcing an inbound event flowed through the pipeline."""
    return {
        "type": "inbound.event",
        "idempotencyKey": idempotency_key,
        "source": source,
        "eventType": event_type,
        "status": status,
        "occurredAt": occurred_at,
        "receivedAt": received_at,
        "duplicate": duplicate,
    }


def roster_changed(action: str, agent_id: str) -> dict[str, Any]:
    """An agent was created/updated/deleted (incl. live config edits)."""
    return {"type": "roster.changed", "action": action, "agentId": agent_id}


def presence_changed(agent_id: str, status: str) -> dict[str, Any]:
    """An agent's live activity status changed (active when working, idle otherwise)."""
    return {"type": "presence.changed", "agentId": agent_id, "status": status}


def goal_changed(
    *,
    change: str,
    goal_id: str,
    status: str,
    activation_state: str,
    readiness_kind: str,
    target_kind: str,
    target_id: str,
    target_label: str | None,
    owner_agent_id: str | None,
    priority: int,
    updated_at: int,
    occurred_at: int,
    dependency: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A goal changed enough for consumers to refetch/reconcile their snapshot."""
    payload: dict[str, Any] = {
        "type": "goal.changed",
        "change": change,
        "goalId": goal_id,
        "status": status,
        "activationState": activation_state,
        "readinessKind": readiness_kind,
        "targetKind": target_kind,
        "targetId": target_id,
        "targetLabel": target_label,
        "ownerAgentId": owner_agent_id,
        "priority": priority,
        "updatedAt": updated_at,
        "occurredAt": occurred_at,
    }
    if dependency is not None:
        payload["dependency"] = dependency
    return payload


def goal_planning_event(
    *,
    event_type: str,
    planning_session_id: str,
    target_kind: str,
    target_id: str,
    target_label: str | None,
    source_ids: list[str],
    occurred_at: int,
    goal_id: str | None = None,
    draft_ready: bool | None = None,
) -> dict[str, Any]:
    """A goal-planning lifecycle delta for admin UI and Platform consumers."""
    payload: dict[str, Any] = {
        "type": event_type,
        "planningSessionId": planning_session_id,
        "targetKind": target_kind,
        "targetId": target_id,
        "targetLabel": target_label,
        "sourceIds": source_ids,
        "occurredAt": occurred_at,
    }
    if goal_id is not None:
        payload["goalId"] = goal_id
    if draft_ready is not None:
        payload["draftReady"] = draft_ready
    return payload
