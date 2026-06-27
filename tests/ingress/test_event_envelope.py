"""Tests for deterministic connected-app ingress event envelopes."""
from __future__ import annotations

from bytedesk_omnigent.integration_event_envelope import (
    IntegrationEventEnvelope,
    build_integration_event_envelope,
)


def test_build_integration_event_envelope_preserves_payload_and_safe_headers() -> None:
    envelope = build_integration_event_envelope(
        source="GitHub",
        match_key="issues.opened",
        payload={"issue": {"number": 42, "title": "Ship it"}},
        headers={
            "X-GitHub-Delivery": "evt-123",
            "X-GitHub-Hook-ID": "hook-456",
            "X-Hub-Signature-256": "sha256=secret",
            "Authorization": "Bearer secret",
            "Content-Type": "application/json",
        },
        received_at=1_717_171_717,
    )

    assert envelope == IntegrationEventEnvelope(
        schema="omnigent.integration_event.v1",
        source="github",
        event="issues.opened",
        received_at=1_717_171_717,
        payload={"issue": {"number": 42, "title": "Ship it"}},
        metadata={
            "content_type": "application/json",
            "delivery_id": "evt-123",
            "hook_id": "hook-456",
        },
    )
    assert envelope.to_payload()["schema"] == "omnigent.integration_event.v1"
    assert "X-Hub-Signature-256" not in str(envelope.to_payload())
    assert "Authorization" not in str(envelope.to_payload())


def test_build_integration_event_envelope_slugs_source_and_defaults_empty_payload() -> None:
    envelope = build_integration_event_envelope(
        source="Microsoft Teams",
        match_key="message.created",
        payload=None,
        headers={"X-Request-ID": "req-789"},
        received_at=22,
    )

    assert envelope.source == "microsoft-teams"
    assert envelope.payload == {}
    assert envelope.metadata == {"delivery_id": "req-789"}
    assert envelope.to_payload() == {
        "schema": "omnigent.integration_event.v1",
        "source": "microsoft-teams",
        "event": "message.created",
        "received_at": 22,
        "payload": {},
        "metadata": {"delivery_id": "req-789"},
    }
