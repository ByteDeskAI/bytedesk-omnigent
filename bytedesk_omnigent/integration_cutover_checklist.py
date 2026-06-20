"""Deterministic cutover checklists for integration capability activation.

The verification matrix defines readiness evidence. This module turns that
readiness contract into an ordered cutover runbook so ByteDesk operators and
autonomous loop workers can rehearse, activate, review, and safely roll back a
catalog integration without requiring provider credentials or tenant data.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from bytedesk_omnigent.integration_capabilities import get_integration_capability
from bytedesk_omnigent.integration_verification_matrix import (
    IntegrationRiskTier,
    compile_integration_verification_matrix,
)

CutoverOwner = Literal[
    "product operator",
    "security owner",
    "integration owner",
    "workflow architect",
]


@dataclass(frozen=True)
class CutoverPhase:
    """One deterministic phase in a provider or harness activation runbook."""

    id: str
    title: str
    owner: CutoverOwner
    entry_criteria: tuple[str, ...]
    exit_evidence: tuple[str, ...]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["entry_criteria"] = list(self.entry_criteria)
        data["exit_evidence"] = list(self.exit_evidence)
        return data


def compile_integration_cutover_checklist(slug: str) -> dict | None:
    """Return a JSON-ready activation cutover checklist for one capability."""

    capability = get_integration_capability(slug)
    matrix = compile_integration_verification_matrix(slug)
    if capability is None or matrix is None:
        return None

    risk_tier = matrix["risk_tier"]
    gate_map = {gate["id"]: gate for gate in matrix["gates"]}
    phases = _build_phases(risk_tier, gate_map)

    return {
        "object": "integration_cutover_checklist",
        "capability_slug": capability.slug,
        "capability_name": capability.name,
        "category": capability.category,
        "risk_tier": risk_tier,
        "auth_model": capability.auth_model,
        "required_scopes": list(capability.required_scopes),
        "required_approvals": _required_approvals(risk_tier),
        "verification_gate_ids": [gate["id"] for gate in matrix["gates"]],
        "minimum_required_evidence_count": matrix["minimum_required_evidence_count"],
        "phases": [phase.to_dict() for phase in phases],
    }


def _required_approvals(risk_tier: IntegrationRiskTier) -> list[str]:
    if risk_tier == "internal_harness":
        return ["integration_owner"]
    if risk_tier == "external_read":
        return ["tenant_admin", "integration_owner"]
    return ["tenant_admin", "security_owner", "integration_owner"]


def _build_phases(
    risk_tier: IntegrationRiskTier, gate_map: dict[str, dict]
) -> tuple[CutoverPhase, ...]:
    is_harness = risk_tier == "internal_harness"
    credential_owner: CutoverOwner = "workflow architect" if is_harness else "security owner"
    rehearsal_title = (
        "Run deterministic workflow rehearsal"
        if is_harness
        else "Run read-only provider dry run"
    )

    return (
        CutoverPhase(
            id="catalog-freeze",
            title="Freeze catalog contract and activation scope",
            owner="product operator",
            entry_criteria=tuple(gate_map["catalog-contract"]["required_evidence"]),
            exit_evidence=(
                "capability owner confirms the catalog contract is the cutover source of truth",
                "tenant-facing activation summary is generated without secrets",
            ),
        ),
        CutoverPhase(
            id="credential-boundary",
            title="Validate credential and authorization boundary",
            owner=credential_owner,
            entry_criteria=tuple(gate_map["auth-boundary"]["required_evidence"]),
            exit_evidence=(
                "approval record names the exact credential boundary used for cutover",
                "scope diff is empty or explicitly accepted by the required approvers",
            ),
        ),
        CutoverPhase(
            id="dry-run-rehearsal",
            title="Normalize ingress and replay behavior in dry run",
            owner="integration owner",
            entry_criteria=(
                *gate_map["ingress-normalization"]["required_evidence"],
                *gate_map["idempotency-replay"]["required_evidence"],
            ),
            exit_evidence=(
                "dry-run payloads produce deterministic Omnigent signals or phase outputs",
                "duplicate replay evidence matches the original normalized outcome",
            ),
        ),
        CutoverPhase(
            id="limited-production-window",
            title=rehearsal_title,
            owner="integration owner",
            entry_criteria=tuple(gate_map["policy-approval"]["required_evidence"]),
            exit_evidence=(
                "limited production window is time-boxed with named owner on call",
                "mutating actions are blocked or approval-gated according to risk tier",
            ),
        ),
        CutoverPhase(
            id="evidence-review",
            title="Review operator-visible evidence",
            owner="product operator",
            entry_criteria=tuple(gate_map["observability-evidence"]["required_evidence"]),
            exit_evidence=(
                "operator status view links task, agent, and provider or workflow evidence",
                "failure path is visible without exposing credentials or private payloads",
            ),
        ),
        CutoverPhase(
            id="rollback-or-scale",
            title="Decide rollback, hold, or scale-out",
            owner="integration owner",
            entry_criteria=tuple(gate_map["rollback-readiness"]["required_evidence"]),
            exit_evidence=(
                "disablement or teardown path is confirmed before broader rollout",
                "scale-out decision records satisfied gate ids and remaining follow-ups",
            ),
        ),
    )
