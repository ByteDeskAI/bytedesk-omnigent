"""BDP-2301 — agent roster bridge: emit gating + AgentStore event subscription."""

from __future__ import annotations

import bytedesk_omnigent.realtime.bridge as bridge


def _capture(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(bridge, "publish", lambda ch, payload: calls.append((ch, payload)))
    return calls


def test_emit_roster_publishes_when_tenant_set(monkeypatch):
    monkeypatch.setenv("BYTEDESK_REALTIME_TENANT_ID", "tenant-abc")
    calls = _capture(monkeypatch)
    bridge.emit_roster("created", "ag_1")
    assert calls == [
        (
            "office:agents:tenant-abc",
            {"type": "roster.changed", "action": "created", "agentId": "ag_1"},
        )
    ]


def test_emit_roster_noop_when_tenant_unset(monkeypatch):
    monkeypatch.delenv("BYTEDESK_REALTIME_TENANT_ID", raising=False)
    calls = _capture(monkeypatch)
    bridge.emit_roster("created", "ag_1")
    assert calls == []


def test_emit_presence_publishes_when_tenant_set(monkeypatch):
    monkeypatch.setenv("BYTEDESK_REALTIME_TENANT_ID", "tenant-abc")
    calls = _capture(monkeypatch)
    bridge.emit_presence("ag_1", "active")
    assert calls == [
        (
            "office:agents:tenant-abc",
            {"type": "presence.changed", "agentId": "ag_1", "status": "active"},
        )
    ]


def test_emit_presence_noop_when_tenant_unset(monkeypatch):
    monkeypatch.delenv("BYTEDESK_REALTIME_TENANT_ID", raising=False)
    calls = _capture(monkeypatch)
    bridge.emit_presence("ag_1", "idle")
    assert calls == []


def test_emit_goal_change_publishes_when_tenant_set(monkeypatch):
    monkeypatch.setenv("BYTEDESK_REALTIME_TENANT_ID", "tenant-abc")
    calls = _capture(monkeypatch)
    bridge.emit_goal_change(
        {
            "type": "goal.changed",
            "change": "created",
            "goalId": "goal_1",
            "status": "open",
            "activationState": "ready",
            "readinessKind": "immediate",
            "targetKind": "organization",
            "targetId": "omnigent",
            "targetLabel": "Organization",
            "ownerAgentId": None,
            "priority": 3,
            "updatedAt": 100,
            "occurredAt": 101,
        }
    )
    assert calls == [
        (
            "office:goals:tenant-abc",
            {
                "type": "goal.changed",
                "change": "created",
                "goalId": "goal_1",
                "status": "open",
                "activationState": "ready",
                "readinessKind": "immediate",
                "targetKind": "organization",
                "targetId": "omnigent",
                "targetLabel": "Organization",
                "ownerAgentId": None,
                "priority": 3,
                "updatedAt": 100,
                "occurredAt": 101,
            },
        )
    ]


def test_emit_entity_change_publishes_when_tenant_set(monkeypatch):
    monkeypatch.setenv("BYTEDESK_REALTIME_TENANT_ID", "tenant-abc")
    calls = _capture(monkeypatch)
    bridge.emit_entity_change(
        {
            "type": "entity.changed",
            "entity": "budget",
            "op": "updated",
            "id": "org:omnigent",
            "occurredAt": 101,
        }
    )
    assert calls == [
        (
            "office:goals:tenant-abc",
            {
                "type": "entity.changed",
                "entity": "budget",
                "op": "updated",
                "id": "org:omnigent",
                "occurredAt": 101,
            },
        )
    ]


def test_emit_entity_change_noop_when_tenant_unset(monkeypatch):
    monkeypatch.delenv("BYTEDESK_REALTIME_TENANT_ID", raising=False)
    calls = _capture(monkeypatch)
    bridge.emit_entity_change({"entity": "template", "op": "created", "id": "t_1"})
    assert calls == []


def test_emit_goal_change_noop_when_tenant_unset(monkeypatch):
    monkeypatch.delenv("BYTEDESK_REALTIME_TENANT_ID", raising=False)
    calls = _capture(monkeypatch)
    bridge.emit_goal_change(
        {
            "change": "updated",
            "goalId": "goal_1",
            "status": "open",
            "activationState": "ready",
            "readinessKind": "immediate",
            "targetKind": "organization",
            "targetId": "omnigent",
            "targetLabel": "Organization",
            "ownerAgentId": None,
            "priority": 3,
            "updatedAt": 100,
            "occurredAt": 101,
        }
    )
    assert calls == []


def test_emit_goal_planning_publishes_when_tenant_set(monkeypatch):
    monkeypatch.setenv("BYTEDESK_REALTIME_TENANT_ID", "tenant-abc")
    calls = _capture(monkeypatch)
    bridge.emit_goal_planning(
        {
            "type": "goal.planning.started",
            "planningSessionId": "conv_1",
            "targetKind": "department",
            "targetId": "Operations",
            "targetLabel": "Operations",
            "sourceIds": ["jira"],
            "occurredAt": 101,
            "draftReady": False,
        }
    )
    assert calls == [
        (
            "office:goals:tenant-abc",
            {
                "type": "goal.planning.started",
                "planningSessionId": "conv_1",
                "targetKind": "department",
                "targetId": "Operations",
                "targetLabel": "Operations",
                "sourceIds": ["jira"],
                "occurredAt": 101,
                "draftReady": False,
            },
        )
    ]


def test_emit_goal_planning_noop_when_tenant_unset(monkeypatch):
    monkeypatch.delenv("BYTEDESK_REALTIME_TENANT_ID", raising=False)
    calls = _capture(monkeypatch)
    bridge.emit_goal_planning(
        {
            "type": "goal.planning.committed",
            "planningSessionId": "conv_1",
            "targetKind": "organization",
            "targetId": "omnigent",
            "targetLabel": "Organization",
            "sourceIds": [],
            "occurredAt": 101,
            "goalId": "goal_1",
        }
    )
    assert calls == []


def test_install_subscribes_to_agent_store_events(monkeypatch):
    from omnigent.stores.agent_store import events as agent_events

    monkeypatch.setenv("BYTEDESK_REALTIME_TENANT_ID", "t")
    monkeypatch.setattr(bridge, "_INSTALLED", False, raising=False)
    calls = _capture(monkeypatch)
    agent_events.reset_for_test()
    try:
        assert bridge.install() is True
        agent_events.emit("created", "ag_9")
    finally:
        agent_events.reset_for_test()
        bridge._INSTALLED = False

    assert calls == [
        (
            "office:agents:t",
            {"type": "roster.changed", "action": "created", "agentId": "ag_9"},
        )
    ]


def test_install_is_idempotent(monkeypatch):
    from omnigent.stores.agent_store import events as agent_events

    monkeypatch.setenv("BYTEDESK_REALTIME_TENANT_ID", "t")
    monkeypatch.setattr(bridge, "_INSTALLED", False, raising=False)
    calls = _capture(monkeypatch)
    agent_events.reset_for_test()
    try:
        assert bridge.install() is True
        assert bridge.install() is False
        agent_events.emit("updated", "ag_u")
    finally:
        agent_events.reset_for_test()
        bridge._INSTALLED = False

    assert calls == [
        (
            "office:agents:t",
            {"type": "roster.changed", "action": "updated", "agentId": "ag_u"},
        )
    ]
