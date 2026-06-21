"""Deterministic demo scenarios for integration capability sales and onboarding."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from bytedesk_omnigent.integration_capabilities import (
    IntegrationCapability,
    get_integration_capability,
)


@dataclass(frozen=True)
class IntegrationDemoScenario:
    """A JSON-ready demo script for one integration capability."""

    capability_slug: str
    capability_name: str
    scenario_slug: str
    entrypoint: str
    sample_trigger: str
    agent_roles: tuple[str, ...]
    demo_steps: tuple[str, ...]
    success_metrics: tuple[str, ...]
    business_case: str
    future_unlocks: tuple[str, ...]

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in ("agent_roles", "demo_steps", "success_metrics", "future_unlocks"):
            data[key] = list(data[key])
        return data


def compile_integration_demo_scenario(slug: str) -> IntegrationDemoScenario | None:
    """Compile one catalog blueprint into a deterministic customer demo scenario."""

    capability = get_integration_capability(slug)
    if capability is None:
        return None

    return IntegrationDemoScenario(
        capability_slug=capability.slug,
        capability_name=capability.name,
        scenario_slug=f"demo-{capability.slug}",
        entrypoint=_entrypoint_for(capability),
        sample_trigger=_sample_trigger_for(capability),
        agent_roles=_agent_roles_for(capability),
        demo_steps=_demo_steps_for(capability),
        success_metrics=_success_metrics_for(capability),
        business_case=capability.business_case,
        future_unlocks=capability.future_unlocks,
    )


def _entrypoint_for(capability: IntegrationCapability) -> str:
    if capability.category == "workflow_harness":
        return "Internal workflow blueprint run"
    if capability.category == "communication" and "slack" in capability.slug:
        return "Slack event or command"
    if capability.category == "communication":
        return "Chat event or command"
    if capability.category == "project_management":
        return "External work item update"
    if capability.category == "knowledge":
        return "Knowledge workspace change"
    if capability.category == "developer":
        return "Repository event"
    if capability.category == "crm_support":
        return "Customer record or support conversation event"
    if capability.category == "commerce_billing":
        return "Revenue or commerce event"
    return "Integration event"


def _sample_trigger_for(capability: IntegrationCapability) -> str:
    if capability.category == "workflow_harness":
        return "A team selects a deterministic workflow blueprint for an autonomous agent run."
    return f"A {capability.name} event requests agent assistance."


def _agent_roles_for(capability: IntegrationCapability) -> tuple[str, ...]:
    if capability.category == "workflow_harness":
        return ("workflow-designer", "specialist-agent", "verification-agent")
    if capability.category == "developer":
        return ("engineering-intake-agent", "coding-agent", "review-verification-agent")
    if capability.category == "knowledge":
        return ("knowledge-curator", "task-execution-agent", "audit-reviewer")
    if capability.category == "crm_support":
        return ("customer-context-agent", "response-drafting-agent", "human-approval-reviewer")
    if capability.category == "commerce_billing":
        return ("revenue-ops-agent", "risk-review-agent", "human-approval-reviewer")
    return ("integration-concierge", "domain-specialist-agent", "human-approval-reviewer")


def _demo_steps_for(capability: IntegrationCapability) -> tuple[str, ...]:
    if capability.category == "workflow_harness":
        return (
            "Select a repeatable customer workflow and capture its typed inputs.",
            "Compile phases into Omnigent tasks with explicit owners, retry policy, "
            "and evidence requirements.",
            "Run the workflow in dry-run mode and collect completion evidence from every phase.",
            "Promote the verified blueprint into a reusable ByteDesk Platform template.",
        )

    return (
        f"Receive and normalize the {capability.name} trigger into an Omnigent integration event.",
        "Create or update the linked Omnigent task with external IDs for idempotency.",
        "Assign the task to the best-fit specialist agent and require approval for write actions.",
        "Publish the final outcome back to the source system with audit evidence attached.",
    )


def _success_metrics_for(capability: IntegrationCapability) -> tuple[str, ...]:
    if capability.category == "workflow_harness":
        return (
            "Blueprint run completes with evidence recorded for every phase.",
            "Reusable template can be launched again with the same inputs and "
            "deterministic gates.",
            "Failed phases produce actionable retry or rollback instructions.",
        )

    return (
        "External event is accepted, deduplicated, and linked to an Omnigent task.",
        "Specialist agent produces a verifiable outcome without bypassing approval policy.",
        "Customer-facing system receives an audited status update or draft response.",
    )
