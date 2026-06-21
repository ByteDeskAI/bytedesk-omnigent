"""Deterministic onboarding questionnaires for integration capability activation.

The integration catalog says what a connector can unlock. This compiler turns one
catalog entry into product/operator questions that must be answered before a
customer workspace starts OAuth, webhook binding, or an internal workflow-harness
rollout. It is pure and credential-free so ByteDesk Platform can render it before
any provider authorization flow begins.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from bytedesk_omnigent.integration_capabilities import (
    CapabilityCategory,
    get_integration_capability,
)
from bytedesk_omnigent.integration_verification_matrix import IntegrationRiskTier


@dataclass(frozen=True)
class OnboardingQuestionSection:
    """A grouped set of pre-activation questions for one integration."""

    id: str
    title: str
    questions: tuple[str, ...]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["questions"] = list(self.questions)
        return data


_BASE_SECTION = OnboardingQuestionSection(
    id="workspace-intent",
    title="Workspace intent and success owner",
    questions=(
        "Which tenant, workspace, or team will activate this capability first?",
        "Who is the business owner accountable for approving the pilot outcome?",
        "Which agent workflow or customer promise should this integration improve first?",
    ),
)

_AUTH_SECTION = OnboardingQuestionSection(
    id="auth-boundary",
    title="Authorization boundary and scope approval",
    questions=(
        "Which provider account or installation will grant access for the pilot?",
        "Are the requested catalog scopes approved as least-privilege for the pilot?",
        "Who can revoke access if the connector behaves unexpectedly?",
    ),
)


_CATEGORY_SECTIONS: dict[CapabilityCategory, OnboardingQuestionSection] = {
    "communication": OnboardingQuestionSection(
        id="communication-rollout",
        title="Communication rollout boundaries",
        questions=(
            "Which channels, threads, or rooms may agents read during the pilot?",
            "Where may agents post updates, and which posts require human approval?",
            "What escalation phrase or reaction should hand the conversation to a human?",
        ),
    ),
    "project_management": OnboardingQuestionSection(
        id="work-item-rollout",
        title="Work item lifecycle rollout",
        questions=(
            "Which boards, projects, or queues should seed Omnigent Tasks?",
            "Which external status transitions may Omnigent write back?",
            "How should conflicts be handled when humans change the source of truth?",
        ),
    ),
    "knowledge": OnboardingQuestionSection(
        id="knowledge-scope",
        title="Knowledge source and provenance scope",
        questions=(
            "Which pages, folders, or databases are in scope for agent reads?",
            "Where may agents append execution notes or generated documentation?",
            "What provenance marker should every agent-authored update include?",
        ),
    ),
    "developer": OnboardingQuestionSection(
        id="developer-safety",
        title="Developer workflow safety",
        questions=(
            "Which repositories, branches, or checks are in scope for the pilot?",
            "Which code changes must always route through pull request review?",
            "Who owns failed-check triage when an autonomous repair loop stalls?",
        ),
    ),
    "crm_support": OnboardingQuestionSection(
        id="customer-record-rollout",
        title="Customer record and support rollout",
        questions=(
            "Which customer segments or ticket queues are safe for the pilot?",
            "Which public replies or record updates require approval before publishing?",
            "How should support-to-sales handoffs preserve customer consent?",
        ),
    ),
    "commerce_billing": OnboardingQuestionSection(
        id="revenue-ops-rollout",
        title="Revenue operations rollout",
        questions=(
            "Which subscriptions, orders, or invoices are in scope for read-only context?",
            "Which revenue-affecting actions are disallowed during the pilot?",
            "Who approves refunds, cancellations, or billing mutations if enabled later?",
        ),
    ),
    "workflow_harness": OnboardingQuestionSection(
        id="workflow-harness",
        title="Deterministic workflow harness rollout",
        questions=(
            "Which repeatable workflow should become the first harness blueprint?",
            "What typed inputs, outputs, and evidence must every phase produce?",
            "Which verifier decides that a terminal phase is complete?",
        ),
    ),
}


def compile_integration_onboarding_questionnaire(slug: str) -> dict | None:
    """Return a JSON-ready pre-activation questionnaire for a catalog capability."""

    capability = get_integration_capability(slug)
    if capability is None:
        return None

    requires_external_auth = bool(capability.required_scopes)
    sections = [_BASE_SECTION]
    if requires_external_auth:
        sections.append(_AUTH_SECTION)
    sections.append(
        _activation_section(_risk_tier(capability.category, capability.required_scopes))
    )
    sections.append(_CATEGORY_SECTIONS[capability.category])
    section_dicts = [section.to_dict() for section in sections]

    return {
        "object": "integration_onboarding_questionnaire",
        "capability_slug": capability.slug,
        "capability_name": capability.name,
        "category": capability.category,
        "auth_model": capability.auth_model,
        "required_scopes": list(capability.required_scopes),
        "requires_external_auth": requires_external_auth,
        "risk_tier": _risk_tier(capability.category, capability.required_scopes),
        "sections": section_dicts,
        "minimum_answer_count": sum(
            len(section["questions"]) for section in section_dicts
        ),
    }


def _activation_section(risk_tier: IntegrationRiskTier) -> OnboardingQuestionSection:
    return OnboardingQuestionSection(
        id="activation-policy",
        title="Activation policy and rollback owner",
        questions=(
            f"Which activation policy should govern this {risk_tier} rollout?",
            "Which actions require approval before Omnigent executes them?",
            "Who owns rollback if the pilot creates noisy or unsafe outcomes?",
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
