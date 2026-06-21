"""Deterministic incident drills for integration capability operations.

The catalog and verification matrix define what should be built and how it is
accepted. This module adds the next operational primitive: a secret-free incident
drill that tells autonomous operators how to pause, preserve evidence, recover,
and communicate when a connector or workflow harness misbehaves.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from bytedesk_omnigent.integration_capabilities import (
    CapabilityCategory,
    get_integration_capability,
)
from bytedesk_omnigent.integration_verification_matrix import (
    IntegrationRiskTier,
    compile_integration_verification_matrix,
)


@dataclass(frozen=True)
class IncidentTrigger:
    """Provider/category-specific trigger that starts an integration drill."""

    id: str
    title: str
    detection_signals: tuple[str, ...]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["detection_signals"] = list(self.detection_signals)
        return data


_TRIGGER_BY_CATEGORY: dict[CapabilityCategory, IncidentTrigger] = {
    "communication": IncidentTrigger(
        id="external-write-side-effect",
        title="Outbound collaboration action is duplicated, delayed, or misrouted",
        detection_signals=(
            "provider confirms a sent message that Omnigent did not mark successful",
            "same idempotency key appears on more than one provider write",
            "thread, channel, or workspace routing differs from the task evidence",
        ),
    ),
    "project_management": IncidentTrigger(
        id="work-item-sync-drift",
        title="External work item state diverges from Omnigent task state",
        detection_signals=(
            "provider status differs from the last acknowledged task transition",
            "comment or checklist write-back is missing expected author attribution",
            "webhook replay produces a different lifecycle transition",
        ),
    ),
    "knowledge": IncidentTrigger(
        id="external-read-staleness",
        title="Knowledge source is stale, over-broad, or missing provenance",
        detection_signals=(
            "indexed content hash no longer matches the provider object revision",
            "read set includes a file, page, mailbox, or database outside the approved scope",
            "agent output cites knowledge without source object provenance",
        ),
    ),
    "developer": IncidentTrigger(
        id="review-safety-regression",
        title="Engineering automation bypasses review or loses CI evidence",
        detection_signals=(
            "code mutation is not represented by a reviewable pull request",
            "failed check run or review comment is missing from task evidence",
            "repository installation permissions exceed the catalog boundary",
        ),
    ),
    "crm_support": IncidentTrigger(
        id="customer-record-integrity",
        title="Customer record mutation or response safety is at risk",
        detection_signals=(
            "public customer reply is queued without required approval evidence",
            "CRM/support record update lacks before/after summary evidence",
            "handoff loses customer consent or account context",
        ),
    ),
    "commerce_billing": IncidentTrigger(
        id="revenue-side-effect-risk",
        title="Revenue-affecting automation may create a financial side effect",
        detection_signals=(
            "refund, cancellation, or billing mutation is requested without explicit approval",
            "payment/order object link is missing from anomaly evidence",
            "provider financial state changes outside the expected idempotency key",
        ),
    ),
    "workflow_harness": IncidentTrigger(
        id="workflow-phase-stall",
        title="Workflow harness phase stalls or emits inconsistent evidence",
        detection_signals=(
            "phase exceeds its declared timeout or retry budget",
            "declared output artifact is missing, malformed, or attached to the wrong phase id",
            "completion evidence conflicts with the workflow graph terminal state",
        ),
    ),
}

_BASE_CONTAINMENT_ACTIONS: tuple[str, ...] = (
    "pause new autonomous launches for this capability slug",
    "preserve task ids, provider object ids, agent ids, and raw event fingerprints",
    "snapshot the last successful verification matrix and policy decision evidence",
)

_RISK_CONTAINMENT_ACTIONS: dict[IntegrationRiskTier, tuple[str, ...]] = {
    "internal_harness": (
        "freeze affected workflow graph versions until the drill closes",
        "route queued phases to manual review instead of automatic retry",
    ),
    "external_read": (
        "disable broad provider reads and continue only from cached approved snapshots",
        "require operator approval before expanding the selected read set",
    ),
    "external_write": (
        "disable outbound write actions while preserving read-only ingest",
        "revoke or rotate write-capable credentials if provider evidence is inconsistent",
    ),
}

_BASE_RECOVERY_GATES: tuple[str, ...] = (
    "incident commander confirms evidence is complete and secret-free",
    "verification matrix gates are re-run for the affected capability slug",
    "post-incident summary links each recovery action to task and provider evidence",
)

_CATEGORY_RECOVERY_GATES: dict[CapabilityCategory, tuple[str, ...]] = {
    "communication": (
        "send a single operator-approved status update to affected collaboration threads",
    ),
    "project_management": (
        "reconcile source-of-truth work item statuses before enabling write-back",
    ),
    "knowledge": (
        "re-index only approved source objects and record provenance hashes",
    ),
    "developer": (
        "verify code/CI state is represented by reviewable pull requests before resuming",
    ),
    "crm_support": (
        "confirm customer-visible responses or record updates have approval evidence",
    ),
    "commerce_billing": (
        "confirm no financial mutation can replay without a fresh approval decision",
    ),
    "workflow_harness": (
        "replay affected phases from the last verified idempotency checkpoint",
    ),
}

_CATEGORY_OPERATOR_ROLES: dict[CapabilityCategory, tuple[str, ...]] = {
    "communication": ("incident_commander", "integration_owner", "customer_contact"),
    "project_management": ("incident_commander", "integration_owner", "work_tracker_owner"),
    "knowledge": ("incident_commander", "integration_owner", "knowledge_owner"),
    "developer": ("incident_commander", "integration_owner", "repo_owner"),
    "crm_support": ("incident_commander", "integration_owner", "customer_contact"),
    "commerce_billing": ("incident_commander", "integration_owner", "finance_owner"),
    "workflow_harness": ("incident_commander", "integration_owner"),
}


def compile_integration_incident_drill(slug: str) -> dict | None:
    """Return a JSON-ready incident drill for one catalog capability."""

    capability = get_integration_capability(slug)
    if capability is None:
        return None

    matrix = compile_integration_verification_matrix(slug)
    if matrix is None:
        return None

    risk_tier = matrix["risk_tier"]
    return {
        "object": "integration_incident_drill",
        "capability_slug": capability.slug,
        "capability_name": capability.name,
        "category": capability.category,
        "risk_tier": risk_tier,
        "trigger": _TRIGGER_BY_CATEGORY[capability.category].to_dict(),
        "containment_actions": list(
            _BASE_CONTAINMENT_ACTIONS + _RISK_CONTAINMENT_ACTIONS[risk_tier]
        ),
        "recovery_gates": list(
            _BASE_RECOVERY_GATES + _CATEGORY_RECOVERY_GATES[capability.category]
        ),
        "minimum_operator_roles": list(_CATEGORY_OPERATOR_ROLES[capability.category]),
        "customer_update_template": (
            f"We detected an issue in the {capability.name} integration, paused risky "
            "automation, preserved execution evidence, and are validating recovery before "
            "re-enabling writes."
        ),
    }
