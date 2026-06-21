"""Deterministic assessment of integration rollout evidence.

The verification matrix defines which evidence is required before an integration
capability can be called production-ready. This module evaluates a caller's
provided, secret-free evidence against that matrix so platform UIs and loop
operators can show exactly which rollout gates are complete or still blocked.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from bytedesk_omnigent.integration_verification_matrix import (
    compile_integration_verification_matrix,
)

EvidenceInput = Mapping[str, Sequence[str]]


def _normalize_evidence(items: Sequence[str] | None) -> list[str]:
    if not items:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return normalized


def assess_integration_verification_evidence(
    slug: str, *, provided_evidence: EvidenceInput
) -> dict | None:
    """Assess provided rollout evidence for one integration capability.

    Returns ``None`` when the capability slug is unknown. Otherwise the returned
    dict is JSON-ready and intentionally echoes only the evidence strings needed
    for gate completion; callers should pass redacted, operator-safe evidence
    labels rather than secrets or raw provider payloads.
    """

    matrix = compile_integration_verification_matrix(slug)
    if matrix is None:
        return None

    gate_assessments: list[dict] = []
    provided_count = 0
    missing_count = 0

    for gate in matrix["gates"]:
        gate_id = gate["id"]
        required = list(gate["required_evidence"])
        provided = _normalize_evidence(provided_evidence.get(gate_id))
        provided_required = [item for item in required if item in set(provided)]
        missing = [item for item in required if item not in set(provided_required)]
        provided_count += len(provided_required)
        missing_count += len(missing)
        gate_assessments.append(
            {
                "gate_id": gate_id,
                "title": gate["title"],
                "status": "complete" if not missing else "incomplete",
                "provided_evidence": provided_required,
                "missing_evidence": missing,
            }
        )

    return {
        "capability_slug": matrix["capability_slug"],
        "capability_name": matrix["capability_name"],
        "category": matrix["category"],
        "risk_tier": matrix["risk_tier"],
        "status": "complete" if missing_count == 0 else "incomplete",
        "provided_evidence_count": provided_count,
        "missing_evidence_count": missing_count,
        "minimum_required_evidence_count": matrix["minimum_required_evidence_count"],
        "gate_assessments": gate_assessments,
    }
