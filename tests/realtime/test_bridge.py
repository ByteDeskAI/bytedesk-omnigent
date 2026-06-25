"""BDP-2301 — agent roster bridge: emit gating + store-method wrappers + install."""

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


def test_wrap_create_calls_orig_then_emits(monkeypatch):
    monkeypatch.setenv("BYTEDESK_REALTIME_TENANT_ID", "t")
    calls = _capture(monkeypatch)

    def orig(self, agent_id, name, bundle_location, description=None):
        return ("created", agent_id)

    result = bridge.wrap_create(orig)(object(), "ag_9", "name", "loc")
    assert result == ("created", "ag_9")
    assert calls == [
        (
            "office:agents:t",
            {"type": "roster.changed", "action": "created", "agentId": "ag_9"},
        )
    ]


def test_wrap_update_skips_emit_when_no_row(monkeypatch):
    monkeypatch.setenv("BYTEDESK_REALTIME_TENANT_ID", "t")
    calls = _capture(monkeypatch)
    assert (
        bridge.wrap_update(lambda self, agent_id, bundle_location: None)(object(), "ag_x", "loc")
        is None
    )
    assert calls == []


def test_wrap_update_emits_when_row_updated(monkeypatch):
    monkeypatch.setenv("BYTEDESK_REALTIME_TENANT_ID", "t")
    calls = _capture(monkeypatch)
    bridge.wrap_update(lambda self, agent_id, bundle_location: "agent")(object(), "ag_u", "loc")
    assert calls == [
        (
            "office:agents:t",
            {"type": "roster.changed", "action": "updated", "agentId": "ag_u"},
        )
    ]


def test_wrap_update_passes_expected_version(monkeypatch):
    monkeypatch.setenv("BYTEDESK_REALTIME_TENANT_ID", "t")
    calls = _capture(monkeypatch)
    seen = {}

    def orig(self, agent_id, bundle_location, *, expected_version=None):
        seen["expected_version"] = expected_version
        return "agent"

    result = bridge.wrap_update(orig)(
        object(),
        "ag_u",
        "loc",
        expected_version=7,
    )

    assert result == "agent"
    assert seen == {"expected_version": 7}
    assert calls == [
        (
            "office:agents:t",
            {"type": "roster.changed", "action": "updated", "agentId": "ag_u"},
        )
    ]


def test_wrap_delete_emits_only_when_existed(monkeypatch):
    monkeypatch.setenv("BYTEDESK_REALTIME_TENANT_ID", "t")
    calls = _capture(monkeypatch)
    assert bridge.wrap_delete(lambda self, agent_id: True)(object(), "ag_d") is True
    assert bridge.wrap_delete(lambda self, agent_id: False)(object(), "ag_e") is False
    assert calls == [
        (
            "office:agents:t",
            {"type": "roster.changed", "action": "deleted", "agentId": "ag_d"},
        )
    ]


def test_install_wraps_store_and_is_idempotent(monkeypatch):
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore

    saved = (
        SqlAlchemyAgentStore.create,
        SqlAlchemyAgentStore.update,
        SqlAlchemyAgentStore.delete,
    )
    monkeypatch.setattr(bridge, "_INSTALLED", False, raising=False)
    try:
        assert bridge.install() is True
        assert SqlAlchemyAgentStore.create is not saved[0]  # wrapped
        assert bridge.install() is False  # idempotent — second call no-ops
    finally:
        (
            SqlAlchemyAgentStore.create,
            SqlAlchemyAgentStore.update,
            SqlAlchemyAgentStore.delete,
        ) = saved
        bridge._INSTALLED = False
