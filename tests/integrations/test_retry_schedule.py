"""Tests for deterministic third-party integration retry schedules."""

from __future__ import annotations

from bytedesk_omnigent.integration_retry import (
    IntegrationRetryPolicy,
    compile_retry_schedule,
)


def test_compile_retry_schedule_honors_retry_after_and_attempt_budget() -> None:
    policy = IntegrationRetryPolicy(
        max_attempts=4,
        base_delay_seconds=30,
        max_delay_seconds=300,
        retryable_statuses=frozenset({408, 429, 500, 502, 503, 504}),
    )

    plan = compile_retry_schedule(
        provider="slack",
        operation="chat.postMessage",
        first_attempt_at=1_700_000_000,
        policy=policy,
        observed_status=429,
        retry_after_seconds=90,
    )

    assert plan.provider == "slack"
    assert plan.operation == "chat.postMessage"
    assert plan.retryable is True
    assert plan.terminal_reason is None
    assert [step.attempt for step in plan.steps] == [2, 3, 4]
    assert [step.delay_seconds for step in plan.steps] == [90, 60, 120]
    assert [step.scheduled_at for step in plan.steps] == [
        1_700_000_090,
        1_700_000_150,
        1_700_000_270,
    ]
    assert plan.idempotency_key == "integration-retry:slack:chat.postMessage:1700000000"


def test_compile_retry_schedule_marks_terminal_failures() -> None:
    plan = compile_retry_schedule(
        provider="stripe",
        operation="customers.create",
        first_attempt_at=1_700_000_000,
        observed_status=401,
    )

    assert plan.retryable is False
    assert plan.steps == []
    assert plan.terminal_reason == "status 401 is not retryable"
