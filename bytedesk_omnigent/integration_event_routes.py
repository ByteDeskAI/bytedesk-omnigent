"""Deterministic connected-app event routing plans.

A small compiler turns a third-party event descriptor into the Omnigent control
plane shape needed to wire provider webhooks/OAuth events into autonomous work:
ingress source, binding match key, specialist capability, task kind,
idempotency key, approval/writeback posture, and deterministic harness steps.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class IntegrationEventRoutePlan:
    """A previewable route from an external app event to Omnigent execution."""

    provider: str
    ingress_source: str
    match_key: str
    required_capability: str
    task_kind: str
    idempotency_key: str
    approval_required: bool
    writeback_policy: str
    desired_outcome: str | None
    steps: tuple[dict[str, str], ...]

    def to_dict(self) -> dict:
        """Return a JSON-serializable representation for API responses."""
        return asdict(self)


@dataclass(frozen=True)
class _ProviderRouteProfile:
    capability: str
    task_kind: str
    approval_for_writeback: bool = True


_PROVIDER_PROFILES: dict[str, _ProviderRouteProfile] = {
    "github": _ProviderRouteProfile("developer.work_item", "external.github.issue"),
    "gitlab": _ProviderRouteProfile("developer.work_item", "external.gitlab.issue"),
    "linear": _ProviderRouteProfile("project_management.work_item", "external.linear.issue"),
    "jira": _ProviderRouteProfile("project_management.work_item", "external.jira.issue"),
    "trello": _ProviderRouteProfile("project_management.work_item", "external.trello.card"),
    "asana": _ProviderRouteProfile("project_management.work_item", "external.asana.task"),
    "monday": _ProviderRouteProfile("project_management.work_item", "external.monday.item"),
    "notion": _ProviderRouteProfile("knowledge.workspace_update", "external.notion.page"),
    "google-workspace": _ProviderRouteProfile(
        "knowledge.workspace_update", "external.google_workspace.event"
    ),
    "slack": _ProviderRouteProfile("chatops.message_triage", "external.slack.event"),
    "microsoft-teams": _ProviderRouteProfile(
        "chatops.message_triage", "external.microsoft_teams.event"
    ),
    "teams": _ProviderRouteProfile("chatops.message_triage", "external.microsoft_teams.event"),
    "discord": _ProviderRouteProfile("chatops.message_triage", "external.discord.event"),
    "zendesk": _ProviderRouteProfile("support.ticket_resolution", "external.zendesk.ticket"),
    "intercom": _ProviderRouteProfile(
        "support.ticket_resolution", "external.intercom.conversation"
    ),
    "hubspot": _ProviderRouteProfile("crm.account_update", "external.hubspot.object"),
    "salesforce": _ProviderRouteProfile("crm.account_update", "external.salesforce.object"),
    "stripe": _ProviderRouteProfile("revenue.payment_ops", "external.stripe.event"),
    "shopify": _ProviderRouteProfile("commerce.order_ops", "external.shopify.event"),
    "airtable": _ProviderRouteProfile("data.records_update", "external.airtable.record"),
}


def compile_event_route(
    *,
    provider: str,
    event_type: str,
    subject_id: str,
    workspace_id: str | None = None,
    desired_outcome: str | None = None,
    writeback: bool = False,
) -> IntegrationEventRoutePlan:
    """Compile a deterministic external-event route plan.

    The compiler is intentionally side-effect free: ByteDesk Platform, an
    integration wizard, or an autonomous loop can preview how an app event would
    map into Omnigent before installing webhooks, registering bindings, or
    creating/resuming tasks.
    """
    provider_slug = _slug(provider)
    event_key = _normalize_event_type(event_type)
    subject_key = _normalize_subject(subject_id)
    workspace_key = _normalize_workspace(workspace_id)
    profile = _PROVIDER_PROFILES.get(
        provider_slug,
        _ProviderRouteProfile(
            "integration.generic_event", f"external.{provider_slug}.event", False
        ),
    )
    approval_required = bool(writeback and profile.approval_for_writeback)
    writeback_policy = (
        "requires_approval" if approval_required else "enabled" if writeback else "disabled"
    )
    steps = [
        {"id": "verify_connected_app", "description": "Confirm installed app and scopes."},
        {"id": "normalize_event", "description": "Normalize provider payload into route input."},
        {
            "id": "resolve_specialist",
            "description": f"Find agent with capability {profile.capability}.",
        },
        {
            "id": "create_or_resume_task",
            "description": "Use the idempotency key to avoid duplicate autonomous work.",
        },
    ]
    if approval_required:
        steps.append(
            {
                "id": "approval_gate",
                "description": "Require human approval before third-party writeback.",
            }
        )
    if writeback:
        steps.append(
            {
                "id": "writeback_outcome",
                "description": "Post the approved Omnigent outcome to the provider.",
            }
        )
    return IntegrationEventRoutePlan(
        provider=provider_slug,
        ingress_source=provider_slug,
        match_key=event_key,
        required_capability=profile.capability,
        task_kind=profile.task_kind,
        idempotency_key=(
            f"integration-route:{provider_slug}:{workspace_key}:{event_key}:{subject_key}"
        ),
        approval_required=approval_required,
        writeback_policy=writeback_policy,
        desired_outcome=desired_outcome.strip() if desired_outcome else None,
        steps=tuple(steps),
    )


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return cleaned or "unknown"


def _normalize_event_type(value: str) -> str:
    cleaned = value.strip().lower().replace(" ", ".")
    return cleaned or "*"


def _normalize_subject(value: str) -> str:
    return value.strip() or "unknown"


def _normalize_workspace(value: str | None) -> str:
    if value is None:
        return "global"
    return value.strip() or "global"
