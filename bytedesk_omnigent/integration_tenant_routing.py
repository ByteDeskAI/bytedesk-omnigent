"""Tenant routing manifests for integration capability rollout.

External connectors are only useful to autonomous agents once provider workspaces,
actors, and events map back to the correct ByteDesk/Omnigent tenant boundary.
This module compiles deterministic, secret-free routing manifests from the static
integration catalog so platform surfaces can preview the tenancy contract before
credentials or webhooks exist.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from bytedesk_omnigent.integration_capabilities import (
    CapabilityCategory,
    get_integration_capability,
)

RoutingMode = Literal["external_workspace_mapping", "internal_workflow_namespace"]
RiskTier = Literal["internal_harness", "external_read", "external_write"]


@dataclass(frozen=True)
class TenantSignalRoute:
    """One default event-to-queue route for a capability category."""

    source_event: str
    target_queue: str
    coordination_goal: str

    def to_dict(self) -> dict:
        return asdict(self)


_BASE_EXTERNAL_ISOLATION_CHECKS: tuple[str, ...] = (
    "provider workspace id is bound to exactly one tenant id",
    "provider actor id is preserved separately from Omnigent agent id",
    "cross-tenant replay with the same provider event id is rejected",
)

_WORKFLOW_ISOLATION_CHECKS: tuple[str, ...] = (
    "tenant namespace cannot read another tenant's workflow runs",
    "workflow blueprint id is stable across retries",
    "phase evidence is scoped to the originating workflow run id",
)

_CATEGORY_ROUTES: dict[CapabilityCategory, tuple[TenantSignalRoute, ...]] = {
    "communication": (
        TenantSignalRoute(
            source_event="communication.message.received",
            target_queue="omnigent.signals.inbox",
            coordination_goal="route human context to the responsible agent",
        ),
        TenantSignalRoute(
            source_event="communication.approval_requested",
            target_queue="omnigent.human_approval",
            coordination_goal="pause autonomous write until approved",
        ),
    ),
    "project_management": (
        TenantSignalRoute(
            source_event="work_item.created",
            target_queue="omnigent.tasks.intake",
            coordination_goal="materialize external work as an Omnigent Task",
        ),
        TenantSignalRoute(
            source_event="work_item.status_changed",
            target_queue="omnigent.tasks.lifecycle",
            coordination_goal="synchronize external lifecycle changes without ownership fights",
        ),
    ),
    "knowledge": (
        TenantSignalRoute(
            source_event="knowledge.document.updated",
            target_queue="omnigent.memory.indexing",
            coordination_goal="refresh scoped agent knowledge with provenance",
        ),
        TenantSignalRoute(
            source_event="knowledge.write.requested",
            target_queue="omnigent.policy.approvals",
            coordination_goal="gate broad or persistent knowledge mutations",
        ),
    ),
    "developer": (
        TenantSignalRoute(
            source_event="developer.issue.assigned",
            target_queue="omnigent.engineering.intake",
            coordination_goal="route engineering work to an appropriate coding agent",
        ),
        TenantSignalRoute(
            source_event="developer.check.failed",
            target_queue="omnigent.engineering.repair",
            coordination_goal="launch reviewable autonomous repair loops",
        ),
    ),
    "crm_support": (
        TenantSignalRoute(
            source_event="customer_record.updated",
            target_queue="omnigent.customer_context",
            coordination_goal="refresh customer context for support and revenue agents",
        ),
        TenantSignalRoute(
            source_event="support.reply.requested",
            target_queue="omnigent.human_approval",
            coordination_goal="hold public customer responses until approved",
        ),
    ),
    "commerce_billing": (
        TenantSignalRoute(
            source_event="revenue.event.detected",
            target_queue="omnigent.revenue_ops",
            coordination_goal="route revenue-risk signals to specialist agents",
        ),
        TenantSignalRoute(
            source_event="billing.mutation.requested",
            target_queue="omnigent.human_approval",
            coordination_goal="require approval before financial side effects",
        ),
    ),
    "workflow_harness": (
        TenantSignalRoute(
            source_event="workflow.phase.completed",
            target_queue="omnigent.workflow.harness",
            coordination_goal="advance deterministic phase graph",
        ),
        TenantSignalRoute(
            source_event="workflow.phase.failed",
            target_queue="omnigent.workflow.recovery",
            coordination_goal="route failed deterministic phases to recovery policy",
        ),
    ),
}


def compile_integration_tenant_routing_manifest(slug: str) -> dict | None:
    """Return a JSON-ready tenant routing manifest for a catalog capability."""

    capability = get_integration_capability(slug)
    if capability is None:
        return None

    risk_tier = _risk_tier(capability.category, capability.required_scopes)
    routing_mode: RoutingMode = (
        "internal_workflow_namespace"
        if capability.category == "workflow_harness"
        else "external_workspace_mapping"
    )
    workspace_identity_fields = (
        ("tenant_id", "workflow_blueprint_id", "workflow_run_id")
        if routing_mode == "internal_workflow_namespace"
        else ("tenant_id", "provider_workspace_id", "provider_actor_id")
    )
    isolation_checks = (
        _WORKFLOW_ISOLATION_CHECKS
        if routing_mode == "internal_workflow_namespace"
        else _BASE_EXTERNAL_ISOLATION_CHECKS
    )

    return {
        "object": "integration_tenant_routing_manifest",
        "capability_slug": capability.slug,
        "capability_name": capability.name,
        "category": capability.category,
        "routing_mode": routing_mode,
        "workspace_identity_fields": list(workspace_identity_fields),
        "default_signal_routes": [
            route.to_dict() for route in _CATEGORY_ROUTES[capability.category]
        ],
        "isolation_checks": list(isolation_checks),
        "audit_tags": [
            f"capability:{capability.slug}",
            f"category:{capability.category}",
            f"risk:{risk_tier}",
        ],
    }


def _risk_tier(category: CapabilityCategory, required_scopes: tuple[str, ...]) -> RiskTier:
    if category == "workflow_harness":
        return "internal_harness"
    if any("write" in scope.lower() or scope.endswith(".write") for scope in required_scopes):
        return "external_write"
    return "external_read"
