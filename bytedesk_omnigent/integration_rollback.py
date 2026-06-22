"""Deterministic rollback plans for third-party integration mutations.

Autonomous agents that mutate external SaaS systems need a compensation contract
before they act: what snapshot to capture, what automation to pause, how to
restore state, and which evidence proves rollback completed.  This module is a
pure compiler for that contract.  It performs no network calls and reads no
secrets, making it safe for platform previews, approvals, and dry-runs.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class IntegrationRollbackStep:
    """One deterministic compensation step for an external integration mutation."""

    name: str
    gate: str
    instruction: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class IntegrationRollbackPlan:
    """Rollback contract compiled before an agent mutates a third-party system."""

    plan_id: str
    provider: str
    operation: str
    agent_id: str
    external_ref: str
    mutation_summary: str
    risk_level: str
    requires_approval: bool
    idempotency_key: str
    required_snapshot_fields: tuple[str, ...]
    verification_evidence: tuple[str, ...]
    steps: tuple[IntegrationRollbackStep, ...]

    def to_dict(self) -> dict:
        """Return a JSON-serializable representation for FastAPI routes."""
        return asdict(self)


_PROVIDER_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "github": (
        ("external_ref", "before_state", "labels", "assignees", "milestone"),
        ("external_ref", "restored_state", "audit_comment_url"),
    ),
    "jira": (
        ("external_ref", "previous_status", "fields", "assignee", "labels"),
        ("external_ref", "restored_status", "history_item_id"),
    ),
    "linear": (
        ("external_ref", "previous_state", "team_id", "assignee_id", "labels"),
        ("external_ref", "restored_state", "audit_comment_id"),
    ),
    "slack": (
        ("external_ref", "channel_id", "message_ts", "before_text", "thread_ts"),
        ("external_ref", "restored_message_ts", "operator_receipt"),
    ),
    "notion": (
        ("external_ref", "before_properties", "parent_id", "last_edited_time"),
        ("external_ref", "restored_properties", "page_version_receipt"),
    ),
    "hubspot": (
        ("external_ref", "object_type", "before_properties", "association_ids"),
        ("external_ref", "restored_properties", "audit_event_id"),
    ),
    "salesforce": (
        ("external_ref", "sobject_type", "before_fields", "owner_id"),
        ("external_ref", "restored_fields", "audit_event_id"),
    ),
    "zendesk": (
        ("external_ref", "previous_status", "tags", "assignee_id", "custom_fields"),
        ("external_ref", "restored_status", "audit_comment_id"),
    ),
    "google-workspace": (
        ("external_ref", "resource_id", "before_metadata", "permissions"),
        ("external_ref", "restored_metadata", "permission_receipt"),
    ),
}

_GENERIC_SNAPSHOT_FIELDS = ("external_ref", "before_state", "changed_fields")
_GENERIC_VERIFICATION_EVIDENCE = (
    "external_ref",
    "post_rollback_state",
    "operator_receipt",
)


def _normalize(value: str) -> str:
    return "-".join(value.strip().lower().replace("_", "-").split())


def _digest(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def compile_integration_rollback_plan(
    *,
    provider: str,
    operation: str,
    agent_id: str,
    external_ref: str,
    mutation_summary: str = "",
    risk_level: str = "medium",
) -> IntegrationRollbackPlan:
    """Compile a deterministic rollback plan for an external integration write.

    The function is intentionally conservative: every plan requires approval
    unless the caller explicitly marks the mutation ``risk_level="low"``. Unknown
    providers receive generic snapshot and verification fields instead of
    invented service-specific semantics.
    """
    normalized_provider = _normalize(provider)
    normalized_operation = _normalize(operation)
    normalized_risk = _normalize(risk_level) or "medium"
    snapshot_fields, verification_evidence = _PROVIDER_FIELDS.get(
        normalized_provider,
        (_GENERIC_SNAPSHOT_FIELDS, _GENERIC_VERIFICATION_EVIDENCE),
    )
    plan_digest = _digest(
        normalized_provider,
        normalized_operation,
        agent_id.strip(),
        external_ref.strip(),
        mutation_summary.strip(),
        normalized_risk,
    )
    idempotency_key = f"integration-rollback:{normalized_provider}:{plan_digest}"
    steps = (
        IntegrationRollbackStep(
            name="capture_pre_mutation_snapshot",
            gate="snapshot_recorded",
            instruction=(
                "Persist the listed snapshot fields before executing the external "
                "mutation so compensation has a known-good target."
            ),
            evidence=snapshot_fields,
        ),
        IntegrationRollbackStep(
            name="freeze_followup_automation",
            gate="automation_quieted",
            instruction=(
                "Pause or mark downstream automations that could re-trigger from "
                "the compensation write; record what was paused."
            ),
            evidence=("paused_automation_ids", "freeze_receipt"),
        ),
        IntegrationRollbackStep(
            name="apply_compensation",
            gate="approval_granted",
            instruction=(
                "Use the snapshot and idempotency key to restore the external "
                "object or create an explicit compensating update."
            ),
            evidence=("idempotency_key", "compensation_receipt"),
        ),
        IntegrationRollbackStep(
            name="verify_external_state",
            gate="verification_passed",
            instruction=(
                "Read the external object after compensation and compare it to "
                "the expected rollback evidence fields."
            ),
            evidence=verification_evidence,
        ),
        IntegrationRollbackStep(
            name="publish_handoff_receipt",
            gate="handoff_published",
            instruction=(
                "Publish a human-readable receipt with the mutation, rollback "
                "decision, external reference, and verification evidence."
            ),
            evidence=("handoff_summary", "operator_receipt"),
        ),
    )
    return IntegrationRollbackPlan(
        plan_id=f"rollback:{normalized_provider}:{plan_digest}",
        provider=normalized_provider,
        operation=normalized_operation,
        agent_id=agent_id.strip(),
        external_ref=external_ref.strip(),
        mutation_summary=mutation_summary.strip(),
        risk_level=normalized_risk,
        requires_approval=normalized_risk != "low",
        idempotency_key=idempotency_key,
        required_snapshot_fields=snapshot_fields,
        verification_evidence=verification_evidence,
        steps=steps,
    )
