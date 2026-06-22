"""Deterministic agent coordination topologies for integration capabilities.

The integration catalog explains what to integrate and the verification matrix
explains how to prove rollout safety. This module adds the agent-management layer:
which Omnigent roles should coordinate one capability, where approvals sit, and
which handoffs must be explicit before a connector or deterministic workflow
harness is tenant-ready.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from bytedesk_omnigent.integration_capabilities import (
    CapabilityCategory,
    get_integration_capability,
)
from bytedesk_omnigent.integration_verification_matrix import (
    compile_integration_verification_matrix,
)


@dataclass(frozen=True)
class CoordinationAgentRole:
    """One managed Omnigent role in an integration topology."""

    id: str
    title: str
    responsibility: str
    required_capabilities: tuple[str, ...]
    required_scopes: tuple[str, ...] = ()
    approval_authority: bool = False

    def to_dict(self) -> dict:
        data = asdict(self)
        data["required_capabilities"] = list(self.required_capabilities)
        data["required_scopes"] = list(self.required_scopes)
        return data


@dataclass(frozen=True)
class CoordinationHandoffEdge:
    """A deterministic handoff between managed Omnigent roles."""

    source: str
    target: str
    trigger: str

    def to_dict(self) -> dict:
        return {"from": self.source, "to": self.target, "trigger": self.trigger}


_WORKFLOW_ROLES: tuple[CoordinationAgentRole, ...] = (
    CoordinationAgentRole(
        id="workflow_orchestrator",
        title="Workflow orchestrator",
        responsibility=(
            "Compile the deterministic workflow graph, assign phase owners, and "
            "route typed inputs without provider-side mutation."
        ),
        required_capabilities=("workflow_harness.plan", "workflow_harness.route"),
    ),
    CoordinationAgentRole(
        id="phase_executor",
        title="Phase executor",
        responsibility=(
            "Run one idempotent workflow phase and emit completion evidence for "
            "the orchestrator before downstream phases proceed."
        ),
        required_capabilities=("workflow_harness.execute_phase",),
    ),
    CoordinationAgentRole(
        id="verification_reviewer",
        title="Verification reviewer",
        responsibility=(
            "Validate phase outputs against declared gates and prevent silent "
            "success when terminal evidence is missing."
        ),
        required_capabilities=("workflow_harness.verify", "evidence.review"),
    ),
    CoordinationAgentRole(
        id="recovery_coordinator",
        title="Recovery coordinator",
        responsibility=(
            "Choose retry, rollback, or human escalation when a deterministic "
            "phase fails closed."
        ),
        required_capabilities=("workflow_harness.recover", "incident.escalate"),
        approval_authority=True,
    ),
)

_WORKFLOW_HANDOFFS: tuple[CoordinationHandoffEdge, ...] = (
    CoordinationHandoffEdge(
        source="workflow_orchestrator",
        target="phase_executor",
        trigger="phase inputs validated and predecessor evidence is complete",
    ),
    CoordinationHandoffEdge(
        source="phase_executor",
        target="verification_reviewer",
        trigger="phase completed, failed, or emitted partial evidence",
    ),
    CoordinationHandoffEdge(
        source="verification_reviewer",
        target="recovery_coordinator",
        trigger="phase failed without terminal evidence",
    ),
)

_PROVIDER_HANDOFFS: tuple[CoordinationHandoffEdge, ...] = (
    CoordinationHandoffEdge(
        source="integration_orchestrator",
        target="connector_operator",
        trigger="validated task requires provider context or action dispatch",
    ),
    CoordinationHandoffEdge(
        source="connector_operator",
        target="policy_approver",
        trigger=(
            "provider mutation, public message, record update, or broad data read "
            "requested"
        ),
    ),
    CoordinationHandoffEdge(
        source="policy_approver",
        target="evidence_auditor",
        trigger="approved or denied action needs durable outcome evidence",
    ),
)

_CATEGORY_ESCALATIONS: dict[CapabilityCategory, tuple[str, ...]] = {
    "communication": (
        "public channel reply requested without explicit task context",
        "human approval thread is missing or stale",
    ),
    "project_management": (
        "external status transition conflicts with human source of truth",
        "work item owner or priority cannot be resolved",
    ),
    "knowledge": (
        "broad document or mailbox read requested",
        "write target lacks provenance or selected workspace binding",
    ),
    "developer": (
        "repository permission expansion requested",
        "code mutation would bypass reviewable pull request flow",
    ),
    "crm_support": (
        "public customer response requested before quality gate approval",
        "customer record update lacks before/after summary",
    ),
    "commerce_billing": (
        "refund, cancellation, or billing mutation requested",
        "financial anomaly lacks source object correlation",
    ),
    "workflow_harness": (
        "phase failed without terminal evidence",
        "downstream phase requested before predecessor evidence is verified",
    ),
}


def compile_integration_coordination_topology(slug: str) -> dict | None:
    """Return a JSON-ready managed-agent topology for a catalog capability."""

    capability = get_integration_capability(slug)
    if capability is None:
        return None

    matrix = compile_integration_verification_matrix(slug)
    if matrix is None:  # Defensive: should not happen for known catalog slugs.
        return None

    risk_tier = matrix["risk_tier"]
    if risk_tier == "internal_harness":
        roles = _WORKFLOW_ROLES
        handoffs = _WORKFLOW_HANDOFFS
    else:
        roles = _provider_roles(
            category=capability.category,
            required_scopes=capability.required_scopes,
            external_write=risk_tier == "external_write",
        )
        handoffs = _PROVIDER_HANDOFFS

    return {
        "object": "integration_coordination_topology",
        "capability_slug": capability.slug,
        "capability_name": capability.name,
        "category": capability.category,
        "risk_tier": risk_tier,
        "agent_roles": [role.to_dict() for role in roles],
        "handoff_edges": [edge.to_dict() for edge in handoffs],
        "escalation_triggers": list(_CATEGORY_ESCALATIONS[capability.category]),
    }


def _provider_roles(
    *,
    category: CapabilityCategory,
    required_scopes: tuple[str, ...],
    external_write: bool,
) -> tuple[CoordinationAgentRole, ...]:
    capabilities_prefix = category.replace("_", ".")
    operator_capabilities = (
        f"{capabilities_prefix}.read",
        f"{capabilities_prefix}.normalize_event",
    )
    if external_write:
        operator_capabilities = (*operator_capabilities, f"{capabilities_prefix}.draft_action")

    return (
        CoordinationAgentRole(
            id="integration_orchestrator",
            title="Integration orchestrator",
            responsibility=(
                "Own task intake, tenant routing, and delegation boundaries for "
                "the connected application."
            ),
            required_capabilities=("task.route", "agent.delegate", "tenant.scope"),
        ),
        CoordinationAgentRole(
            id="connector_operator",
            title="Connector operator",
            responsibility=(
                "Normalize provider context, prepare idempotent actions, and keep "
                "provider-specific details out of orchestration policy."
            ),
            required_capabilities=operator_capabilities,
            required_scopes=required_scopes,
        ),
        CoordinationAgentRole(
            id="policy_approver",
            title="Policy approver",
            responsibility=(
                "Approve or deny risky provider reads and mutations before the "
                "connector operator can dispatch them."
            ),
            required_capabilities=("policy.evaluate", "approval.decide"),
            approval_authority=True,
        ),
        CoordinationAgentRole(
            id="evidence_auditor",
            title="Evidence auditor",
            responsibility=(
                "Record safe outcome evidence and escalate missing, conflicting, "
                "or secret-bearing integration traces."
            ),
            required_capabilities=("evidence.record", "audit.escalate"),
        ),
    )
