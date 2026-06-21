"""Credentialless sandbox fixtures for integration capability validation.

The verification matrix says which rollout evidence is required. This module
adds deterministic provider-event fixtures that operators and autonomous loops
can replay without live OAuth credentials before investing in a real connector.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from bytedesk_omnigent.integration_capabilities import (
    CapabilityCategory,
    get_integration_capability,
)
from bytedesk_omnigent.integration_verification_matrix import IntegrationRiskTier


@dataclass(frozen=True)
class IntegrationSandboxFixture:
    """One synthetic provider event and the Omnigent signal it should produce."""

    id: str
    title: str
    provider_event: str
    expected_signal_type: str
    assertions: tuple[str, ...]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["assertions"] = list(self.assertions)
        return data


_CATEGORY_FIXTURES: dict[CapabilityCategory, tuple[IntegrationSandboxFixture, ...]] = {
    "communication": (
        IntegrationSandboxFixture(
            id="communication-message-received",
            title="Receive a provider conversation message",
            provider_event="message.created",
            expected_signal_type="integration.message.received",
            assertions=(
                "source thread or channel id is preserved for replies",
                "sender identity is retained without exposing raw credentials",
                "duplicate provider event ids map to the same normalized signal",
            ),
        ),
        IntegrationSandboxFixture(
            id="communication-approval-response",
            title="Convert an approval interaction into an Omnigent signal",
            provider_event="interactive.approval_submitted",
            expected_signal_type="integration.approval.responded",
            assertions=(
                "approval actor is captured for audit",
                "task id is correlated to the original prompt",
                "denied approvals produce no provider-side mutation",
            ),
        ),
    ),
    "project_management": (
        IntegrationSandboxFixture(
            id="work-item-created",
            title="Import a provider work item as an Omnigent Task",
            provider_event="work_item.created",
            expected_signal_type="integration.work_item.created",
            assertions=(
                "provider item id becomes a stable external reference",
                "title, description, labels, and priority are normalized",
                "initial task state is deterministic for repeated fixture runs",
            ),
        ),
        IntegrationSandboxFixture(
            id="work-item-status-changed",
            title="Map an external status transition",
            provider_event="work_item.status_changed",
            expected_signal_type="integration.work_item.updated",
            assertions=(
                "external status maps to an allowed Omnigent Task lifecycle state",
                "source-of-truth ownership is retained on the provider object",
                "write-back idempotency key is derived from provider item id and transition id",
            ),
        ),
        IntegrationSandboxFixture(
            id="work-item-comment-added",
            title="Preserve comment attribution and task context",
            provider_event="work_item.comment_added",
            expected_signal_type="integration.work_item.comment_added",
            assertions=(
                "comment author and timestamp survive normalization",
                "task correlation uses provider item id rather than mutable titles",
                "agent-visible summary excludes provider secrets and tokens",
            ),
        ),
    ),
    "knowledge": (
        IntegrationSandboxFixture(
            id="knowledge-document-selected",
            title="Index an explicitly selected knowledge object",
            provider_event="document.selected",
            expected_signal_type="integration.knowledge.selected",
            assertions=(
                "document id and workspace id are preserved",
                "read scope is limited to the selected object",
                "provenance is attached to every generated memory chunk",
            ),
        ),
        IntegrationSandboxFixture(
            id="knowledge-document-updated",
            title="Append an agent-authored update with provenance",
            provider_event="document.update_requested",
            expected_signal_type="integration.knowledge.update_requested",
            assertions=(
                "write request includes source task and agent ids",
                "broad workspace writes are rejected by default",
                "fixture output is safe for operator review without credentials",
            ),
        ),
    ),
    "developer": (
        IntegrationSandboxFixture(
            id="developer-check-failed",
            title="Turn a failed automation check into a repair task",
            provider_event="check_run.failed",
            expected_signal_type="integration.developer.check_failed",
            assertions=(
                "repository and pull request references are retained",
                "failure summary becomes task evidence",
                "repair actions route through reviewable pull requests",
            ),
        ),
        IntegrationSandboxFixture(
            id="developer-review-commented",
            title="Normalize a review comment for coding-agent follow-up",
            provider_event="pull_request_review_comment.created",
            expected_signal_type="integration.developer.review_comment_created",
            assertions=(
                "comment path and line range are preserved",
                "reviewer attribution is captured",
                "least-privilege installation context is represented without tokens",
            ),
        ),
    ),
    "crm_support": (
        IntegrationSandboxFixture(
            id="customer-ticket-opened",
            title="Import a customer-facing ticket for triage",
            provider_event="ticket.created",
            expected_signal_type="integration.customer_record.ticket_created",
            assertions=(
                "customer and ticket ids are retained for handoff",
                "public reply drafts require approval evidence",
                "PII-bearing fields are summarized for operator-safe display",
            ),
        ),
        IntegrationSandboxFixture(
            id="customer-note-append-requested",
            title="Validate controlled CRM/support note append",
            provider_event="customer_record.note_append_requested",
            expected_signal_type="integration.customer_record.note_append_requested",
            assertions=(
                "before and after summaries are recorded",
                "source task and agent ids are correlated",
                "mutating write remains behind an approval gate",
            ),
        ),
    ),
    "commerce_billing": (
        IntegrationSandboxFixture(
            id="commerce-order-risk-detected",
            title="Detect a revenue event that needs agent follow-up",
            provider_event="order.risk_detected",
            expected_signal_type="integration.commerce.order_risk_detected",
            assertions=(
                "order and customer ids are retained",
                "read-only context is separated from payment mutations",
                "financial anomaly summary includes a source object link",
            ),
        ),
        IntegrationSandboxFixture(
            id="commerce-refund-requested",
            title="Validate approval gating for a revenue mutation",
            provider_event="refund.requested",
            expected_signal_type="integration.commerce.refund_requested",
            assertions=(
                "refund mutation requires explicit approval evidence",
                "denial produces no provider-side mutation",
                "idempotency key is derived from provider refund request id",
            ),
        ),
    ),
    "workflow_harness": (
        IntegrationSandboxFixture(
            id="workflow-blueprint-phase-graph",
            title="Compile a deterministic workflow phase graph",
            provider_event="workflow_blueprint.submitted",
            expected_signal_type="integration.workflow_blueprint.received",
            assertions=(
                "phase ids are stable across repeated compiles",
                "typed inputs and outputs are present for each phase",
                "terminal phases declare completion evidence requirements",
            ),
        ),
        IntegrationSandboxFixture(
            id="workflow-blueprint-evidence-complete",
            title="Capture terminal evidence for every workflow phase",
            provider_event="workflow_blueprint.phase_completed",
            expected_signal_type="integration.workflow_blueprint.phase_completed",
            assertions=(
                "completion evidence references the stable phase id",
                "phase outputs match the declared output contract",
                "replayed completion events return the same terminal status",
            ),
        ),
    ),
}


def compile_integration_sandbox_fixtures(slug: str) -> dict | None:
    """Return credentialless sandbox fixtures for one catalog capability."""

    capability = get_integration_capability(slug)
    if capability is None:
        return None

    fixtures = _CATEGORY_FIXTURES[capability.category]
    return {
        "object": "integration_sandbox_fixture_bundle",
        "capability_slug": capability.slug,
        "capability_name": capability.name,
        "category": capability.category,
        "mode": "credentialless",
        "risk_tier": _risk_tier(capability.category, capability.required_scopes),
        "fixtures": [fixture.to_dict() for fixture in fixtures],
        "operator_notes": (
            "no live credentials required; fixtures use synthetic provider events; "
            "assertions are deterministic and safe for local CI or Platform preview"
        ),
    }


def _risk_tier(
    category: CapabilityCategory, required_scopes: tuple[str, ...]
) -> IntegrationRiskTier:
    if category == "workflow_harness":
        return "internal_harness"
    if any("write" in scope.lower() or scope.endswith(".write") for scope in required_scopes):
        return "external_write"
    return "external_read"
