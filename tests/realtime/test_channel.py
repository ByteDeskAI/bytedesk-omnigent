"""BDP-2301 — pin the office:agents channel + delta contract (the omnigent half
of the hand-mirrored agreement with the C# RealtimeTopicRegistry)."""

from __future__ import annotations

from bytedesk_omnigent.realtime.channel import (
    goal_changed,
    office_agents_channel,
    office_goals_channel,
    presence_changed,
    roster_changed,
)


def test_office_agents_channel_is_dashed_guid_suffix():
    tenant = "7d484722-2847-434f-a00b-b5c7ad21e95b"
    # MUST match RealtimeTopicRegistry: $"office:agents:{tenant}" (dashed, BDP-1397).
    assert office_agents_channel(tenant) == f"office:agents:{tenant}"


def test_office_goals_channel_is_dashed_guid_suffix():
    tenant = "7d484722-2847-434f-a00b-b5c7ad21e95b"
    assert office_goals_channel(tenant) == f"office:goals:{tenant}"


def test_roster_changed_envelope():
    assert roster_changed("updated", "ag_1") == {
        "type": "roster.changed",
        "action": "updated",
        "agentId": "ag_1",
    }


def test_presence_changed_envelope():
    assert presence_changed("ag_1", "active") == {
        "type": "presence.changed",
        "agentId": "ag_1",
        "status": "active",
    }


def test_goal_changed_envelope():
    assert goal_changed(
        change="created",
        goal_id="goal_1",
        status="open",
        activation_state="waiting",
        readiness_kind="dependent",
        target_kind="department",
        target_id="Operations",
        target_label="Operations",
        owner_agent_id=None,
        priority=2,
        updated_at=100,
        occurred_at=101,
        dependency={"id": "dep_1", "status": "pending"},
    ) == {
        "type": "goal.changed",
        "change": "created",
        "goalId": "goal_1",
        "status": "open",
        "activationState": "waiting",
        "readinessKind": "dependent",
        "targetKind": "department",
        "targetId": "Operations",
        "targetLabel": "Operations",
        "ownerAgentId": None,
        "priority": 2,
        "updatedAt": 100,
        "occurredAt": 101,
        "dependency": {"id": "dep_1", "status": "pending"},
    }
