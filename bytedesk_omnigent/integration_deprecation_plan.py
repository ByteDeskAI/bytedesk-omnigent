"""Deterministic deprecation plans for integration capability lifecycle management.

The capability catalog and verification matrix help Omnigent decide what to build
and how to certify it. This compiler covers the other end of the lifecycle: how
operators safely freeze, drain, disable, and retire an integration without losing
audit evidence or surprising tenants.
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
class DeprecationPhase:
    """One ordered retirement phase for an integration capability."""

    id: str
    title: str
    owner: str
    exit_criteria: tuple[str, ...]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["exit_criteria"] = list(self.exit_criteria)
        return data


_BASE_PHASES: tuple[DeprecationPhase, ...] = (
    DeprecationPhase(
        id="announce-freeze",
        title="Announce retirement window and freeze new activations",
        owner="platform-operator",
        exit_criteria=(
            "affected tenants, operators, and successor owners are notified",
            "new connector activations are blocked or explicitly allowlisted",
            "tenant-facing status explains the retirement reason without secrets",
        ),
    ),
    DeprecationPhase(
        id="drain-ingress",
        title="Drain inbound events and reconcile pending work",
        owner="integration-owner",
        exit_criteria=(
            "webhook queues and dead-letter entries are below the retirement threshold",
            "open Omnigent Tasks have migrate, complete, or cancel decisions",
            "provider subscriptions are marked read-only before teardown",
        ),
    ),
    DeprecationPhase(
        id="disable-mutations",
        title="Disable provider-side mutations before credential revocation",
        owner="policy-owner",
        exit_criteria=(
            "write tools are disabled or approval-denied by policy",
            "read-only evidence collection remains available during the drain window",
            "operators can confirm no new provider mutations are emitted",
        ),
    ),
    DeprecationPhase(
        id="archive-evidence",
        title="Archive audit evidence and tenant-visible history",
        owner="compliance-owner",
        exit_criteria=(
            "task ids, provider object ids, and agent ids remain correlated",
            "success, failure, and cancellation outcomes are exportable",
            "retention notes are attached to the connector retirement record",
        ),
    ),
    DeprecationPhase(
        id="revoke-credentials",
        title="Revoke OAuth grants, tokens, webhooks, and provider subscriptions",
        owner="security-owner",
        exit_criteria=(
            "all stored credentials are deleted or marked inert",
            "provider webhook or subscription teardown has verified success",
            "post-revocation health checks cannot perform provider reads or writes",
        ),
    ),
    DeprecationPhase(
        id="finalize-successor",
        title="Finalize successor path and close retirement record",
        owner="product-owner",
        exit_criteria=(
            "successor owner or replacement workflow is named",
            "remaining tasks have an explicit migration or cancellation outcome",
            "operators can still read historical execution evidence",
        ),
    ),
)

_CATEGORY_RETENTION_NOTES: dict[CapabilityCategory, str] = {
    "communication": "channel export or thread permalink inventory",
    "project_management": "work item id, status, comment, and checklist snapshots",
    "knowledge": "document/page/database provenance and last-write summaries",
    "developer": "repository, issue, pull request, review, and CI evidence links",
    "crm_support": "customer record, ticket, conversation, and consent summaries",
    "commerce_billing": "order, invoice, subscription, and refund audit references",
    "workflow_harness": "workflow run graph snapshots",
}

_NOTICE_DAYS_BY_RISK_TIER = {
    "internal_harness": 0,
    "external_read": 7,
    "external_write": 14,
}

_SUCCESSOR_REQUIREMENTS = (
    "successor owner or replacement workflow is named",
    "remaining tasks have an explicit migration or cancellation outcome",
    "operators can still read historical execution evidence",
)


def compile_integration_deprecation_plan(slug: str) -> dict | None:
    """Return a JSON-ready retirement plan for one catalog capability."""

    capability = get_integration_capability(slug)
    verification_matrix = compile_integration_verification_matrix(slug)
    if capability is None or verification_matrix is None:
        return None

    risk_tier = verification_matrix["risk_tier"]
    minimum_notice_days = _NOTICE_DAYS_BY_RISK_TIER[risk_tier]

    return {
        "object": "integration_deprecation_plan",
        "capability_slug": capability.slug,
        "capability_name": capability.name,
        "category": capability.category,
        "risk_tier": risk_tier,
        "requires_customer_notice": minimum_notice_days > 0,
        "minimum_notice_days": minimum_notice_days,
        "reversible_until_phase": "revoke-credentials",
        "category_retention_notes": _CATEGORY_RETENTION_NOTES[capability.category],
        "successor_requirements": list(_SUCCESSOR_REQUIREMENTS),
        "phases": [phase.to_dict() for phase in _BASE_PHASES],
    }
