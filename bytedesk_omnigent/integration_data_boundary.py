"""Deterministic data-boundary manifests for integration capabilities.

The integration catalog and verification matrix describe what to build and how to
prove rollout quality. This module adds a privacy/security handoff that tells
operators which provider data may enter Omnigent, which mutations may leave it,
and which audit fields must be captured before an autonomous integration is
installed in a tenant environment.
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
class IntegrationDataBoundary:
    """JSON-ready privacy and mutation boundary for one catalog capability."""

    capability_slug: str
    capability_name: str
    category: CapabilityCategory
    risk_tier: IntegrationRiskTier
    inbound_data_classes: tuple[str, ...]
    outbound_mutation_classes: tuple[str, ...]
    secret_boundaries: tuple[str, ...]
    required_audit_fields: tuple[str, ...]
    retention_policy: str

    def to_dict(self) -> dict:
        data = asdict(self)
        data["object"] = "integration_data_boundary_manifest"
        for key in (
            "inbound_data_classes",
            "outbound_mutation_classes",
            "secret_boundaries",
            "required_audit_fields",
        ):
            data[key] = list(data[key])
        return data


_CATEGORY_BOUNDARIES: dict[
    CapabilityCategory, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]
] = {
    "communication": (
        ("workspace_id", "channel_id", "thread_ts", "user_profile", "message_text"),
        ("post_message", "thread_reply", "approval_prompt"),
        (
            "workspace_id",
            "channel_id",
            "thread_ts",
            "provider_event_id",
            "agent_id",
            "task_id",
        ),
    ),
    "project_management": (
        ("workspace_id", "work_item_id", "status", "comment_text", "assignee"),
        ("status_update", "comment_append", "assignment_update"),
        ("workspace_id", "work_item_id", "provider_event_id", "agent_id", "task_id"),
    ),
    "knowledge": (
        ("workspace_id", "document_id", "page_title", "selected_content", "author"),
        ("append_content", "update_selected_content", "create_page"),
        ("workspace_id", "document_id", "provider_event_id", "agent_id", "task_id"),
    ),
    "developer": (
        (
            "repository_id",
            "issue_or_pr_id",
            "commit_sha",
            "check_run_state",
            "review_comment",
        ),
        ("open_pull_request", "append_comment", "update_issue_status"),
        ("repository_id", "issue_or_pr_id", "commit_sha", "agent_id", "task_id"),
    ),
    "crm_support": (
        ("workspace_id", "customer_id", "ticket_id", "conversation_text", "contact_email"),
        ("internal_note", "draft_reply", "tag_update", "assignment_update"),
        ("workspace_id", "customer_id", "ticket_id", "agent_id", "task_id"),
    ),
    "commerce_billing": (
        ("account_id", "customer_id", "order_id", "subscription_id", "invoice_state"),
        ("internal_note", "risk_alert", "approved_billing_action"),
        ("account_id", "customer_id", "order_id", "agent_id", "task_id"),
    ),
    "workflow_harness": (
        (
            "workflow_blueprint_id",
            "phase_inputs",
            "agent_role",
            "verification_evidence",
        ),
        ("create_task", "record_evidence"),
        ("workflow_blueprint_id", "phase_id", "agent_id", "task_id"),
    ),
}

_EXTERNAL_SECRET_BOUNDARIES = (
    "OAuth tokens stay in the configured secret backend and are never included "
    "in task payloads.",
    "Webhook signatures or verification secrets are compared at ingress and "
    "redacted from evidence.",
)

_INTERNAL_SECRET_BOUNDARIES = (
    "Workflow definitions may reference secret names only; resolved secret values "
    "stay in the runtime secret backend.",
)

_DEFAULT_RETENTION_POLICY = (
    "Retain normalized task/evidence metadata; do not retain raw provider payloads beyond "
    "replay/debug windows."
)


def compile_integration_data_boundary(slug: str) -> dict | None:
    """Return a JSON-ready data-boundary manifest for a catalog capability."""

    capability = get_integration_capability(slug)
    if capability is None:
        return None

    matrix = compile_integration_verification_matrix(slug)
    if matrix is None:
        return None

    inbound, outbound, audit = _CATEGORY_BOUNDARIES[capability.category]
    secret_boundaries = (
        _INTERNAL_SECRET_BOUNDARIES
        if matrix["risk_tier"] == "internal_harness"
        else _EXTERNAL_SECRET_BOUNDARIES
    )

    return IntegrationDataBoundary(
        capability_slug=capability.slug,
        capability_name=capability.name,
        category=capability.category,
        risk_tier=matrix["risk_tier"],
        inbound_data_classes=inbound,
        outbound_mutation_classes=outbound,
        secret_boundaries=secret_boundaries,
        required_audit_fields=audit,
        retention_policy=_DEFAULT_RETENTION_POLICY,
    ).to_dict()
