"""Deterministic integration workflow plan compiler.

This is the Archon-style harness seam for connected apps: before Omnigent runs an
agent, a connector can ask for a deterministic plan that normalizes provider
context, resolves the right capability, inserts approval gates for risky systems,
and writes outcomes back to the source app.  The compiler is pure and side-effect
free so Platform, Office, webhooks, and future OAuth connectors can preview the
same plan they will later execute through tasks/tool-steps.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any

_PROVIDER_CAPABILITIES: dict[str, str] = {
    "github": "developer.work_item",
    "linear": "project_management.work_item",
    "jira": "project_management.work_item",
    "trello": "project_management.card",
    "asana": "project_management.work_item",
    "monday": "project_management.work_item",
    "notion": "knowledge.page",
    "google_workspace": "knowledge.workspace",
    "slack": "collaboration.thread",
    "microsoft_teams": "collaboration.thread",
    "teams": "collaboration.thread",
    "discord": "collaboration.thread",
    "zendesk": "support.ticket",
    "intercom": "support.conversation",
    "hubspot": "crm.record",
    "salesforce": "crm.record",
    "stripe": "commerce.account",
    "shopify": "commerce.order",
    "airtable": "database.record",
    "generic": "integration.request",
}

_APPROVAL_DEFAULT_PROVIDERS = {
    "airtable",
    "hubspot",
    "intercom",
    "salesforce",
    "shopify",
    "stripe",
    "zendesk",
}

_PROVIDER_ALIASES = {"ms_teams": "microsoft_teams", "msteams": "microsoft_teams"}


@dataclass(frozen=True)
class IntegrationWorkflowStep:
    """One deterministic or agentic step in a connected-app workflow plan."""

    key: str
    kind: str
    name: str
    description: str
    deterministic: bool
    inputs: dict[str, Any]
    idempotency_key: str


@dataclass(frozen=True)
class IntegrationWorkflowPlan:
    """Compiled connected-app plan ready for a workflow harness or task runner."""

    provider: str
    capability: str
    goal: str
    object_ref: str
    requester: str | None
    idempotency_key: str
    approval_required: bool
    writeback_enabled: bool
    task_template: dict[str, Any]
    steps: tuple[IntegrationWorkflowStep, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return asdict(self)


def normalize_provider(provider: str) -> str:
    """Normalize a provider slug and reject unknown connector families."""
    slug = re.sub(r"[^a-z0-9]+", "_", provider.strip().lower()).strip("_")
    slug = _PROVIDER_ALIASES.get(slug, slug)
    if slug not in _PROVIDER_CAPABILITIES:
        supported = ", ".join(sorted(_PROVIDER_CAPABILITIES))
        raise ValueError(f"unsupported integration provider {provider!r}; supported: {supported}")
    return slug


def compile_integration_workflow_plan(
    *,
    provider: str,
    goal: str,
    object_ref: str,
    requester: str | None = None,
    context_refs: list[str] | None = None,
    idempotency_key: str | None = None,
    require_approval: bool | None = None,
    writeback: bool = True,
) -> IntegrationWorkflowPlan:
    """Compile a deterministic connected-app workflow plan.

    The result deliberately does not execute anything. It gives future Slack,
    Notion, GitHub, Linear/Jira, CRM, support, commerce, and Platform adapters the
    same stable harness shape: deterministic context collection first, exactly one
    agent capability turn, guarded write-back last.
    """
    provider_slug = normalize_provider(provider)
    normalized_context_refs = list(context_refs or [])
    base_key = idempotency_key or _stable_key(
        provider_slug, goal.strip(), object_ref.strip(), requester or ""
    )
    capability = _PROVIDER_CAPABILITIES[provider_slug]
    approval_required = (
        provider_slug in _APPROVAL_DEFAULT_PROVIDERS
        if require_approval is None
        else require_approval
    )

    common_inputs: dict[str, Any] = {
        "provider": provider_slug,
        "object_ref": object_ref,
        "goal": goal,
        "requester": requester,
        "context_refs": normalized_context_refs,
    }
    steps: list[IntegrationWorkflowStep] = [
        _step(
            base_key,
            "01_normalize_event",
            "tool",
            "integration.normalize_event",
            "Normalize the connected-app object into Omnigent's provider-neutral envelope.",
            True,
            common_inputs,
        ),
        _step(
            base_key,
            "02_fetch_context",
            "tool",
            f"providers.{provider_slug}.fetch_context",
            "Fetch only the referenced source-system context needed for this goal.",
            True,
            {
                "provider": provider_slug,
                "object_ref": object_ref,
                "context_refs": normalized_context_refs,
            },
        ),
        _step(
            base_key,
            "03_resolve_assignee",
            "tool",
            "resolve_assignee",
            "Resolve the best Omnigent agent by required capability before any LLM work.",
            True,
            {"capability": capability, "department": None},
        ),
    ]

    if approval_required:
        steps.append(
            _step(
                base_key,
                "04_request_approval",
                "approval",
                "approval.request",
                "Pause for app-scoped approval before customer, revenue, or "
                "system-of-record writes.",
                True,
                {"provider": provider_slug, "object_ref": object_ref, "requester": requester},
            )
        )

    steps.append(
        _step(
            base_key,
            "05_run_capability",
            "agent",
            "agent.run_capability",
            "Run exactly one capability-scoped Omnigent agent turn with normalized context.",
            False,
            {"capability": capability, "goal": goal, "object_ref": object_ref},
        )
    )

    if writeback:
        steps.append(
            _step(
                base_key,
                "06_writeback",
                "tool",
                f"providers.{provider_slug}.writeback",
                "Write a comment, status, artifact link, or structured result back "
                "to the source app.",
                True,
                {"provider": provider_slug, "object_ref": object_ref},
            )
        )

    steps.append(
        _step(
            base_key,
            "07_record_outcome",
            "tool",
            "outcome_record",
            "Record measurable outcome metadata for future routing and governance.",
            True,
            {"provider": provider_slug, "capability": capability, "object_ref": object_ref},
        )
    )

    task_template = {
        "title": f"{provider_slug}: {goal.strip()}",
        "source": f"integration:{provider_slug}",
        "required_capability": capability,
        "priority": 3,
        "payload": {
            "provider": provider_slug,
            "object_ref": object_ref,
            "requester": requester,
            "context_refs": normalized_context_refs,
            "workflow_plan_idempotency_key": base_key,
        },
    }

    return IntegrationWorkflowPlan(
        provider=provider_slug,
        capability=capability,
        goal=goal,
        object_ref=object_ref,
        requester=requester,
        idempotency_key=base_key,
        approval_required=approval_required,
        writeback_enabled=writeback,
        task_template=task_template,
        steps=tuple(steps),
    )


def _stable_key(provider: str, goal: str, object_ref: str, requester: str) -> str:
    digest = hashlib.sha256("|".join([provider, goal, object_ref, requester]).encode()).hexdigest()
    return f"iwp_{digest[:32]}"


def _step(
    base_key: str,
    key: str,
    kind: str,
    name: str,
    description: str,
    deterministic: bool,
    inputs: dict[str, Any],
) -> IntegrationWorkflowStep:
    return IntegrationWorkflowStep(
        key=key,
        kind=kind,
        name=name,
        description=description,
        deterministic=deterministic,
        inputs=inputs,
        idempotency_key=f"{base_key}:{key}",
    )


__all__ = [
    "IntegrationWorkflowPlan",
    "IntegrationWorkflowStep",
    "compile_integration_workflow_plan",
    "normalize_provider",
]
