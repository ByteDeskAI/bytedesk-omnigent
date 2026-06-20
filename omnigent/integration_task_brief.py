"""Deterministic task briefs for third-party integration events.

This module gives webhook/OAuth/service-integration surfaces a small,
pure compiler that turns a normalized SaaS event into an agent-ready
handoff package. It intentionally avoids provider SDKs and I/O so callers
can use it from HTTP ingress, schedulers, tests, and replay tooling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_SAFE_SCALAR_TYPES = (str, int, float, bool)
_DEFAULT_NEXT_STEPS: tuple[str, ...] = (
    "Inspect the source event and payload facts.",
    "Decide whether the event requires agent action or can be acknowledged.",
    "Execute the objective, then post a concise outcome back to the integration surface.",
)

_PROVIDER_CAPABILITIES: dict[str, str] = {
    "airtable": "record-operations",
    "asana": "project-triage",
    "discord": "community-triage",
    "github": "pull-request-triage",
    "google-workspace": "workspace-collaboration",
    "hubspot": "crm-operations",
    "intercom": "support-triage",
    "jira": "issue-triage",
    "linear": "issue-triage",
    "microsoft-teams": "collaborative-triage",
    "monday": "project-triage",
    "notion": "knowledge-base-operations",
    "salesforce": "crm-operations",
    "shopify": "commerce-operations",
    "slack": "collaborative-triage",
    "stripe": "billing-operations",
    "trello": "project-triage",
    "zendesk": "support-triage",
}


@dataclass(frozen=True, kw_only=True)
class IntegrationEvent:
    """Provider-neutral event shape for integration-to-agent handoffs.

    :param provider: Source service name, e.g. ``"GitHub"`` or ``"Slack"``.
    :param event_type: Provider event type, e.g. ``"pull_request.opened"``.
    :param resource_id: Stable provider resource id for dedupe/correlation.
    :param title: Human-readable task title from the event.
    :param actor: User/app that caused the event, when known.
    :param url: Deep link back to the source service, when available.
    :param summary: Short context sentence for the agent.
    :param payload: Optional normalized payload. Only safe scalar/list facts are
        copied into the brief; bulky nested objects stay out of the prompt.
    """

    provider: str
    event_type: str
    resource_id: str
    title: str
    actor: str | None = None
    url: str | None = None
    summary: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


def compile_task_brief(event: IntegrationEvent, *, objective: str | None = None) -> dict[str, Any]:
    """Compile an integration event into a deterministic agent task brief.

    :param event: Normalized third-party event.
    :param objective: Optional caller-supplied mission for the agent. When
        absent, the compiler derives a concise default from the event.
    :returns: A JSON-serializable mapping with ``source``, ``task``, and
        ``handoff`` sections.
    :raises ValueError: If a core routing field is blank.
    """
    provider = _require_text("provider", event.provider)
    event_type = _require_text("event_type", event.event_type)
    resource_id = _require_text("resource_id", event.resource_id)
    title = _require_text("title", event.title)
    provider_key = _provider_key(provider)
    selected_objective = (objective or "").strip() or f"Handle {provider} {event_type} for {title}"

    return {
        "version": 1,
        "source": {
            "provider": provider_key,
            "event_type": event_type,
            "resource_id": resource_id,
            "url": event.url,
        },
        "task": {
            "title": f"{provider}: {title}",
            "objective": selected_objective,
            "context": event.summary or "",
            "requested_by": event.actor,
            "routing_labels": (
                "integration",
                f"provider:{provider_key}",
                f"event:{event_type}",
            ),
            "payload_facts": _payload_facts(event.payload),
        },
        "handoff": {
            "recommended_agent_capabilities": _recommended_capabilities(provider_key),
            "next_steps": _DEFAULT_NEXT_STEPS,
        },
    }


def compile_task_brief_markdown(event: IntegrationEvent, *, objective: str | None = None) -> str:
    """Render :func:`compile_task_brief` as a stable spawned-agent prompt.

    :param event: Normalized third-party event.
    :param objective: Optional task objective to include in the brief.
    :returns: Markdown text suitable for session prompts, PR comments, or logs.
    """
    brief = compile_task_brief(event, objective=objective)
    provider_name = event.provider.strip()
    task = brief["task"]
    handoff = brief["handoff"]
    source = brief["source"]
    lines = [
        f"# {provider_name} integration task brief",
        "",
        f"- Event: {source['event_type']}",
        f"- Resource: {source['resource_id']}",
        f"- Requested by: {task['requested_by'] or 'unknown'}",
        f"- Objective: {task['objective']}",
        f"- Context: {task['context'] or 'No summary provided.'}",
        f"- Routing labels: {', '.join(task['routing_labels'])}",
        f"- Recommended capabilities: {', '.join(handoff['recommended_agent_capabilities'])}",
        "",
        "## Next steps",
    ]
    lines.extend(f"{index}. {step}" for index, step in enumerate(handoff["next_steps"], start=1))
    return "\n".join(lines)


def _require_text(field_name: str, value: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"integration event {field_name} must be a non-empty string")
    return text


def _provider_key(provider: str) -> str:
    return provider.strip().lower().replace(" ", "-").replace("_", "-")


def _recommended_capabilities(provider_key: str) -> tuple[str, str, str]:
    provider_capability = _PROVIDER_CAPABILITIES.get(provider_key, "event-triage")
    return ("third-party-integration", provider_key, provider_capability)


def _payload_facts(payload: dict[str, Any]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    for key, value in payload.items():
        if _is_safe_fact(value):
            facts[key] = value
        elif isinstance(value, dict):
            for child_key, child_value in value.items():
                if _is_safe_fact(child_value):
                    facts[f"{key}.{child_key}"] = child_value
    return facts


def _is_safe_fact(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, _SAFE_SCALAR_TYPES):
        return True
    if isinstance(value, list):
        return all(isinstance(item, _SAFE_SCALAR_TYPES) for item in value)
    return False
