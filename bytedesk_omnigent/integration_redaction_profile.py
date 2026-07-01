"""Secret-safe redaction profiles for integration capability evidence.

The integration catalog describes what a connector can do. This module derives a
stable, credentialless redaction profile so autonomous agents, ByteDesk Platform,
and future harness runners know which request, event, and evidence fields may be
logged, summarized, hashed, or fully redacted before tenant activation.
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

RedactionAction = Literal["allow", "summarize", "hash", "redact"]
DefaultLogLevel = Literal["structured_evidence", "metadata_only"]


@dataclass(frozen=True)
class RedactionFieldRule:
    """One deterministic redaction instruction for integration evidence."""

    field: str
    action: RedactionAction
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


_BASE_FIELD_RULES: tuple[RedactionFieldRule, ...] = (
    RedactionFieldRule(
        field="headers.authorization",
        action="redact",
        reason="authorization headers can contain bearer, bot, app, or webhook credentials",
    ),
    RedactionFieldRule(
        field="headers.cookie",
        action="redact",
        reason="cookies can contain tenant or user session credentials",
    ),
    RedactionFieldRule(
        field="webhook.signature",
        action="hash",
        reason="signature hashes preserve replay evidence without exposing shared secrets",
    ),
    RedactionFieldRule(
        field="provider.object_id",
        action="allow",
        reason="stable provider ids are required for traceability and idempotency evidence",
    ),
)

_EXTERNAL_WRITE_RULES: tuple[RedactionFieldRule, ...] = (
    RedactionFieldRule(
        field="outbound_request.body",
        action="redact",
        reason="external write requests may contain customer data or mutation payloads",
    ),
)

_CATEGORY_RULES: dict[CapabilityCategory, RedactionFieldRule] = {
    "communication": RedactionFieldRule(
        field="message.text",
        action="summarize",
        reason="communication payloads can contain customer, employee, or approval context",
    ),
    "project_management": RedactionFieldRule(
        field="work_item.description",
        action="summarize",
        reason=(
            "work item bodies can include customer commitments, private plans, "
            "or credentials pasted by users"
        ),
    ),
    "knowledge": RedactionFieldRule(
        field="document.content",
        action="summarize",
        reason="knowledge connectors can access proprietary documents and operational runbooks",
    ),
    "developer": RedactionFieldRule(
        field="repository.diff",
        action="summarize",
        reason="repository diffs can contain proprietary code and accidental secret material",
    ),
    "crm_support": RedactionFieldRule(
        field="customer_record.timeline",
        action="summarize",
        reason=(
            "CRM and support timelines can contain personal data and sensitive "
            "customer history"
        ),
    ),
    "commerce_billing": RedactionFieldRule(
        field="payment_method.details",
        action="redact",
        reason="billing evidence must never expose payment instrument details",
    ),
    "workflow_harness": RedactionFieldRule(
        field="workflow.phase.output",
        action="hash",
        reason=(
            "phase outputs may include generated customer artifacts; hashes "
            "preserve deterministic replay evidence"
        ),
    ),
}

_ALWAYS_REDACT_HEADERS = (
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-slack-signature",
    "stripe-signature",
    "x-hub-signature",
    "x-hubspot-signature",
)


def compile_integration_redaction_profile(slug: str) -> dict | None:
    """Return a JSON-ready secret redaction profile for a catalog capability."""

    capability = get_integration_capability(slug)
    if capability is None:
        return None

    matrix = compile_integration_verification_matrix(slug)
    if matrix is None:
        return None

    risk_tier: IntegrationRiskTier = matrix["risk_tier"]
    rules = list(_BASE_FIELD_RULES)
    if risk_tier == "external_write":
        rules.extend(_EXTERNAL_WRITE_RULES)
    rules.append(_CATEGORY_RULES[capability.category])

    return {
        "object": "integration_redaction_profile",
        "capability_slug": capability.slug,
        "capability_name": capability.name,
        "category": capability.category,
        "risk_tier": risk_tier,
        "default_log_level": _default_log_level(risk_tier),
        "always_redact_headers": list(_ALWAYS_REDACT_HEADERS),
        "sensitive_scopes": _sensitive_scopes(capability.required_scopes),
        "field_rules": [rule.to_dict() for rule in rules],
        "retention_policy": _retention_policy(capability.category, risk_tier),
    }


def _default_log_level(risk_tier: IntegrationRiskTier) -> DefaultLogLevel:
    if risk_tier == "internal_harness":
        return "structured_evidence"
    return "metadata_only"


def _sensitive_scopes(required_scopes: tuple[str, ...]) -> list[str]:
    return [
        scope
        for scope in required_scopes
        if any(
            token in scope.lower()
            for token in ("write", "chat:", "documents", "spreadsheets", "tickets")
        )
    ]


def _retention_policy(
    category: CapabilityCategory, risk_tier: IntegrationRiskTier
) -> dict[str, object]:
    if risk_tier == "internal_harness":
        return {
            "evidence_days": 30,
            "payload_days": 0,
            "rationale": (
                "internal workflow harnesses should keep structured evidence "
                "without retaining raw phase payloads"
            ),
        }
    if category in {"commerce_billing", "crm_support"}:
        return {
            "evidence_days": 30,
            "payload_days": 0,
            "rationale": (
                "customer and revenue integrations should retain traceability "
                "metadata but discard raw provider payloads"
            ),
        }
    return {
        "evidence_days": 30,
        "payload_days": 7,
        "rationale": (
            "short payload retention supports connector debugging while "
            "minimizing tenant data exposure"
        ),
    }
