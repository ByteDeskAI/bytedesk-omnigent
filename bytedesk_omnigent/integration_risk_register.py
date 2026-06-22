"""Deterministic risk registers for integration capability rollout.

The integration catalog and verification matrix explain what to build and how to
prove rollout readiness. This module adds the operator/security view: predictable
risk items and controls that must be addressed before autonomous agents are
allowed to operate inside third-party systems.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from bytedesk_omnigent.integration_capabilities import (
    CapabilityCategory,
    get_integration_capability,
)
from bytedesk_omnigent.integration_verification_matrix import (
    compile_integration_verification_matrix,
)

RiskSeverity = Literal["medium", "high", "critical"]


@dataclass(frozen=True)
class IntegrationRisk:
    """One deterministic rollout risk and its required controls."""

    id: str
    severity: RiskSeverity
    title: str
    controls: tuple[str, ...]
    blocked_until_evidence: str

    def to_dict(self) -> dict:
        data = asdict(self)
        data["controls"] = list(self.controls)
        return data


_EXTERNAL_BASE_RISKS: tuple[IntegrationRisk, ...] = (
    IntegrationRisk(
        id="credential-exposure",
        severity="high",
        title="Provider credentials or OAuth tokens could leak into logs or prompts",
        controls=(
            "store provider credentials only in an approved secret boundary",
            "redact tokens and provider secrets from task evidence and operator logs",
            "document re-authorization or credential rotation before activation",
        ),
        blocked_until_evidence="auth-boundary",
    ),
    IntegrationRisk(
        id="unauthorized-provider-write",
        severity="high",
        title="Agent could mutate provider state without explicit approval",
        controls=(
            "separate read-only tools from write-capable tools",
            "require policy approval evidence for every high-risk provider mutation",
            "record denied approvals without making provider-side changes",
        ),
        blocked_until_evidence="policy-approval",
    ),
    IntegrationRisk(
        id="event-spoofing",
        severity="medium",
        title="Untrusted events could masquerade as provider-originated work",
        controls=(
            "verify webhook signatures or documented provider authenticity signals",
            "preserve external event ids and tenant/workspace routing keys",
            "fail closed when event type or source cannot be normalized",
        ),
        blocked_until_evidence="ingress-normalization",
    ),
)

_INTERNAL_HARNESS_RISKS: tuple[IntegrationRisk, ...] = (
    IntegrationRisk(
        id="workflow-drift",
        severity="medium",
        title="Workflow harness phases could drift from the declared blueprint",
        controls=(
            "use stable phase node ids in every compiled workflow",
            "declare typed inputs and outputs for each phase",
            "reject blueprints whose phase graph changes during a deterministic run",
        ),
        blocked_until_evidence="workflow-determinism",
    ),
    IntegrationRisk(
        id="phase-evidence-gap",
        severity="medium",
        title="Terminal workflow phases could finish without auditable evidence",
        controls=(
            "capture completion evidence for every terminal phase",
            "correlate phase evidence with the source task and assigned agent",
            "surface missing evidence before marking a workflow ready",
        ),
        blocked_until_evidence="workflow-determinism",
    ),
    IntegrationRisk(
        id="operator-blindness",
        severity="medium",
        title="Operators could lose visibility into autonomous workflow outcomes",
        controls=(
            "publish safe status summaries without secrets",
            "correlate task id, workflow phase id, and agent id",
            "emit success and failure outcome records",
        ),
        blocked_until_evidence="observability-evidence",
    ),
)

_CATEGORY_RISKS: dict[CapabilityCategory, IntegrationRisk] = {
    "communication": IntegrationRisk(
        id="message-spam-or-leakage",
        severity="medium",
        title="Outbound collaboration messages could spam channels or expose context",
        controls=(
            "rate-limit outbound messages per workspace or channel",
            "tie every reply to a source thread, task, or approval request",
            "sanitize operator-visible summaries before posting to humans",
        ),
        blocked_until_evidence="communication-loop",
    ),
    "project_management": IntegrationRisk(
        id="source-of-truth-conflict",
        severity="medium",
        title="Agent status write-back could conflict with human project tracking",
        controls=(
            "map external lifecycle states to Omnigent tasks deterministically",
            "preserve human author attribution on comments and checklist updates",
            "avoid status write-back that fights the external source of truth",
        ),
        blocked_until_evidence="work-item-lifecycle",
    ),
    "knowledge": IntegrationRisk(
        id="overbroad-knowledge-access",
        severity="high",
        title="Agent could read or update knowledge beyond the selected scope",
        controls=(
            "constrain reads to selected files, pages, databases, or mailboxes",
            "require approval for broad search or mailbox access",
            "include provenance on every knowledge write",
        ),
        blocked_until_evidence="knowledge-scope-control",
    ),
    "developer": IntegrationRisk(
        id="review-bypass",
        severity="high",
        title="Engineering automation could bypass review-safe delivery paths",
        controls=(
            "use least-privilege repository installation permissions",
            "route code and CI mutations through reviewable pull requests",
            "preserve failed checks and review comments as task evidence",
        ),
        blocked_until_evidence="developer-change-safety",
    ),
    "crm_support": IntegrationRisk(
        id="customer-facing-misfire",
        severity="high",
        title="Agent could publish an incorrect customer-facing reply or record update",
        controls=(
            "require approval for public customer replies until quality gates pass",
            "capture before and after summaries for record updates",
            "preserve customer context and consent in support-to-sales handoffs",
        ),
        blocked_until_evidence="customer-record-safety",
    ),
    "commerce_billing": IntegrationRisk(
        id="revenue-side-effect",
        severity="critical",
        title="Agent could trigger a revenue-affecting side effect",
        controls=(
            "separate read-only revenue context from payment mutations",
            "require explicit approval for refunds, cancellations, and billing writes",
            "link financial alerts back to source provider objects",
        ),
        blocked_until_evidence="revenue-mutation-safety",
    ),
    "workflow_harness": _INTERNAL_HARNESS_RISKS[0],
}


def compile_integration_risk_register(slug: str) -> dict | None:
    """Return a JSON-ready rollout risk register for a catalog capability."""

    capability = get_integration_capability(slug)
    matrix = compile_integration_verification_matrix(slug)
    if capability is None or matrix is None:
        return None

    risk_tier = matrix["risk_tier"]
    if risk_tier == "internal_harness":
        risks = _INTERNAL_HARNESS_RISKS
    else:
        risks = (*_EXTERNAL_BASE_RISKS, _CATEGORY_RISKS[capability.category])

    risk_dicts = [risk.to_dict() for risk in risks]
    return {
        "object": "integration_risk_register",
        "capability_slug": capability.slug,
        "capability_name": capability.name,
        "category": capability.category,
        "risk_tier": risk_tier,
        "requires_policy_approval": risk_tier == "external_write",
        "risks": risk_dicts,
        "minimum_control_count": sum(len(risk["controls"]) for risk in risk_dicts),
    }
