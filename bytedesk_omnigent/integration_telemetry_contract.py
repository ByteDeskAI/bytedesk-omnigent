"""Deterministic telemetry contracts for integration capability rollout.

Verification matrices say what evidence must exist before an integration is
production-ready. Telemetry contracts say which trace fields, events, and health
metrics operators must emit so autonomous agents, ByteDesk Platform dashboards,
and customer success teams can observe that evidence without inspecting secrets
or provider payloads.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from bytedesk_omnigent.integration_capabilities import (
    CapabilityCategory,
    get_integration_capability,
)

IntegrationRiskTier = Literal["internal_harness", "external_read", "external_write"]


@dataclass(frozen=True)
class TelemetryEventContract:
    """One normalized event an integration adapter must emit."""

    event: str
    required_fields: tuple[str, ...]
    purpose: str

    def to_dict(self) -> dict:
        data = asdict(self)
        data["required_fields"] = list(self.required_fields)
        return data


@dataclass(frozen=True)
class HealthIndicator:
    """One operator-facing metric target for an integration capability."""

    metric: str
    target: str
    owner: str = "integration-operator"

    def to_dict(self) -> dict:
        return asdict(self)


_BASE_EXTERNAL_FIELDS: tuple[str, ...] = (
    "tenant_id",
    "capability_slug",
    "provider_workspace_id",
    "provider_event_id",
    "task_id",
    "agent_id",
)

_WORKFLOW_FIELDS: tuple[str, ...] = (
    "tenant_id",
    "capability_slug",
    "workflow_id",
    "phase_id",
    "task_id",
    "agent_id",
    "evidence_id",
)

_EXTERNAL_READ_EVENTS: tuple[TelemetryEventContract, ...] = (
    TelemetryEventContract(
        event="integration.ingress.received",
        required_fields=(*_BASE_EXTERNAL_FIELDS, "received_at"),
        purpose=(
            "Record every provider delivery before normalization for replay and support "
            "traceability."
        ),
    ),
    TelemetryEventContract(
        event="integration.ingress.normalized",
        required_fields=(*_BASE_EXTERNAL_FIELDS, "normalized_signal_type"),
        purpose="Prove external provider events became deterministic Omnigent signals.",
    ),
    TelemetryEventContract(
        event="integration.read.completed",
        required_fields=(*_BASE_EXTERNAL_FIELDS, "read_operation", "result_count"),
        purpose="Track least-privilege read activity without exposing provider payload contents.",
    ),
)

_EXTERNAL_WRITE_EVENTS: tuple[TelemetryEventContract, ...] = (
    TelemetryEventContract(
        event="integration.ingress.received",
        required_fields=(*_BASE_EXTERNAL_FIELDS, "received_at"),
        purpose=(
            "Record every provider delivery before normalization for replay and support "
            "traceability."
        ),
    ),
    TelemetryEventContract(
        event="integration.ingress.normalized",
        required_fields=(*_BASE_EXTERNAL_FIELDS, "normalized_signal_type"),
        purpose="Prove external provider events became deterministic Omnigent signals.",
    ),
    TelemetryEventContract(
        event="integration.action.policy_checked",
        required_fields=(
            "tenant_id",
            "capability_slug",
            "task_id",
            "agent_id",
            "action_id",
            "approval_strategy",
            "policy_decision",
        ),
        purpose=(
            "Show mutating provider actions were evaluated against approval and autonomy "
            "policy before dispatch."
        ),
    ),
    TelemetryEventContract(
        event="integration.action.dispatched",
        required_fields=(*_BASE_EXTERNAL_FIELDS, "action_id", "provider_object_id"),
        purpose=(
            "Correlate approved outbound mutations to provider-side objects and Omnigent "
            "tasks."
        ),
    ),
    TelemetryEventContract(
        event="integration.action.failed",
        required_fields=(*_BASE_EXTERNAL_FIELDS, "action_id", "failure_class"),
        purpose=(
            "Make failed writes auditable and safe to route into retries or escalation "
            "workflows."
        ),
    ),
)

_WORKFLOW_EVENTS: tuple[TelemetryEventContract, ...] = (
    TelemetryEventContract(
        event="integration.workflow.phase_started",
        required_fields=(*_WORKFLOW_FIELDS, "started_at"),
        purpose="Capture deterministic workflow phase starts for Archon-style harness replay.",
    ),
    TelemetryEventContract(
        event="integration.workflow.phase_completed",
        required_fields=(*_WORKFLOW_FIELDS, "completed_at", "output_ref"),
        purpose="Prove each terminal phase produced typed completion evidence.",
    ),
    TelemetryEventContract(
        event="integration.workflow.phase_failed",
        required_fields=(*_WORKFLOW_FIELDS, "failure_class", "retryable"),
        purpose=(
            "Route deterministic harness failures into retries, rollbacks, or human "
            "escalation."
        ),
    ),
)


def compile_integration_telemetry_contract(slug: str) -> dict | None:
    """Return a JSON-ready telemetry contract for one catalog capability."""

    capability = get_integration_capability(slug)
    if capability is None:
        return None

    risk_tier = _risk_tier(capability.category, capability.required_scopes)
    metric_prefix = f"omnigent.integration.{_metric_slug(capability.slug)}"
    if risk_tier == "internal_harness":
        events = _WORKFLOW_EVENTS
        required_trace_fields = _WORKFLOW_FIELDS
        health_indicators = (
            HealthIndicator(
                metric=f"{metric_prefix}.phase_success_rate",
                target=">= 99% over 24h",
            ),
            HealthIndicator(
                metric=f"{metric_prefix}.phase_retry_rate",
                target="<= 2% over 24h",
            ),
        )
    elif risk_tier == "external_write":
        events = _EXTERNAL_WRITE_EVENTS
        required_trace_fields = _BASE_EXTERNAL_FIELDS
        health_indicators = _external_health_indicators(metric_prefix)
    else:
        events = _EXTERNAL_READ_EVENTS
        required_trace_fields = _BASE_EXTERNAL_FIELDS
        health_indicators = _external_health_indicators(metric_prefix)

    return {
        "object": "integration_telemetry_contract",
        "capability_slug": capability.slug,
        "capability_name": capability.name,
        "category": capability.category,
        "risk_tier": risk_tier,
        "metric_prefix": metric_prefix,
        "required_trace_fields": list(required_trace_fields),
        "events": [event.to_dict() for event in events],
        "health_indicators": [indicator.to_dict() for indicator in health_indicators],
    }


def _external_health_indicators(metric_prefix: str) -> tuple[HealthIndicator, ...]:
    return (
        HealthIndicator(
            metric=f"{metric_prefix}.normalization_success_rate",
            target=">= 99% over 24h",
        ),
        HealthIndicator(
            metric=f"{metric_prefix}.duplicate_delivery_rate",
            target="<= 1% over 24h",
        ),
        HealthIndicator(
            metric=f"{metric_prefix}.policy_denial_rate",
            target="operator-reviewed daily",
        ),
    )


def _risk_tier(
    category: CapabilityCategory, required_scopes: tuple[str, ...]
) -> IntegrationRiskTier:
    if category == "workflow_harness":
        return "internal_harness"
    if any("write" in scope.lower() or scope.endswith(".write") for scope in required_scopes):
        return "external_write"
    return "external_read"


def _metric_slug(slug: str) -> str:
    return slug.replace("-", "_")
