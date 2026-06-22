"""Deterministic service-to-agent blueprints for third-party integrations.

This module is deliberately pure data + formatting: it does not read secrets,
call provider APIs, or mutate stores. Product surfaces and autonomous loop agents
can use it to preview the agent that should be created once a connected app is
authorized.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IntegrationAgentService:
    """A supported third-party service target for agent creation previews."""

    slug: str
    display_name: str
    agent_role: str
    suggested_name: str
    auth_model: str
    recommended_scopes: tuple[str, ...]
    trigger_events: tuple[str, ...]
    primary_actions: tuple[str, ...]
    business_value: str
    priority: int


def _service(
    slug: str,
    display_name: str,
    agent_role: str,
    suggested_name: str,
    auth_model: str,
    recommended_scopes: tuple[str, ...],
    trigger_events: tuple[str, ...],
    primary_actions: tuple[str, ...],
    business_value: str,
    priority: int,
) -> IntegrationAgentService:
    """Construct a catalog entry while keeping the table compact."""
    return IntegrationAgentService(
        slug=slug,
        display_name=display_name,
        agent_role=agent_role,
        suggested_name=suggested_name,
        auth_model=auth_model,
        recommended_scopes=recommended_scopes,
        trigger_events=trigger_events,
        primary_actions=primary_actions,
        business_value=business_value,
        priority=priority,
    )


_INTEGRATION_AGENT_SERVICES: tuple[IntegrationAgentService, ...] = (
    _service(
        "slack",
        "Slack",
        "Slack triage and response agent",
        "Slack Triage Captain",
        "oauth2",
        ("channels:history", "chat:write", "commands", "users:read"),
        ("message.channels", "app_mention", "slash_command"),
        ("summarize_thread", "open_task", "post_response", "escalate_human"),
        "Turns team chat into governed Omnigent tasks and closes the loop in-channel.",
        100,
    ),
    _service(
        "notion",
        "Notion",
        "Notion knowledge and workspace agent",
        "Notion Knowledge Steward",
        "oauth2",
        ("read_content", "update_content", "insert_content"),
        ("page.created", "page.updated", "database.updated"),
        ("search_workspace", "sync_task_brief", "update_page", "request_review"),
        "Keeps customer/project knowledge synchronized with autonomous task execution.",
        96,
    ),
    _service(
        "github",
        "GitHub",
        "GitHub issue and pull request agent",
        "GitHub Delivery Agent",
        "github_app",
        ("issues:read", "issues:write", "pull_requests:read", "contents:read"),
        ("issues.opened", "issue_comment.created", "pull_request.opened"),
        ("triage_issue", "draft_plan", "comment_status", "link_session"),
        "Converts repository events into auditable engineering-agent workstreams.",
        94,
    ),
    _service(
        "linear",
        "Linear",
        "Linear work item execution agent",
        "Linear Delivery Agent",
        "oauth2",
        ("read", "write", "issues:create", "comments:create"),
        ("Issue", "Comment", "IssueLabel"),
        ("claim_issue", "compile_task_brief", "post_progress", "request_approval"),
        "Makes product backlog items directly executable by Omnigent agents.",
        92,
    ),
    _service(
        "jira",
        "Jira",
        "Jira ticket execution agent",
        "Jira Delivery Agent",
        "oauth2",
        ("read:jira-work", "write:jira-work", "manage:jira-webhook"),
        ("jira:issue_created", "jira:issue_updated", "comment_created"),
        ("classify_ticket", "create_subtasks", "post_comment", "escalate_blocker"),
        "Bridges enterprise ticket queues into deterministic Omnigent task handling.",
        90,
    ),
    _service(
        "google-workspace",
        "Google Workspace",
        "Google Workspace productivity agent",
        "Workspace Operations Agent",
        "oauth2",
        ("drive.readonly", "documents", "gmail.modify", "calendar.events"),
        ("drive.file.changed", "gmail.message", "calendar.event.updated"),
        ("summarize_doc", "draft_reply", "schedule_followup", "create_task"),
        "Lets agents operate across documents, email, and calendar with scoped consent.",
        88,
    ),
    _service(
        "hubspot",
        "HubSpot",
        "HubSpot CRM follow-up agent",
        "HubSpot Revenue Agent",
        "oauth2",
        ("crm.objects.contacts.read", "crm.objects.deals.read", "crm.objects.notes.write"),
        ("contact.creation", "deal.propertyChange", "conversation.creation"),
        ("qualify_lead", "summarize_account", "draft_followup", "create_note"),
        "Automates revenue operations while preserving CRM audit trails.",
        84,
    ),
    _service(
        "zendesk",
        "Zendesk",
        "Zendesk support resolution agent",
        "Zendesk Support Agent",
        "oauth2",
        ("tickets:read", "tickets:write", "users:read"),
        ("ticket.created", "ticket.updated", "comment.created"),
        ("classify_ticket", "draft_resolution", "escalate_sla", "update_ticket"),
        "Converts support tickets into governed autonomous resolution flows.",
        82,
    ),
)

_SERVICES_BY_SLUG = {service.slug: service for service in _INTEGRATION_AGENT_SERVICES}


def list_integration_agent_services() -> list[dict[str, Any]]:
    """Return ranked service summaries for integration-agent creation previews."""
    return [_service_summary(service) for service in _INTEGRATION_AGENT_SERVICES]


def get_integration_agent_blueprint(slug: str) -> dict[str, Any] | None:
    """Return a deterministic agent-creation blueprint for a service slug."""
    normalized_slug = slug.strip().lower()
    service = _SERVICES_BY_SLUG.get(normalized_slug)
    if service is None:
        return None

    return {
        "service": _service_summary(service),
        "agent_blueprint": {
            "suggested_name": service.suggested_name,
            "description": service.business_value,
            "harness": "claude",
            "instructions": _instructions_for(service),
            "capabilities": [
                f"integration.{service.slug}.intake",
                f"integration.{service.slug}.sync",
                f"integration.{service.slug}.escalate",
            ],
            "starter_tools": [
                "sys_read_inbox",
                "sys_call_async",
                "sys_post_comment",
            ],
            "governance": {
                "write_actions_require_approval": True,
                "store_external_ids": True,
                "dead_letter_queue": f"integration.{service.slug}.dead_letter",
            },
        },
        "launch_checklist": [
            "Create or select the ByteDesk workspace that will own this connected app.",
            (
                f"Complete {service.auth_model} authorization for "
                f"{service.display_name} with the recommended scopes."
            ),
            f"Bind inbound {service.display_name} events to a ByteDesk task queue or agent inbox.",
            "Run a dry-run task against the agent before enabling autonomous writes.",
        ],
    }


def supported_integration_agent_slugs() -> list[str]:
    """Return supported service slugs in rank order."""
    return [service.slug for service in _INTEGRATION_AGENT_SERVICES]


def _service_summary(service: IntegrationAgentService) -> dict[str, Any]:
    """Convert a service entry to a JSON-ready summary."""
    return {
        "slug": service.slug,
        "display_name": service.display_name,
        "agent_role": service.agent_role,
        "auth_model": service.auth_model,
        "recommended_scopes": list(service.recommended_scopes),
        "trigger_events": list(service.trigger_events),
        "primary_actions": list(service.primary_actions),
        "business_value": service.business_value,
        "priority": service.priority,
    }


def _instructions_for(service: IntegrationAgentService) -> str:
    """Build concise agent instructions for the selected integration target."""
    return (
        f"You are the {service.agent_role} for {service.display_name}. "
        "Convert inbound third-party events into ByteDesk tasks, preserve the "
        "external record ID in every update, ask for approval before mutating "
        "external systems, and escalate to a human when confidence is low."
    )
