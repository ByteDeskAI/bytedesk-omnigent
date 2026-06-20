"""Deterministic retry plans for third-party integration operations.

When an autonomous agent calls Slack, Stripe, Jira, Notion, or another SaaS API,
the runtime needs a repeatable answer to: retry, when, and with what key?  This
module is intentionally pure so workflow harnesses and ByteDesk Platform bridges
can preview the same schedule before handing work to a queue or dead-letter lane.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class IntegrationRetryPolicy:
    """Retry budget and backoff limits for an integration operation."""

    max_attempts: int = 3
    base_delay_seconds: int = 30
    max_delay_seconds: int = 300
    retryable_statuses: frozenset[int] = DEFAULT_RETRYABLE_STATUSES


@dataclass(frozen=True)
class RetryStep:
    """One future attempt in a deterministic integration retry schedule."""

    attempt: int
    delay_seconds: int
    scheduled_at: int


@dataclass(frozen=True)
class IntegrationRetryPlan:
    """Compiled retry/no-retry decision for an integration operation."""

    provider: str
    operation: str
    retryable: bool
    idempotency_key: str
    steps: list[RetryStep]
    terminal_reason: str | None = None


def compile_retry_schedule(
    *,
    provider: str,
    operation: str,
    first_attempt_at: int,
    observed_status: int,
    policy: IntegrationRetryPolicy | None = None,
    retry_after_seconds: int | None = None,
) -> IntegrationRetryPlan:
    """Compile a deterministic retry schedule for a failed integration call.

    ``first_attempt_at`` and all returned timestamps are epoch seconds.  If the
    provider supplies ``Retry-After`` on a retryable failure, that value governs
    the first retry only; remaining retries use capped exponential backoff from
    ``base_delay_seconds``.  The idempotency key is stable for the logical first
    attempt, allowing queue workers and SaaS adapters to dedupe retries.
    """
    retry_policy = policy or IntegrationRetryPolicy()
    idempotency_key = _idempotency_key(provider, operation, first_attempt_at)

    if retry_policy.max_attempts <= 1:
        return IntegrationRetryPlan(
            provider=provider,
            operation=operation,
            retryable=False,
            idempotency_key=idempotency_key,
            steps=[],
            terminal_reason="retry budget exhausted",
        )

    if observed_status not in retry_policy.retryable_statuses:
        return IntegrationRetryPlan(
            provider=provider,
            operation=operation,
            retryable=False,
            idempotency_key=idempotency_key,
            steps=[],
            terminal_reason=f"status {observed_status} is not retryable",
        )

    steps: list[RetryStep] = []
    cursor = first_attempt_at
    for attempt in range(2, retry_policy.max_attempts + 1):
        delay = _delay_for_attempt(
            attempt=attempt,
            policy=retry_policy,
            retry_after_seconds=retry_after_seconds,
        )
        cursor += delay
        steps.append(RetryStep(attempt=attempt, delay_seconds=delay, scheduled_at=cursor))

    return IntegrationRetryPlan(
        provider=provider,
        operation=operation,
        retryable=True,
        idempotency_key=idempotency_key,
        steps=steps,
    )


def _delay_for_attempt(
    *,
    attempt: int,
    policy: IntegrationRetryPolicy,
    retry_after_seconds: int | None,
) -> int:
    if attempt == 2 and retry_after_seconds is not None:
        return min(max(0, retry_after_seconds), policy.max_delay_seconds)
    backoff_power = attempt - 2
    return min(policy.base_delay_seconds * (2**backoff_power), policy.max_delay_seconds)


def _idempotency_key(provider: str, operation: str, first_attempt_at: int) -> str:
    return f"integration-retry:{provider}:{operation}:{first_attempt_at}"


__all__ = [
    "IntegrationRetryPlan",
    "IntegrationRetryPolicy",
    "RetryStep",
    "compile_retry_schedule",
]
