"""Deterministic SLO profiles for integration capability launches.

The catalog and verification matrix describe what an integration is and how it
should be proven. This module adds the operator-facing reliability promise that
ByteDesk Platform can show before enabling a connector: availability target,
sync freshness, action latency, measurement events, and freeze thresholds.
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
class IntegrationSloProfile:
    """JSON-ready reliability profile for one integration capability."""

    object: str
    capability_slug: str
    capability_name: str
    category: CapabilityCategory
    risk_tier: IntegrationRiskTier
    availability_target: str
    sync_freshness_target: str
    action_latency_target: str
    measurement_events: tuple[str, ...]
    category_controls: tuple[str, ...]
    operator_promises: tuple[str, ...]
    error_budget_policy: dict[str, str]

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in ("measurement_events", "category_controls", "operator_promises"):
            data[key] = list(data[key])
        return data


@dataclass(frozen=True)
class _RiskSloDefaults:
    availability_target: str
    sync_freshness_target: str
    action_latency_target: str
    measurement_events: tuple[str, ...]
    operator_promises: tuple[str, ...]


_RISK_DEFAULTS: dict[IntegrationRiskTier, _RiskSloDefaults] = {
    "internal_harness": _RiskSloDefaults(
        availability_target="99.0% monthly",
        sync_freshness_target="phase state updates visible within 30 seconds",
        action_latency_target="95% of local harness actions complete within 30 seconds",
        measurement_events=(
            "workflow.phase.started",
            "workflow.phase.completed",
            "workflow.phase.failed",
        ),
        operator_promises=(
            "Workflow phases expose deterministic state transitions and terminal evidence.",
            "Harness failures preserve typed phase inputs and outputs for replay.",
        ),
    ),
    "external_read": _RiskSloDefaults(
        availability_target="99.3% monthly",
        sync_freshness_target="provider read snapshots refreshed within 5 minutes",
        action_latency_target="95% of read operations complete within 45 seconds",
        measurement_events=(
            "integration.read.started",
            "integration.read.completed",
            "integration.read.failed",
        ),
        operator_promises=(
            "Provider reads are retried without mutating external systems.",
            "Stale snapshots are marked before agents use them for autonomous decisions.",
        ),
    ),
    "external_write": _RiskSloDefaults(
        availability_target="99.5% monthly",
        sync_freshness_target="provider events normalized within 2 minutes",
        action_latency_target="95% of approved writes complete within 60 seconds",
        measurement_events=(
            "integration.event.received",
            "integration.event.normalized",
            "integration.action.approved",
            "integration.action.completed",
            "integration.action.failed",
        ),
        operator_promises=(
            "Approved writes either complete with provider evidence or fail closed.",
            "Write-side outages freeze new mutations before evidence quality degrades.",
        ),
    ),
}

_CATEGORY_CONTROLS: dict[CapabilityCategory, tuple[str, ...]] = {
    "communication": (
        "Outbound collaboration messages are rate-limited and auditable.",
        "Escalation prompts retain channel, thread, actor, and task context.",
    ),
    "project_management": (
        "Work item sync preserves external source-of-truth status.",
        "Status write-back conflicts are surfaced before autonomous retries continue.",
    ),
    "knowledge": (
        "Knowledge reads and writes retain source document provenance.",
        "Broad search surfaces are freshness-marked before agent use.",
    ),
    "developer": (
        "Pull request and CI automation keeps review-safe evidence.",
        "Repository-side mutations stay scoped to installation permissions.",
    ),
    "crm_support": (
        "Customer-visible responses stay approval-gated until quality targets pass.",
        "Record mutations capture before and after summaries for audit.",
    ),
    "commerce_billing": (
        "Revenue-affecting mutations freeze when error budget thresholds are crossed.",
        "Financial object evidence is retained for every autonomous recommendation.",
    ),
    "workflow_harness": (
        "Workflow templates declare deterministic phase ids and terminal evidence.",
        "Replay uses captured phase inputs instead of live external side effects.",
    ),
}


def compile_integration_slo_profile(slug: str) -> dict | None:
    """Return a JSON-ready SLO profile for one catalog capability."""

    capability = get_integration_capability(slug)
    if capability is None:
        return None

    risk_tier = _risk_tier(capability.category, capability.required_scopes)
    defaults = _RISK_DEFAULTS[risk_tier]
    profile = IntegrationSloProfile(
        object="integration_slo_profile",
        capability_slug=capability.slug,
        capability_name=capability.name,
        category=capability.category,
        risk_tier=risk_tier,
        availability_target=defaults.availability_target,
        sync_freshness_target=defaults.sync_freshness_target,
        action_latency_target=defaults.action_latency_target,
        measurement_events=defaults.measurement_events,
        category_controls=_CATEGORY_CONTROLS[capability.category],
        operator_promises=defaults.operator_promises,
        error_budget_policy={
            "freeze_threshold": "25% of monthly budget remaining",
            "page_threshold": "50% of monthly budget remaining",
            "review_cadence": "weekly during pilot, monthly after production acceptance",
        },
    )
    return profile.to_dict()


def _risk_tier(
    category: CapabilityCategory, required_scopes: tuple[str, ...]
) -> IntegrationRiskTier:
    if category == "workflow_harness":
        return "internal_harness"
    if any("write" in scope.lower() or scope.endswith(".write") for scope in required_scopes):
        return "external_write"
    return "external_read"
