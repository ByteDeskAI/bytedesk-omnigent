"""Deterministic remediation playbooks for failed integration rollout gates.

The verification matrix says what evidence is required before an integration is
production-ready. This module turns missing/failed gate ids into a compact,
operator- and agent-readable playbook so autonomous implementation loops can
repair rollout gaps without guessing the next action.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from bytedesk_omnigent.integration_verification_matrix import (
    compile_integration_verification_matrix,
)


@dataclass(frozen=True)
class RemediationStep:
    """One deterministic repair step for a failed verification gate."""

    gate_id: str
    gate_title: str
    owner: str
    evidence_to_collect: tuple[str, ...]
    recommended_actions: tuple[str, ...]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["evidence_to_collect"] = list(self.evidence_to_collect)
        data["recommended_actions"] = list(self.recommended_actions)
        return data


_GATE_OWNERS: dict[str, str] = {
    "catalog-contract": "integration-product-owner",
    "auth-boundary": "integration-security-owner",
    "ingress-normalization": "integration-platform-owner",
    "idempotency-replay": "integration-reliability-owner",
    "policy-approval": "integration-governance-owner",
    "observability-evidence": "integration-observability-owner",
    "rollback-readiness": "integration-operations-owner",
    "communication-loop": "workspace-operations-owner",
    "work-item-lifecycle": "workflow-operations-owner",
    "knowledge-scope-control": "knowledge-governance-owner",
    "developer-change-safety": "engineering-systems-owner",
    "customer-record-safety": "customer-operations-owner",
    "revenue-mutation-safety": "finance-operations-owner",
    "workflow-determinism": "workflow-harness-owner",
}

_ACTION_PREFIXES: dict[str, tuple[str, ...]] = {
    "catalog-contract": (
        "refresh the catalog entry and confirm product metadata is complete",
        "pin the capability slug in implementation notes and test fixtures",
    ),
    "auth-boundary": (
        "compare requested OAuth/API scopes against the catalog scope contract",
        "document credential storage, refresh, and re-authorization behavior",
    ),
    "ingress-normalization": (
        "capture provider event samples and normalize them into Omnigent signal fields",
        "add fail-closed handling for unsupported external event types",
    ),
    "idempotency-replay": (
        "derive idempotency keys from stable provider identifiers",
        "replay duplicate deliveries and record deterministic outcomes",
    ),
    "policy-approval": (
        "separate read-only tools from mutating provider actions",
        "bind high-risk writes to the required approval strategy",
    ),
    "observability-evidence": (
        "attach task, provider object, and agent identifiers to success/failure records",
        "verify operator-facing status excludes secrets and raw credentials",
    ),
    "rollback-readiness": (
        "document disablement and webhook teardown steps",
        "name a manual recovery owner and escalation path",
    ),
    "communication-loop": (
        "correlate agent replies to source threads or channels",
        "bound outbound messages with workspace/channel rate limits",
    ),
    "work-item-lifecycle": (
        "map external status transitions to Omnigent Task states",
        "verify write-back cannot override human source-of-truth updates",
    ),
    "knowledge-scope-control": (
        "limit reads to selected files, pages, databases, or mailboxes",
        "stamp writes with source task and agent provenance",
    ),
    "developer-change-safety": (
        "verify repository permissions are installation-scoped and least-privilege",
        "route code or CI mutations through reviewable pull requests",
    ),
    "customer-record-safety": (
        "require approval for public customer replies until quality gates pass",
        "capture before and after summaries for customer record updates",
    ),
    "revenue-mutation-safety": (
        "classify refunds, cancellations, and billing changes as approval-required",
        "separate read-only revenue context from payment-side effects",
    ),
    "workflow-determinism": (
        "stabilize phase ids and declare typed inputs and outputs per phase",
        "capture terminal-phase completion evidence for each deterministic run",
    ),
}


def compile_integration_remediation_playbook(
    slug: str,
    *,
    failed_gate_ids: tuple[str, ...] = (),
) -> dict | None:
    """Return a JSON-ready remediation playbook for failed rollout gates.

    Unknown capabilities return ``None``. Unknown gate ids are preserved in the
    response so API callers can surface operator input mistakes without losing
    valid remediation steps for known failed gates.
    """

    matrix = compile_integration_verification_matrix(slug)
    if matrix is None:
        return None

    gate_by_id = {gate["id"]: gate for gate in matrix["gates"]}
    requested_gate_ids = tuple(failed_gate_ids) or tuple(gate_by_id)
    steps: list[RemediationStep] = []
    unknown_gate_ids: list[str] = []

    for gate_id in requested_gate_ids:
        gate = gate_by_id.get(gate_id)
        if gate is None:
            unknown_gate_ids.append(gate_id)
            continue
        steps.append(
            RemediationStep(
                gate_id=gate_id,
                gate_title=gate["title"],
                owner=_GATE_OWNERS[gate_id],
                evidence_to_collect=tuple(gate["required_evidence"]),
                recommended_actions=(
                    *_ACTION_PREFIXES[gate_id],
                    f"rerun verification matrix gate {gate_id} and attach evidence "
                    "before promotion",
                ),
            )
        )

    requires_human_approval = matrix["risk_tier"] == "external_write" or any(
        step.gate_id in {"auth-boundary", "policy-approval", "rollback-readiness"}
        for step in steps
    )

    return {
        "object": "integration_remediation_playbook",
        "capability_slug": matrix["capability_slug"],
        "capability_name": matrix["capability_name"],
        "category": matrix["category"],
        "risk_tier": matrix["risk_tier"],
        "failed_gate_ids": [step.gate_id for step in steps],
        "unknown_failed_gate_ids": unknown_gate_ids,
        "steps": [step.to_dict() for step in steps],
        "summary": {
            "total_failed_gates": len(steps),
            "total_unknown_gate_ids": len(unknown_gate_ids),
            "requires_human_approval": requires_human_approval,
        },
    }
