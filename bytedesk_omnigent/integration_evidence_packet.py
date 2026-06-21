"""Evidence packet compiler for integration capability rollout.

Verification matrices define the gates. This module flattens those gates into an
operator-ready evidence packet that a planning agent, ByteDesk Platform UI, or
review workflow can hand to humans before enabling an integration for tenants.
The compiler is deterministic and secret-free: callers attach redacted evidence
outside this pure data structure.
"""

from __future__ import annotations

from bytedesk_omnigent.integration_capabilities import CapabilityCategory
from bytedesk_omnigent.integration_verification_matrix import (
    IntegrationRiskTier,
    compile_integration_verification_matrix,
)

_REVIEW_LANES: dict[CapabilityCategory, str] = {
    "communication": "security_and_operations",
    "project_management": "operations",
    "knowledge": "data_governance",
    "developer": "engineering_security",
    "crm_support": "customer_operations",
    "commerce_billing": "finance_and_security",
    "workflow_harness": "platform_architecture",
}

_INTERNAL_COLLECTION_NOTES = [
    "Attach deterministic fixture inputs, expected phase outputs, and replay logs "
    "for each workflow gate.",
    "Include schema version and migration notes when workflow blueprint contracts change.",
]

_EXTERNAL_BASE_NOTES = [
    "Treat provider tokens, signing secrets, and customer payload samples as "
    "redacted evidence only.",
    "Capture tenant/workspace identifiers as opaque ids; do not include raw "
    "customer content in the packet.",
]

_EXTERNAL_WRITE_NOTE = (
    "External write integrations require approval evidence before any provider-side "
    "mutation is enabled."
)


def compile_integration_evidence_packet(slug: str) -> dict | None:
    """Return a JSON-ready operator evidence packet for one capability."""

    matrix = compile_integration_verification_matrix(slug)
    if matrix is None:
        return None

    gates = matrix["gates"]
    evidence_items = [
        {
            "id": f"{gate['id']}:{index}",
            "gate_id": gate["id"],
            "gate_title": gate["title"],
            "required_evidence": required_evidence,
            "status": "required",
        }
        for gate in gates
        for index, required_evidence in enumerate(gate["required_evidence"], start=1)
    ]
    evidence_count = len(evidence_items)
    gate_count = len(gates)

    return {
        "object": "integration_evidence_packet",
        "capability_slug": matrix["capability_slug"],
        "capability_name": matrix["capability_name"],
        "category": matrix["category"],
        "risk_tier": matrix["risk_tier"],
        "review_lane": _REVIEW_LANES[matrix["category"]],
        "operator_summary": (
            f"Collect {evidence_count} required evidence item(s) across {gate_count} gate(s) "
            f"before enabling {matrix['capability_name']} for production tenants."
        ),
        "evidence_items": evidence_items,
        "collection_notes": _collection_notes(matrix["risk_tier"]),
        "handoff_prompt": (
            f"Verify {matrix['capability_name']} readiness by attaching evidence for "
            f"{evidence_count} required item(s); keep secrets redacted and link each item "
            "to a task, test run, runbook, or approval record."
        ),
    }


def _collection_notes(risk_tier: IntegrationRiskTier) -> list[str]:
    if risk_tier == "internal_harness":
        return list(_INTERNAL_COLLECTION_NOTES)

    notes = list(_EXTERNAL_BASE_NOTES)
    if risk_tier == "external_write":
        notes.append(_EXTERNAL_WRITE_NOTE)
    return notes
