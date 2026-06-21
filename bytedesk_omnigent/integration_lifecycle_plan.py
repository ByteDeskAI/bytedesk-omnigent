"""Deterministic lifecycle plans for integration capability management.

The catalog says which integrations matter; verification matrices say how to
prove them. This module gives operators and ByteDesk Platform a deterministic
state machine for moving one catalog capability from selection to activation,
suspension, or retirement without relying on ad-hoc project notes.
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
class LifecycleStage:
    """One lifecycle state for a catalog-backed integration capability."""

    id: str
    title: str
    owner: str
    required_evidence: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["required_evidence"] = list(self.required_evidence)
        return data


_BASE_SELECTION = LifecycleStage(
    id="catalog-selected",
    title="Capability selected from catalog",
    owner="product-operator",
    required_evidence=(
        "capability slug resolves in the integration catalog",
        "business case is accepted for the target tenant or marketplace segment",
        "implementation owner and support owner are named",
    ),
)

_OAUTH_AUTHORIZED = LifecycleStage(
    id="oauth-authorized",
    title="Provider authorization boundary established",
    owner="platform-engineer",
    required_evidence=(
        "requested scopes match the catalog contract",
        "credential storage path is secret-manager backed or explicitly inert",
        "reauthorization and token revocation paths are documented",
    ),
)

_WEBHOOK_BOUND = LifecycleStage(
    id="webhook-bound",
    title="External event ingress is bound",
    owner="integration-engineer",
    required_evidence=(
        "external workspace, tenant, or account id maps to an Omnigent tenant",
        "event ids become deterministic idempotency keys",
        "unsupported events fail closed with an auditable reason",
    ),
)

_BLUEPRINT_BOUND = LifecycleStage(
    id="blueprint-bound",
    title="Workflow blueprint is bound",
    owner="agent-operator",
    required_evidence=(
        "phase graph has stable node ids",
        "typed inputs and outputs are declared for every phase",
        "default agent roles are mapped to Omnigent capabilities",
    ),
)

_POLICY_APPROVED = LifecycleStage(
    id="policy-approved",
    title="Write actions are policy approved",
    owner="risk-operator",
    required_evidence=(
        "read-only actions are separated from write actions",
        "write actions are mapped to approval policy",
        "denied approvals leave no provider-side mutation",
    ),
)

_SANDBOX_VALIDATED = LifecycleStage(
    id="sandbox-validated",
    title="Sandbox behavior is validated",
    owner="qa-operator",
    required_evidence=(
        "happy path dry-run produces task and outcome evidence",
        "duplicate delivery returns the same normalized outcome",
        "terminal failure includes a safe operator-facing reason",
    ),
)

_PILOT_ENABLED = LifecycleStage(
    id="pilot-enabled",
    title="Tenant pilot is enabled",
    owner="customer-success",
    required_evidence=(
        "pilot tenant and allowed agent roster are explicit",
        "success metric and rollback trigger are documented",
        "operator dashboard can identify source provider objects",
    ),
)

_PRODUCTION_ACTIVE = LifecycleStage(
    id="production-active",
    title="Production activation is approved",
    owner="platform-operator",
    required_evidence=(
        "verification gates are satisfied",
        "support escalation path is live",
        "disablement path preserves historical evidence",
    ),
)

_SUSPENDED = LifecycleStage(
    id="suspended",
    title="Capability is suspended",
    owner="platform-operator",
    required_evidence=(
        "new external ingress is disabled or ignored",
        "in-flight tasks are drained, cancelled, or reassigned",
        "tenant-facing reason is recorded without secrets",
    ),
)

_RETIRED = LifecycleStage(
    id="retired",
    title="Capability is retired",
    owner="product-operator",
    required_evidence=(
        "provider subscriptions and credentials are revoked",
        "historical task and outcome evidence remains queryable",
        "replacement or migration guidance is published if needed",
    ),
)

_ALLOWED_TRANSITIONS = {
    "catalog-selected": ["oauth-authorized", "blueprint-bound", "suspended"],
    "oauth-authorized": ["webhook-bound", "policy-approved", "suspended"],
    "webhook-bound": ["policy-approved", "sandbox-validated", "suspended"],
    "blueprint-bound": ["sandbox-validated", "suspended"],
    "policy-approved": ["pilot-enabled"],
    "sandbox-validated": ["pilot-enabled", "suspended"],
    "pilot-enabled": ["production-active", "suspended"],
    "production-active": ["suspended", "retired"],
    "suspended": ["pilot-enabled", "retired"],
    "retired": [],
}


def compile_integration_lifecycle_plan(slug: str) -> dict[str, object] | None:
    """Return a JSON-ready lifecycle state machine for one catalog capability."""

    capability = get_integration_capability(slug)
    if capability is None:
        return None

    risk_tier = _risk_tier(capability.category, capability.required_scopes)
    stages = _stages_for_risk_tier(risk_tier)
    stage_ids = {stage.id for stage in stages}
    transitions = {
        stage.id: [target for target in _ALLOWED_TRANSITIONS[stage.id] if target in stage_ids]
        for stage in stages
    }

    return {
        "object": "integration_capability_lifecycle_plan",
        "capability_slug": capability.slug,
        "capability_name": capability.name,
        "category": capability.category,
        "risk_tier": risk_tier,
        "auth_model": capability.auth_model,
        "required_scopes": list(capability.required_scopes),
        "stages": [stage.to_dict() for stage in stages],
        "allowed_transitions": transitions,
        "terminal_states": ["production-active", "suspended", "retired"],
        "minimum_evidence_count": sum(len(stage.required_evidence) for stage in stages),
    }


def _stages_for_risk_tier(risk_tier: IntegrationRiskTier) -> tuple[LifecycleStage, ...]:
    if risk_tier == "internal_harness":
        return (
            _BASE_SELECTION,
            _BLUEPRINT_BOUND,
            _SANDBOX_VALIDATED,
            _PILOT_ENABLED,
            _PRODUCTION_ACTIVE,
            _SUSPENDED,
            _RETIRED,
        )

    stages: tuple[LifecycleStage, ...] = (
        _BASE_SELECTION,
        _OAUTH_AUTHORIZED,
        _WEBHOOK_BOUND,
        _SANDBOX_VALIDATED,
        _PILOT_ENABLED,
        _PRODUCTION_ACTIVE,
        _SUSPENDED,
        _RETIRED,
    )
    if risk_tier == "external_write":
        return (
            _BASE_SELECTION,
            _OAUTH_AUTHORIZED,
            _WEBHOOK_BOUND,
            _POLICY_APPROVED,
            _PILOT_ENABLED,
            _PRODUCTION_ACTIVE,
            _SUSPENDED,
            _RETIRED,
        )
    return stages


def _risk_tier(
    category: CapabilityCategory, required_scopes: tuple[str, ...]
) -> IntegrationRiskTier:
    if category == "workflow_harness":
        return "internal_harness"
    if any("write" in scope.lower() or scope.endswith(".write") for scope in required_scopes):
        return "external_write"
    return "external_read"
