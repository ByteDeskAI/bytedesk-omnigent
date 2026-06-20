"""Tests for deterministic third-party integration rate-limit plans."""
from __future__ import annotations

import pytest

from bytedesk_omnigent.integration_rate_limits import (
    IntegrationRateLimitProfile,
    compile_rate_limit_plan,
)


def test_compile_rate_limit_plan_normalizes_service_defaults() -> None:
    plan = compile_rate_limit_plan(
        service="Slack",
        operation="chat.postMessage",
        idempotency_fields=["workspace_id", "channel_id", "client_msg_id"],
    )

    assert plan["service"] == "slack"
    assert plan["operation"] == "chat.postMessage"
    assert plan["window_seconds"] == 60
    assert plan["max_attempts"] == 4
    assert plan["retry_statuses"] == [429, 500, 502, 503, 504]
    assert plan["backoff"] == {
        "strategy": "exponential_jitter",
        "initial_delay_seconds": 1.0,
        "max_delay_seconds": 30.0,
    }
    assert (
        plan["idempotency_key"]
        == "slack:chat.postMessage:{workspace_id}:{channel_id}:{client_msg_id}"
    )
    assert plan["agent_instructions"] == [
        "Respect Retry-After when present before applying local backoff.",
        "Use idempotency_key for retries so external side effects do not double-fire.",
        "Escalate to human approval after max_attempts is exhausted.",
    ]


def test_compile_rate_limit_plan_accepts_custom_profile_and_sorts_retry_statuses() -> None:
    profile = IntegrationRateLimitProfile(
        service="Acme CRM",
        window_seconds=300,
        max_attempts=2,
        retry_statuses=(503, 429, 500),
        initial_delay_seconds=2.5,
        max_delay_seconds=45.0,
    )

    plan = compile_rate_limit_plan(
        service="Acme CRM",
        operation="contacts.upsert",
        idempotency_fields=["tenant_id", "external_contact_id"],
        profile=profile,
    )

    assert plan["service"] == "acme-crm"
    assert plan["window_seconds"] == 300
    assert plan["max_attempts"] == 2
    assert plan["retry_statuses"] == [429, 500, 503]
    assert plan["idempotency_key"] == "acme-crm:contacts.upsert:{tenant_id}:{external_contact_id}"


@pytest.mark.parametrize("value", ["", "   "])
def test_compile_rate_limit_plan_rejects_blank_required_values(value: str) -> None:
    with pytest.raises(ValueError):
        compile_rate_limit_plan(
            service=value,
            operation="contacts.upsert",
            idempotency_fields=["tenant_id"],
        )

    with pytest.raises(ValueError):
        compile_rate_limit_plan(
            service="linear",
            operation=value,
            idempotency_fields=["tenant_id"],
        )


def test_compile_rate_limit_plan_requires_idempotency_fields() -> None:
    with pytest.raises(ValueError):
        compile_rate_limit_plan(
            service="linear",
            operation="issue.create",
            idempotency_fields=[],
        )
