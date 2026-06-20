"""Deterministic integration handoff package compiler.

Turns a third-party event into an agent-ready work package ByteDesk Platform can
preview, persist, or hand to an autonomous agent. The compiler is pure so
connectors can use it before any provider writeback or task execution occurs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

_CODE_PROVIDERS = {"github", "gitlab", "bitbucket"}
_REVENUE_PROVIDERS = {"hubspot", "salesforce", "stripe", "shopify"}
_SUPPORT_PROVIDERS = {"slack", "zendesk", "intercom", "microsoft-teams", "teams", "discord"}
_PROJECT_PROVIDERS = {"linear", "jira", "trello", "asana", "monday", "airtable", "notion"}
_HIGH_PRIORITY_EVENTS = ("deal.", "payment.", "charge.", "checkout.", "order.", "ticket.")


@dataclass(frozen=True)
class IntegrationHandoffPackage:
    """Agent-ready package compiled from one external integration event."""

    provider: str
    workspace_id: str
    event_type: str
    external_id: str
    correlation_id: str
    agent_brief: dict[str, str | None]
    routing: dict[str, Any]
    workflow_steps: list[str]
    acceptance_checks: list[str]
    payload_excerpt: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation for routes and PR previews."""
        return asdict(self)


def compile_integration_handoff_package(
    *,
    provider: str,
    workspace_id: str,
    event_type: str,
    external_id: str,
    actor: str | None = None,
    title: str | None = None,
    url: str | None = None,
    payload: dict[str, Any] | None = None,
    requested_capabilities: list[str] | tuple[str, ...] | None = None,
) -> IntegrationHandoffPackage:
    """Compile a deterministic external-event handoff contract.

    The returned package deliberately contains no secrets and performs no network
    calls. It gives ByteDesk Platform a stable correlation id, routing hints, a
    concise agent brief, deterministic workflow stages, and acceptance checks that
    make provider writeback auditable.
    """
    provider_slug = _slug(provider)
    event = event_type.strip()
    external = str(external_id).strip()
    correlation_id = (
        f"integration-handoff:v1:{provider_slug}:{workspace_id}:{event}:{external}"
    )
    display_provider = _display_provider(provider_slug)
    summary = f"{display_provider} {event}"
    if actor:
        summary = f"{summary} from {actor}"
    summary = f"{summary} needs autonomous follow-up."

    brief_title = title or f"Handle {display_provider} {event}"
    capabilities = [
        str(item).strip()
        for item in (requested_capabilities or [])
        if str(item).strip()
    ]
    return IntegrationHandoffPackage(
        provider=provider_slug,
        workspace_id=workspace_id,
        event_type=event,
        external_id=external,
        correlation_id=correlation_id,
        agent_brief={"title": brief_title, "summary": summary, "source_url": url},
        routing={
            "requested_capabilities": capabilities,
            "recommended_agent_type": _recommended_agent_type(provider_slug, event, capabilities),
            "priority": _priority(provider_slug, event),
        },
        workflow_steps=[
            "normalize_external_context",
            "select_or_create_agent",
            "hydrate_agent_brief",
            "execute_agent_task",
            "record_outcome",
            "write_back_to_provider",
        ],
        acceptance_checks=[
            "source_event_is_traceable",
            "agent_brief_has_title_and_summary",
            "provider_writeback_is_idempotent",
        ],
        payload_excerpt=dict(payload or {}),
    )


def _slug(value: str) -> str:
    return value.strip().lower().replace("_", "-").replace(" ", "-")


def _display_provider(provider_slug: str) -> str:
    if provider_slug == "github":
        return "GitHub"
    if provider_slug == "hubspot":
        return "HubSpot"
    if provider_slug in {"microsoft-teams", "teams"}:
        return "Microsoft Teams"
    return provider_slug.replace("-", " ").title()


def _recommended_agent_type(provider: str, event_type: str, capabilities: list[str]) -> str:
    capability_text = " ".join(capabilities).lower()
    if provider in _CODE_PROVIDERS or "code" in capability_text:
        return "code-reviewer"
    if provider in _REVENUE_PROVIDERS:
        return "revenue-operations-agent"
    if provider in _SUPPORT_PROVIDERS or "support" in capability_text:
        return "support-agent"
    if provider in _PROJECT_PROVIDERS or any(
        token in event_type for token in ("issue", "task", "card", "page")
    ):
        return "project-operations-agent"
    return "integration-operations-agent"


def _priority(provider: str, event_type: str) -> str:
    if provider in _REVENUE_PROVIDERS:
        return "high"
    if event_type.startswith(_HIGH_PRIORITY_EVENTS):
        return "high"
    return "normal"
