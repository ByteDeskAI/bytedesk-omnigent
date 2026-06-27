"""Deterministic verification matrices for integration capability rollout.

The integration catalog explains what should be built. This module turns one
catalog entry into the acceptance gates an autonomous loop, platform UI, or
operator should require before calling that integration production-ready.
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
class VerificationGate:
    """One deterministic evidence gate for an integration capability."""

    id: str
    title: str
    required_evidence: tuple[str, ...]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["required_evidence"] = list(self.required_evidence)
        return data


_BASE_GATES: tuple[VerificationGate, ...] = (
    VerificationGate(
        id="catalog-contract",
        title="Catalog contract is explicit and stable",
        required_evidence=(
            "capability slug resolves in the integration catalog",
            "auth model and required scopes are documented",
            "business case and future unlocks are present",
        ),
    ),
    VerificationGate(
        id="auth-boundary",
        title="Authorization boundary is least-privilege",
        required_evidence=(
            "requested scopes match the catalog entry",
            "credential storage path is secret-manager backed or explicitly inert",
            "token refresh or re-authorization path is documented",
        ),
    ),
    VerificationGate(
        id="ingress-normalization",
        title="External events normalize into Omnigent signals",
        required_evidence=(
            "external event id is preserved for traceability",
            "tenant or workspace id is retained for routing",
            "unsupported event types fail closed with an auditable reason",
        ),
    ),
    VerificationGate(
        id="idempotency-replay",
        title="Replay and retry behavior is deterministic",
        required_evidence=(
            "idempotency key is derived from stable provider identifiers",
            "duplicate delivery returns the same normalized outcome",
            "retry schedule and terminal failure behavior are declared",
        ),
    ),
    VerificationGate(
        id="policy-approval",
        title="Mutating actions are policy-gated",
        required_evidence=(
            "read-only actions are separated from write actions",
            "high-risk writes name the required approval strategy",
            "denied approvals leave no provider-side mutation",
        ),
    ),
    VerificationGate(
        id="observability-evidence",
        title="Execution leaves operator-visible evidence",
        required_evidence=(
            "task id, provider object id, and agent id are correlated",
            "success and failure paths produce outcome records",
            "operator-facing status is safe to expose without secrets",
        ),
    ),
    VerificationGate(
        id="rollback-readiness",
        title="Rollback and disablement are defined",
        required_evidence=(
            "connector can be disabled without deleting historical evidence",
            "webhook or subscription teardown steps are documented",
            "manual recovery owner and escalation path are named",
        ),
    ),
)

_CATEGORY_GATES: dict[CapabilityCategory, VerificationGate] = {
    "communication": VerificationGate(
        id="communication-loop",
        title="Human collaboration loop is bounded and auditable",
        required_evidence=(
            "agent replies can be correlated to source thread or channel",
            "approval or escalation prompts include actor and task context",
            "outbound messages are rate-limited per workspace or channel",
        ),
    ),
    "project_management": VerificationGate(
        id="work-item-lifecycle",
        title="External work item lifecycle maps to Omnigent Tasks",
        required_evidence=(
            "create, update, block, and complete states map deterministically",
            "comments or checklist updates preserve author attribution",
            "status write-back cannot fight the human source of truth",
        ),
    ),
    "knowledge": VerificationGate(
        id="knowledge-scope-control",
        title="Knowledge access is scoped and provenance-preserving",
        required_evidence=(
            "read set is constrained to selected files, pages, or databases",
            "writes include provenance back to the source task and agent",
            "broad search or mailbox access requires an explicit approval gate",
        ),
    ),
    "developer": VerificationGate(
        id="developer-change-safety",
        title="Engineering automation is review-safe",
        required_evidence=(
            "repository permissions are least-privilege and installation-scoped",
            "code or CI mutations route through reviewable pull requests",
            "failed checks and review comments are preserved as task evidence",
        ),
    ),
    "crm_support": VerificationGate(
        id="customer-record-safety",
        title="Customer-facing records are protected",
        required_evidence=(
            "public customer replies require approval until quality gates pass",
            "record updates capture before and after summaries",
            "support-to-sales handoffs preserve customer context and consent",
        ),
    ),
    "commerce_billing": VerificationGate(
        id="revenue-mutation-safety",
        title="Revenue-affecting actions are risk-tiered",
        required_evidence=(
            "refund, cancellation, and billing mutations require explicit approval",
            "read-only revenue context is separated from payment-side effects",
            "financial anomaly alerts include source object links",
        ),
    ),
    "workflow_harness": VerificationGate(
        id="workflow-determinism",
        title="Workflow harness phases are deterministic",
        required_evidence=(
            "phase graph uses stable node ids",
            "typed inputs and outputs are declared per phase",
            "completion evidence is captured for every terminal phase",
        ),
    ),
}


def _risk_tier(
    category: CapabilityCategory, required_scopes: tuple[str, ...]
) -> IntegrationRiskTier:
    if category == "workflow_harness":
        return "internal_harness"
    mutating_scope_markers = ("write", "update", "insert", "delete", "send")
    if any(
        any(marker in scope.lower() for marker in mutating_scope_markers)
        or scope.endswith(".write")
        for scope in required_scopes
    ):
        return "external_write"
    return "external_read"


def compile_integration_verification_matrix(slug: str) -> dict | None:
    """Return a JSON-ready rollout verification matrix for a catalog capability."""

    capability = get_integration_capability(slug)
    if capability is None:
        return None

    gates = (*_BASE_GATES, _CATEGORY_GATES[capability.category])
    gate_dicts = [gate.to_dict() for gate in gates]
    return {
        "capability_slug": capability.slug,
        "capability_name": capability.name,
        "category": capability.category,
        "risk_tier": _risk_tier(capability.category, capability.required_scopes),
        "auth_model": capability.auth_model,
        "required_scopes": list(capability.required_scopes),
        "gates": gate_dicts,
        "minimum_required_evidence_count": sum(
            len(gate["required_evidence"]) for gate in gate_dicts
        ),
    }
