"""Tests for the inbound realtime emit + channel contract (ADR-0155, BDP-2563)."""
from __future__ import annotations

from bytedesk_omnigent.inbound.store import InboundEventRecord
from bytedesk_omnigent.realtime import bridge
from bytedesk_omnigent.realtime.channel import (
    inbound_event_changed,
    office_inbound_channel,
)


def _record(status="received") -> InboundEventRecord:
    return InboundEventRecord(
        idempotency_key="github:pr:repo#1:abc", source="github", type="pull_request.merged",
        status=status, occurred_at=100, received_at=100, tenant_id=None, event_id="d1",
        raw_payload={}, normalized={}, headers={}, attempts=0, error=None,
        created_at=100, updated_at=100)


def test_channel_string_and_delta_shape() -> None:
    assert office_inbound_channel("t-1") == "office:inbound:t-1"
    delta = inbound_event_changed(idempotency_key="k", source="github",
                                  event_type="pull_request.merged", status="received",
                                  occurred_at=1, received_at=2)
    assert delta["type"] == "inbound.event" and delta["source"] == "github"
    assert delta["eventType"] == "pull_request.merged" and delta["duplicate"] is False


def test_emit_dormant_without_tenant(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(bridge.config, "tenant_id", lambda: None)
    monkeypatch.setattr(bridge, "publish", lambda ch, payload: calls.append((ch, payload)))
    bridge.emit_inbound_event(_record(), inserted=True)
    assert calls == []  # no Redis publish without a configured tenant


def test_emit_publishes_with_tenant(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(bridge.config, "tenant_id", lambda: "tenant-x")
    monkeypatch.setattr(bridge, "publish", lambda ch, payload: calls.append((ch, payload)))
    bridge.emit_inbound_event(_record(), inserted=True)
    assert len(calls) == 1
    channel, payload = calls[0]
    assert channel == "office:inbound:tenant-x" and payload["type"] == "inbound.event"


def test_emit_marks_duplicate(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(bridge.config, "tenant_id", lambda: "tenant-x")
    monkeypatch.setattr(bridge, "publish", lambda ch, payload: calls.append((ch, payload)))
    bridge.emit_inbound_event(_record(), inserted=False)
    assert calls[0][1]["duplicate"] is True and calls[0][1]["status"] == "duplicate"
