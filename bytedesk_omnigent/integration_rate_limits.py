"""Deterministic rate-limit plans for third-party integration agents.

Autonomous agents that write into Slack, Linear, Notion, CRMs, ticketing tools,
or ByteDesk Platform need a small, explicit envelope before they call external
APIs: retryable statuses, bounded attempts, backoff, and an idempotency key shape.
This module compiles that envelope without importing any provider SDKs or secrets.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_DEFAULT_RETRY_STATUSES = (429, 500, 502, 503, 504)


@dataclass(frozen=True)
class IntegrationRateLimitProfile:
    """Rate-limit/retry policy for a third-party service family."""

    service: str
    window_seconds: int = 60
    max_attempts: int = 4
    retry_statuses: tuple[int, ...] = _DEFAULT_RETRY_STATUSES
    initial_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0


def compile_rate_limit_plan(
    *,
    service: str,
    operation: str,
    idempotency_fields: list[str] | tuple[str, ...],
    profile: IntegrationRateLimitProfile | None = None,
) -> dict[str, Any]:
    """Compile a deterministic external-API retry envelope for an agent.

    The returned dict is intentionally JSON-serializable so it can be embedded in
    agent manifests, integration handoff packages, ByteDesk task briefs, or PR
    bodies without pulling in a service SDK. ``idempotency_fields`` are rendered
    as placeholders in a stable key shape, making retries safe for side-effecting
    writes such as ``chat.postMessage`` or ``contacts.upsert``.
    """
    normalized_service = _slug(_require_text("service", service))
    normalized_operation = _require_text("operation", operation)
    fields = tuple(_require_text("idempotency field", field) for field in idempotency_fields)
    if not fields:
        raise ValueError("idempotency_fields must contain at least one field")

    selected = profile or IntegrationRateLimitProfile(service=normalized_service)
    retry_statuses = sorted({int(status) for status in selected.retry_statuses})
    idempotency_key = ":".join(
        (normalized_service, normalized_operation, *(f"{{{field}}}" for field in fields))
    )

    return {
        "service": normalized_service,
        "operation": normalized_operation,
        "window_seconds": int(selected.window_seconds),
        "max_attempts": int(selected.max_attempts),
        "retry_statuses": retry_statuses,
        "backoff": {
            "strategy": "exponential_jitter",
            "initial_delay_seconds": float(selected.initial_delay_seconds),
            "max_delay_seconds": float(selected.max_delay_seconds),
        },
        "idempotency_key": idempotency_key,
        "agent_instructions": [
            "Respect Retry-After when present before applying local backoff.",
            "Use idempotency_key for retries so external side effects do not double-fire.",
            "Escalate to human approval after max_attempts is exhausted.",
        ],
    }


def _require_text(label: str, value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label} must be non-empty")
    return cleaned


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
