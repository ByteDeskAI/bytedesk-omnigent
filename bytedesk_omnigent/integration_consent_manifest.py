"""Deterministic consent manifests for integration capability activation.

The catalog describes which integrations Omnigent should support. This module
turns a catalog entry into user-facing consent copy, scope rationales, and
operator risk prompts without reading credentials or contacting providers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from bytedesk_omnigent.integration_capabilities import (
    CapabilityCategory,
    IntegrationCapability,
    get_integration_capability,
)

ConsentRiskLevel = Literal["low", "moderate", "high"]


@dataclass(frozen=True)
class ScopeRationale:
    """Human-readable reason for one requested provider scope."""

    scope: str
    rationale: str
    risk_level: ConsentRiskLevel

    def to_dict(self) -> dict:
        return asdict(self)


_RISK_PROMPTS: dict[CapabilityCategory, tuple[str, ...]] = {
    "communication": (
        "Confirm which channels, teams, or threads agents may read before enabling ingestion.",
        "Require approval before agents post messages visible to humans or customers.",
    ),
    "project_management": (
        "Confirm which boards, projects, or issue queues agents may synchronize.",
        "Require approval before agents change external work item status or ownership.",
    ),
    "knowledge": (
        "Confirm selected files, pages, databases, or mailboxes before granting "
        "broad read access.",
        "Require explicit approval before an agent sends email or shares generated "
        "documents externally.",
    ),
    "developer": (
        "Confirm repository allow-lists and installation permissions before activation.",
        "Require pull-request review before agent-authored code changes reach protected branches.",
    ),
    "crm_support": (
        "Confirm customer record fields agents may read and summarize.",
        "Require approval before public replies, customer record mutation, or deal-stage changes.",
    ),
    "commerce_billing": (
        "Confirm revenue objects agents may inspect before activation.",
        "Require explicit approval for refunds, cancellations, billing changes, or "
        "order mutations.",
    ),
    "workflow_harness": (
        "Review workflow phase inputs, outputs, and completion evidence before activation.",
    ),
}


def compile_integration_consent_manifest(slug: str) -> dict | None:
    """Return a JSON-ready activation consent manifest for a catalog capability."""

    capability = get_integration_capability(slug)
    if capability is None:
        return None

    return {
        "object": "integration_consent_manifest",
        "capability_slug": capability.slug,
        "capability_name": capability.name,
        "provider_category": capability.category,
        "auth_model": capability.auth_model,
        "consent_summary": _consent_summary(capability),
        "operator_disclosure": _operator_disclosure(capability),
        "scope_rationales": [
            rationale.to_dict() for rationale in _scope_rationales(capability)
        ],
        "risk_prompts": list(_RISK_PROMPTS[capability.category]),
    }


def _consent_summary(capability: IntegrationCapability) -> str:
    if capability.category == "workflow_harness":
        return f"Enable {capability.name} without external OAuth credentials."
    return (
        f"Connect {capability.name} so Omnigent agents can execute the cataloged "
        "integration workflow."
    )


def _operator_disclosure(capability: IntegrationCapability) -> str:
    if capability.category == "workflow_harness":
        return (
            "This capability is internal to Omnigent and does not request "
            "third-party account access."
        )
    return (
        f"Omnigent will request {capability.auth_model} access only for the listed scopes. "
        "Operators should disclose the agent actions enabled by each scope before activation."
    )


def _scope_rationales(capability: IntegrationCapability) -> tuple[ScopeRationale, ...]:
    return tuple(
        ScopeRationale(
            scope=scope,
            rationale=(
                f"Allows Omnigent agents to perform the cataloged {capability.name} "
                "workflow with least-privilege access."
            ),
            risk_level=_scope_risk(scope),
        )
        for scope in capability.required_scopes
    )


def _scope_risk(scope: str) -> ConsentRiskLevel:
    normalized = scope.lower()
    if any(marker in normalized for marker in ("write", "send", "refund", "cancel")):
        return "high"
    moderate_markers = (
        "read",
        "history",
        "file",
        "documents",
        "spreadsheets",
        "calendar",
        "offline_access",
        "refresh_token",
    )
    if any(marker in normalized for marker in moderate_markers):
        return "moderate"
    return "low"
