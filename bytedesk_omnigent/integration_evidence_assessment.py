"""Deterministic evidence assessments for integration activation readiness.

The verification matrix describes the evidence required before a catalog
integration should be activated. This module lets platform surfaces and
autonomous workflow harnesses preview whether provided evidence satisfies those
requirements without persisting tenant data, reading secrets, or calling an
external provider.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from bytedesk_omnigent.integration_verification_matrix import (
    compile_integration_verification_matrix,
)


@dataclass(frozen=True)
class IntegrationEvidenceItem:
    """Caller-provided evidence for one verification gate."""

    gate_id: str
    evidence: tuple[str, ...]
    source: str

    @classmethod
    def from_payload(cls, payload: dict) -> IntegrationEvidenceItem:
        evidence = payload.get("evidence", ())
        if isinstance(evidence, str):
            evidence = (evidence,)
        return cls(
            gate_id=str(payload.get("gate_id", "")),
            evidence=tuple(str(item) for item in evidence),
            source=str(payload.get("source", "unspecified")),
        )


def assess_integration_evidence(
    slug: str,
    *,
    evidence_items: Iterable[IntegrationEvidenceItem],
) -> dict | None:
    """Return a JSON-ready readiness assessment for a catalog capability.

    Evidence is matched exactly against the required evidence strings emitted by
    ``compile_integration_verification_matrix``. Unknown gate ids and extra
    evidence are ignored so callers can safely pass a larger certification run
    result while the assessment remains tied to the current deterministic matrix.
    """

    matrix = compile_integration_verification_matrix(slug)
    if matrix is None:
        return None

    evidence_by_gate: dict[str, list[IntegrationEvidenceItem]] = {}
    for item in evidence_items:
        evidence_by_gate.setdefault(item.gate_id, []).append(item)

    gate_results = []
    missing_evidence_count = 0
    satisfied_gate_count = 0
    for gate in matrix["gates"]:
        required_evidence = tuple(gate["required_evidence"])
        gate_items = evidence_by_gate.get(gate["id"], [])
        provided_evidence = _ordered_intersection(
            required_evidence,
            tuple(evidence for item in gate_items for evidence in item.evidence),
        )
        missing_evidence = [
            evidence for evidence in required_evidence if evidence not in provided_evidence
        ]
        sources = sorted({item.source for item in gate_items if item.source})
        satisfied = not missing_evidence
        if satisfied:
            satisfied_gate_count += 1
        missing_evidence_count += len(missing_evidence)
        gate_results.append(
            {
                "gate_id": gate["id"],
                "title": gate["title"],
                "satisfied": satisfied,
                "provided_evidence": provided_evidence,
                "missing_evidence": missing_evidence,
                "sources": sources,
            }
        )

    total_gate_count = len(gate_results)
    return {
        "object": "integration_evidence_assessment",
        "capability_slug": matrix["capability_slug"],
        "capability_name": matrix["capability_name"],
        "category": matrix["category"],
        "risk_tier": matrix["risk_tier"],
        "ready_for_activation": satisfied_gate_count == total_gate_count,
        "satisfied_gate_count": satisfied_gate_count,
        "total_gate_count": total_gate_count,
        "minimum_required_evidence_count": matrix["minimum_required_evidence_count"],
        "missing_evidence_count": missing_evidence_count,
        "gate_results": gate_results,
    }


def _ordered_intersection(required: tuple[str, ...], provided: tuple[str, ...]) -> list[str]:
    provided_set = set(provided)
    return [item for item in required if item in provided_set]
