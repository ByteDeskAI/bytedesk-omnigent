"""Deterministic bundles for packaging integration capabilities into agent offers.

The capability catalog describes individual integration seams. This module groups
those seams into marketable, operator-friendly bundles that can be used by
ByteDesk Platform and autonomous planning agents to create complete agent
workforce offerings instead of one connector at a time.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from bytedesk_omnigent.integration_capabilities import (
    IntegrationCapability,
    get_integration_capability,
)


@dataclass(frozen=True)
class ActivationPhase:
    """One deterministic phase for enabling an integration bundle."""

    id: str
    title: str
    required_output: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class IntegrationCapabilityBundle:
    """A productized group of catalog capabilities for one agent workforce."""

    slug: str
    name: str
    target_agent: str
    capability_slugs: tuple[str, ...]
    implementation_description: str
    business_case: str
    future_unlocks: tuple[str, ...]
    priority_score: int

    def compile(self) -> CompiledIntegrationCapabilityBundle:
        capabilities = tuple(
            capability
            for slug in self.capability_slugs
            if (capability := get_integration_capability(slug)) is not None
        )
        return CompiledIntegrationCapabilityBundle(
            bundle=self,
            capabilities=capabilities,
            activation_sequence=_ACTIVATION_SEQUENCE,
        )

    def to_dict(self) -> dict:
        return self.compile().to_dict()


@dataclass(frozen=True)
class CompiledIntegrationCapabilityBundle:
    """A bundle plus resolved catalog entries and rollout sequence."""

    bundle: IntegrationCapabilityBundle
    capabilities: tuple[IntegrationCapability, ...]
    activation_sequence: tuple[ActivationPhase, ...]

    @property
    def aggregate_priority_score(self) -> int:
        if not self.capabilities:
            return self.bundle.priority_score
        return round(
            sum(capability.priority_score for capability in self.capabilities)
            / len(self.capabilities)
        )

    def to_dict(self) -> dict:
        return {
            "object": "integration_capability_bundle",
            "slug": self.bundle.slug,
            "name": self.bundle.name,
            "target_agent": self.bundle.target_agent,
            "capability_slugs": list(self.bundle.capability_slugs),
            "capabilities": [capability.to_dict() for capability in self.capabilities],
            "activation_sequence": [
                phase.to_dict() for phase in self.activation_sequence
            ],
            "implementation_description": self.bundle.implementation_description,
            "business_case": self.bundle.business_case,
            "future_unlocks": list(self.bundle.future_unlocks),
            "priority_score": self.bundle.priority_score,
            "aggregate_priority_score": self.aggregate_priority_score,
        }


_ACTIVATION_SEQUENCE: tuple[ActivationPhase, ...] = (
    ActivationPhase(
        id="catalog-confirmation",
        title="Confirm catalog coverage and ownership",
        required_output="selected capability slugs, workspace owner, and tenant boundary",
    ),
    ActivationPhase(
        id="auth-scope-review",
        title="Review least-privilege OAuth or API scopes",
        required_output="approved scope set and credential storage plan",
    ),
    ActivationPhase(
        id="sandbox-dry-run",
        title="Run deterministic sandbox workflow",
        required_output="captured ingress, tool-call, and idempotency evidence",
    ),
    ActivationPhase(
        id="pilot-with-approvals",
        title="Pilot with human approvals on mutating actions",
        required_output="approved pilot cohort, rollback owner, and audit trail",
    ),
    ActivationPhase(
        id="production-enable",
        title="Enable production routing with monitoring",
        required_output="operator dashboard, SLO threshold, and disable path",
    ),
)

_BUNDLES: tuple[IntegrationCapabilityBundle, ...] = (
    IntegrationCapabilityBundle(
        slug="engineering-autonomy-stack",
        name="Engineering autonomy stack",
        target_agent="Autonomous engineering copilot",
        capability_slugs=(
            "github-engineering-copilot",
            "linear-jira-work-intake",
            "slack-command-center",
        ),
        implementation_description=(
            "Package GitHub, work-tracker, and Slack capabilities into one agent "
            "offer that can ingest issues, repair CI, post updates, and keep "
            "human engineers in the approval loop."
        ),
        business_case=(
            "Engineering teams are the fastest path to paid autonomous-agent "
            "adoption because repositories, tickets, and chat produce measurable "
            "cycle-time and incident-resolution outcomes."
        ),
        future_unlocks=(
            "Managed PR repair subscriptions.",
            "Release captain agents for SMB engineering teams.",
            "Codeowner-aware specialist routing.",
        ),
        priority_score=99,
    ),
    IntegrationCapabilityBundle(
        slug="customer-success-command-center",
        name="Customer success command center",
        target_agent="Support and customer-success coordinator",
        capability_slugs=(
            "zendesk-intercom-support-desk",
            "hubspot-salesforce-crm-agent",
            "notion-knowledge-operator",
        ),
        implementation_description=(
            "Combine support tickets, CRM context, and knowledge-base updates so "
            "agents can triage customer issues, draft replies, and preserve new "
            "answers as reusable operational memory."
        ),
        business_case=(
            "Support-heavy customers can justify Omnigent through lower response "
            "latency, better handoffs, and compounding knowledge capture."
        ),
        future_unlocks=(
            "Customer health agents.",
            "Support-to-sales escalation workflows.",
            "Automatic knowledge-gap remediation."
        ),
        priority_score=94,
    ),
    IntegrationCapabilityBundle(
        slug="revenue-ops-agent-pack",
        name="Revenue ops agent pack",
        target_agent="Revenue operations analyst",
        capability_slugs=(
            "stripe-shopify-revenue-ops",
            "hubspot-salesforce-crm-agent",
            "google-workspace-operator",
        ),
        implementation_description=(
            "Package commerce, CRM, and workspace capabilities for agents that "
            "monitor renewals, payment failures, high-value orders, and customer "
            "follow-up workflows without direct financial mutation by default."
        ),
        business_case=(
            "Connects autonomous work to revenue protection, churn prevention, and "
            "finance/customer-success collaboration."
        ),
        future_unlocks=(
            "Churn risk response agents.",
            "Finance exception triage.",
            "Automated renewal workspace generation.",
        ),
        priority_score=90,
    ),
)


def list_integration_capability_bundles() -> list[IntegrationCapabilityBundle]:
    """Return agent-offer bundles ordered by product priority."""

    return sorted(_BUNDLES, key=lambda bundle: bundle.priority_score, reverse=True)


def compile_integration_capability_bundle(
    slug: str,
) -> CompiledIntegrationCapabilityBundle | None:
    """Resolve one integration bundle into catalog entries and activation phases."""

    for bundle in _BUNDLES:
        if bundle.slug == slug:
            return bundle.compile()
    return None
