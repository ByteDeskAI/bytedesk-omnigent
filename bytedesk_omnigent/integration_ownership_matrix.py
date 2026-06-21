"""Deterministic ownership matrices for integration capability rollout.

Integration verification gates define what evidence must exist. This companion
module defines who must provide, review, and operate that evidence before a
connector or workflow harness is enabled for a tenant. The output is pure and
JSON-ready so platform UI, planning agents, and autonomous loops can request a
safe handoff plan without touching live credentials or provider APIs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from bytedesk_omnigent.integration_capabilities import (
    CapabilityCategory,
    get_integration_capability,
)
from bytedesk_omnigent.integration_verification_matrix import (
    IntegrationRiskTier,
    compile_integration_verification_matrix,
)

RoleId = Literal[
    "business-owner",
    "provider-admin",
    "security-reviewer",
    "platform-operator",
    "workflow-owner",
    "knowledge-steward",
]


@dataclass(frozen=True)
class OwnershipLane:
    """One actor lane required to safely launch an integration capability."""

    id: RoleId
    title: str
    responsibilities: tuple[str, ...]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["responsibilities"] = list(self.responsibilities)
        return data


_BUSINESS_OWNER = OwnershipLane(
    id="business-owner",
    title="Business owner",
    responsibilities=(
        "Confirm the integration business case and success metric.",
        "Name the escalation owner for blocked or failed autonomous work.",
    ),
)
_PROVIDER_ADMIN = OwnershipLane(
    id="provider-admin",
    title="Provider administrator",
    responsibilities=(
        "Create or approve the provider application, webhook, or bot install.",
        "Confirm tenant/workspace boundaries before credentials are activated.",
    ),
)
_SECURITY_REVIEWER = OwnershipLane(
    id="security-reviewer",
    title="Security reviewer",
    responsibilities=(
        "Validate least-privilege scopes and credential storage boundaries.",
        "Approve policy gates for provider-side mutations and customer-visible writes.",
    ),
)
_PLATFORM_OPERATOR = OwnershipLane(
    id="platform-operator",
    title="Omnigent platform operator",
    responsibilities=(
        "Bind normalized events, tasks, and outcome evidence to the tenant workspace.",
        "Own rollback, disablement, and observable health checks after activation.",
    ),
)
_WORKFLOW_OWNER = OwnershipLane(
    id="workflow-owner",
    title="Workflow owner",
    responsibilities=(
        "Publish deterministic phases, inputs, outputs, and completion evidence.",
        "Confirm each phase maps to an authorized agent role and policy boundary.",
    ),
)
_KNOWLEDGE_STEWARD = OwnershipLane(
    id="knowledge-steward",
    title="Knowledge steward",
    responsibilities=(
        "Approve the pages, files, databases, or mailboxes agents may read.",
        "Review provenance requirements for agent-authored knowledge updates.",
    ),
)

_EXTERNAL_PARTICIPANTS: dict[str, str] = {
    "slack-command-center": "Slack workspace admin",
    "notion-knowledge-operator": "Notion workspace owner",
    "trello-task-bridge": "Trello board administrator",
    "github-engineering-copilot": "GitHub organization owner",
    "linear-jira-work-intake": "Linear or Jira administrator",
    "google-workspace-operator": "Google Workspace administrator",
    "hubspot-salesforce-crm-agent": "CRM platform administrator",
    "zendesk-intercom-support-desk": "Support desk administrator",
    "stripe-shopify-revenue-ops": "Commerce or billing administrator",
}

_CATEGORY_EXTRA_LANES: dict[CapabilityCategory, tuple[OwnershipLane, ...]] = {
    "communication": (),
    "project_management": (),
    "knowledge": (_KNOWLEDGE_STEWARD,),
    "developer": (),
    "crm_support": (),
    "commerce_billing": (),
    "workflow_harness": (_WORKFLOW_OWNER,),
}


def compile_integration_ownership_matrix(slug: str) -> dict | None:
    """Return a JSON-ready owner/approver handoff matrix for one capability."""

    capability = get_integration_capability(slug)
    verification = compile_integration_verification_matrix(slug)
    if capability is None or verification is None:
        return None

    risk_tier = verification["risk_tier"]
    lanes = _lanes_for(capability.category, risk_tier)
    return {
        "capability_slug": capability.slug,
        "capability_name": capability.name,
        "category": capability.category,
        "risk_tier": risk_tier,
        "required_approvers": _required_approvers(risk_tier),
        "external_participants": _external_participants(capability.slug),
        "lanes": [lane.to_dict() for lane in lanes],
        "handoff_checklist": _handoff_checklist(
            capability.category,
            risk_tier,
            capability.required_scopes,
        ),
    }


def _lanes_for(
    category: CapabilityCategory, risk_tier: IntegrationRiskTier
) -> tuple[OwnershipLane, ...]:
    if risk_tier == "internal_harness":
        return (_WORKFLOW_OWNER, _PLATFORM_OPERATOR, _SECURITY_REVIEWER)

    lanes = [_BUSINESS_OWNER, _PROVIDER_ADMIN, _SECURITY_REVIEWER, _PLATFORM_OPERATOR]
    for lane in _CATEGORY_EXTRA_LANES[category]:
        if lane not in lanes:
            lanes.insert(-1, lane)
    return tuple(lanes)


def _required_approvers(risk_tier: IntegrationRiskTier) -> list[str]:
    if risk_tier == "internal_harness":
        return ["workflow_owner", "platform_operator"]
    if risk_tier == "external_write":
        return ["workspace_admin", "security_reviewer", "business_owner"]
    return ["workspace_admin", "security_reviewer"]


def _external_participants(slug: str) -> list[str]:
    participant = _EXTERNAL_PARTICIPANTS.get(slug)
    return [participant] if participant is not None else []


def _handoff_checklist(
    category: CapabilityCategory,
    risk_tier: IntegrationRiskTier,
    required_scopes: tuple[str, ...],
) -> list[str]:
    if risk_tier == "internal_harness":
        return [
            "Publish the deterministic workflow blueprint and phase graph.",
            "Bind every phase to an owning agent role and completion evidence.",
            "Record rollback and disablement owner before activation.",
        ]

    checklist = [
        "Confirm the business owner, provider admin, security reviewer, and Omnigent operator.",
        f"Validate least-privilege OAuth scopes: {', '.join(required_scopes) or 'none'}.",
        "Document webhook or subscription teardown before first tenant activation.",
    ]
    if category == "knowledge":
        checklist.append("Record the approved knowledge boundary and provenance policy.")
    if risk_tier == "external_write":
        checklist.append("Bind provider-side writes to approval policy and outcome evidence.")
    return checklist
