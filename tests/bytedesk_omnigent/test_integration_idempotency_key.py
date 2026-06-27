from __future__ import annotations

from bytedesk_omnigent.integration_idempotency_key import (
    build_integration_idempotency_key,
)


def test_prefers_provider_delivery_header_without_echoing_secret_headers() -> None:
    key = build_integration_idempotency_key(
        source="GitHub",
        event="pull_request.opened",
        headers={
            "X-GitHub-Delivery": "abc-123",
            "Authorization": "Bearer should-not-appear",
            "X-Hub-Signature-256": "sha256=secret-signature",
        },
        payload={"action": "opened", "number": 42},
    )

    assert key.scope == "integration:github:pull_request.opened"
    assert key.key == "delivery:abc-123"
    assert key.source == "github"
    assert key.event == "pull_request.opened"
    assert key.strategy == "provider_delivery_id"
    assert "secret" not in key.key
    assert "Bearer" not in key.key


def test_uses_known_payload_identifier_when_headers_have_no_delivery_id() -> None:
    key = build_integration_idempotency_key(
        source="stripe",
        event="invoice.paid",
        headers={},
        payload={"id": "evt_123", "object": "event", "data": {"object": {"id": "in_123"}}},
    )

    assert key.scope == "integration:stripe:invoice.paid"
    assert key.key == "payload:id:evt_123"
    assert key.strategy == "payload_identifier"


def test_canonical_payload_hash_is_stable_for_reordered_json() -> None:
    first = build_integration_idempotency_key(
        source="Microsoft Teams",
        event="message.created",
        headers={},
        payload={"b": [2, 1], "a": {"nested": True}},
    )
    second = build_integration_idempotency_key(
        source="microsoft-teams",
        event="message.created",
        headers={},
        payload={"a": {"nested": True}, "b": [2, 1]},
    )

    assert first.scope == "integration:microsoft-teams:message.created"
    assert second.scope == first.scope
    assert first.key == second.key
    assert first.key.startswith("payload_sha256:")
    assert first.strategy == "canonical_payload_hash"
