"""Deterministic readiness assessments for integration verification evidence.

The verification matrix declares the evidence an integration must provide before
rollout. This module scores caller-submitted evidence against that matrix without
reading tenant data, credentials, GitHub, or live provider APIs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Literal

from bytedesk_omnigent.integration_verification_matrix import (
    compile_integration_verification_matrix,
)

GateAssessmentStatus = Literal["missing", "partial", "satisfied"]
ActivationState = Literal["ready", "blocked_by_policy_evidence", "in_progress"]


@dataclass(frozen=True)
class GateReadinessAssessment:
    """Readiness status for one verification gate."""

    id: str
    title: str
    status: GateAssessmentStatus
    satisfied_evidence: tuple[str, ...]
    missing_evidence: tuple[str, ...]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["satisfied_evidence"] = list(self.satisfied_evidence)
        data["missing_evidence"] = list(self.missing_evidence)
        return data


def compile_integration_readiness_assessment(
    slug: str,
    *,
    evidence: Mapping[str, Sequence[str]] | None = None,
) -> dict | None:
    """Score submitted evidence against a capability verification matrix.

    ``evidence`` is keyed by verification gate id. Values are evidence labels or
    descriptions that should match the matrix's ``required_evidence`` entries.
    Unknown gate ids and unknown evidence labels are ignored so callers can pass
    richer operator evidence without hiding true missing requirements.
    """

    matrix = compile_integration_verification_matrix(slug)
    if matrix is None:
        return None

    submitted = _normalize_evidence(evidence or {})
    gate_assessments: list[GateReadinessAssessment] = []
    satisfied_evidence_count = 0
    submitted_evidence_count = 0

    for gate in matrix["gates"]:
        gate_id = gate["id"]
        required = tuple(gate["required_evidence"])
        submitted_for_gate = submitted.get(gate_id, frozenset())
        satisfied = tuple(item for item in required if item in submitted_for_gate)
        missing = tuple(item for item in required if item not in submitted_for_gate)
        submitted_evidence_count += len(satisfied)
        satisfied_evidence_count += len(satisfied)
        gate_assessments.append(
            GateReadinessAssessment(
                id=gate_id,
                title=gate["title"],
                status=_gate_status(satisfied=satisfied, missing=missing),
                satisfied_evidence=satisfied,
                missing_evidence=missing,
            )
        )

    total_evidence_count = int(matrix["minimum_required_evidence_count"])
    missing_evidence_count = total_evidence_count - satisfied_evidence_count
    satisfied_gate_count = sum(
        1 for gate in gate_assessments if gate.status == "satisfied"
    )
    next_missing_gate_id = next(
        (gate.id for gate in gate_assessments if gate.status != "satisfied"), None
    )

    return {
        "object": "integration_readiness_assessment",
        "capability_slug": matrix["capability_slug"],
        "capability_name": matrix["capability_name"],
        "risk_tier": matrix["risk_tier"],
        "activation_state": _activation_state(
            risk_tier=matrix["risk_tier"],
            gate_assessments=tuple(gate_assessments),
        ),
        "readiness_percent": round(
            (satisfied_evidence_count / total_evidence_count) * 100
        )
        if total_evidence_count
        else 100,
        "satisfied_gate_count": satisfied_gate_count,
        "total_gate_count": len(gate_assessments),
        "submitted_evidence_count": submitted_evidence_count,
        "satisfied_evidence_count": satisfied_evidence_count,
        "missing_evidence_count": missing_evidence_count,
        "next_missing_gate_id": next_missing_gate_id,
        "gates": [gate.to_dict() for gate in gate_assessments],
    }


def _normalize_evidence(
    evidence: Mapping[str, Sequence[str]],
) -> dict[str, frozenset[str]]:
    normalized: dict[str, frozenset[str]] = {}
    for gate_id, values in evidence.items():
        if not isinstance(gate_id, str) or isinstance(values, (str, bytes)):
            continue
        normalized[gate_id] = frozenset(
            value.strip() for value in values if isinstance(value, str) and value.strip()
        )
    return normalized


def _gate_status(
    *, satisfied: tuple[str, ...], missing: tuple[str, ...]
) -> GateAssessmentStatus:
    if not satisfied:
        return "missing"
    if missing:
        return "partial"
    return "satisfied"


def _activation_state(
    *,
    risk_tier: str,
    gate_assessments: tuple[GateReadinessAssessment, ...],
) -> ActivationState:
    if all(gate.status == "satisfied" for gate in gate_assessments):
        return "ready"
    policy_gate = next(
        (gate for gate in gate_assessments if gate.id == "policy-approval"), None
    )
    if risk_tier == "external_write" and (
        policy_gate is None or policy_gate.status != "satisfied"
    ):
        return "blocked_by_policy_evidence"
    return "in_progress"
