# ruff: noqa: E501
"""Integration capability catalog for autonomous agent expansion.

Iteration 1 of the Omnigent autonomous-feature loop adds a deterministic,
product-facing catalog of high-value third-party integration blueprints. The
catalog is intentionally static for now: it gives the platform, Office UI, and
future planning agents one canonical surface for deciding which OAuth/MCP
connectors unlock the most agent value next, without requiring live credentials
or network calls.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

CapabilityCategory = Literal[
    "communication",
    "project_management",
    "knowledge",
    "developer",
    "crm_support",
    "commerce_billing",
    "workflow_harness",
]

IntegrationStatus = Literal["candidate", "prototype", "planned"]


@dataclass(frozen=True)
class IntegrationCapability:
    """A first-party blueprint for one agent integration capability."""

    slug: str
    name: str
    category: CapabilityCategory
    status: IntegrationStatus
    auth_model: str
    agent_value: tuple[str, ...]
    required_scopes: tuple[str, ...]
    implementation_description: str
    future_unlocks: tuple[str, ...]
    business_case: str
    priority_score: int
    references: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        data = asdict(self)
        # JSON responses should use arrays, not tuples, for stable OpenAPI/client
        # expectations.
        for key in ("agent_value", "required_scopes", "future_unlocks", "references"):
            data[key] = list(data[key])
        return data


_CAPABILITIES: tuple[IntegrationCapability, ...] = (
    IntegrationCapability(
        slug="slack-command-center",
        name="Slack command center",
        category="communication",
        status="candidate",
        auth_model="OAuth 2.0 bot + user tokens",
        agent_value=(
            "Let agents observe channel context, ask clarifying questions, and post status updates where teams already work.",
            "Route approvals, escalations, and handoffs through Slack threads instead of a separate dashboard.",
        ),
        required_scopes=("channels:history", "chat:write", "commands", "users:read"),
        implementation_description=(
            "Add a Slack OAuth connector, event adapter, and MCP/tool facade that maps channel messages, slash commands, "
            "thread replies, and approval buttons into Omnigent signals and Tasks. Start read-only + post-message, then "
            "gate destructive actions behind the existing policy and two-key approval surfaces."
        ),
        future_unlocks=(
            "Team-agent triage inside customer Slack Connect channels.",
            "Human-in-the-loop approval buttons for autonomous workflows.",
            "Cross-agent incident rooms with summarized state handoff.",
        ),
        business_case=(
            "Reduces time-to-value for business users because agents can collaborate in the same workspace where work is assigned, "
            "approved, and discussed."
        ),
        priority_score=98,
        references=("https://api.slack.com/authentication/oauth-v2",),
    ),
    IntegrationCapability(
        slug="notion-knowledge-operator",
        name="Notion knowledge operator",
        category="knowledge",
        status="candidate",
        auth_model="OAuth 2.0 internal or public integration",
        agent_value=(
            "Give agents a durable knowledge-base read/write surface for plans, SOPs, meeting notes, and execution logs.",
            "Let agents update docs as part of task completion instead of leaving context trapped in chat transcripts.",
        ),
        required_scopes=("read_content", "update_content", "insert_content"),
        implementation_description=(
            "Create a Notion connector that indexes selected pages/databases into Omnigent memory and exposes safe page append/update "
            "tools. Keep page creation scoped to explicit workspaces and record every write in the outcome ledger."
        ),
        future_unlocks=(
            "Self-maintaining team runbooks.",
            "Agent-generated implementation plans with live status sections.",
            "Customer-specific knowledge packs for specialist agents.",
        ),
        business_case=(
            "Turns agent work into reusable organizational memory, lowering repeated-context cost and improving customer trust in autonomous execution."
        ),
        priority_score=94,
        references=("https://developers.notion.com/docs/authorization",),
    ),
    IntegrationCapability(
        slug="trello-task-bridge",
        name="Trello task bridge",
        category="project_management",
        status="candidate",
        auth_model="OAuth 1.0a / token-based Trello authorization",
        agent_value=(
            "Map cards, checklists, labels, and comments into Omnigent Tasks and signals.",
            "Let lightweight SMB teams keep Trello while using Omnigent for autonomous execution.",
        ),
        required_scopes=("read", "write"),
        implementation_description=(
            "Build a Trello board adapter that imports cards as Tasks, watches card moves as lifecycle signals, and writes back execution "
            "summaries as comments. Use idempotency keys from card IDs + action IDs."
        ),
        future_unlocks=(
            "No-migration onboarding for Trello-first customers.",
            "Card-level autonomous execution buttons.",
            "Outcome analytics by board/list/label."
        ),
        business_case="Expands Omnigent into SMB project boards with minimal onboarding friction.",
        priority_score=88,
        references=("https://developer.atlassian.com/cloud/trello/guides/rest-api/authorization/",),
    ),
    IntegrationCapability(
        slug="github-engineering-copilot",
        name="GitHub engineering copilot",
        category="developer",
        status="candidate",
        auth_model="GitHub App installation + OAuth web flow",
        agent_value=(
            "Let coding agents reason over issues, PRs, reviews, checks, and repository events with least-privilege installation tokens.",
            "Turn failed CI, review comments, and issue assignments into autonomous Omnigent Tasks."
        ),
        required_scopes=("contents:read", "issues:write", "pull_requests:write", "checks:read"),
        implementation_description=(
            "Prefer a GitHub App over user PATs. Convert webhook events into signed ingress signals, expose repository tools through MCP, "
            "and bind PR/CI state to Task lifecycle transitions."
        ),
        future_unlocks=(
            "Autonomous PR repair loops.",
            "Reviewer-specialist routing by code ownership/outcomes.",
            "Release-note and changelog automation."
        ),
        business_case="Directly supports engineering teams using Omnigent as a managed agent workforce.",
        priority_score=96,
        references=("https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps",),
    ),
    IntegrationCapability(
        slug="linear-jira-work-intake",
        name="Linear/Jira work intake",
        category="project_management",
        status="candidate",
        auth_model="OAuth 2.0 / Atlassian 3LO",
        agent_value=(
            "Synchronize issues, priorities, comments, and status transitions into Omnigent Tasks.",
            "Keep human project-management tools as source-of-truth while agents execute in Omnigent."
        ),
        required_scopes=("read", "write", "offline_access"),
        implementation_description=(
            "Define a common WorkItemAdapter Protocol and ship Linear + Jira implementations. Normalize issue events into TaskCreated, "
            "TaskUpdated, and TaskBlocked signals while preserving external IDs for idempotency."
        ),
        future_unlocks=(
            "Customer-managed autonomous backlogs.",
            "SLA-aware routing and escalation.",
            "Portfolio dashboards across multiple work trackers."
        ),
        business_case="Lets customers adopt Omnigent without abandoning existing work-management investments.",
        priority_score=97,
        references=(
            "https://developers.linear.app/docs/oauth/authentication",
            "https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/",
        ),
    ),
    IntegrationCapability(
        slug="google-workspace-operator",
        name="Google Workspace operator",
        category="knowledge",
        status="candidate",
        auth_model="OAuth 2.0 with domain-wide delegation option",
        agent_value=(
            "Let agents read/write Docs, Sheets, Drive files, Gmail drafts, and Calendar events under explicit scopes.",
            "Convert meetings, emails, and spreadsheets into actionable agent tasks and updates."
        ),
        required_scopes=(
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/documents",
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/calendar.events",
        ),
        implementation_description=(
            "Create separate least-privilege tools for Drive discovery, Docs append/update, Sheets row operations, and Calendar event creation. "
            "Require approval policies for outbound email send and broad Drive reads."
        ),
        future_unlocks=(
            "Meeting-to-task automation.",
            "Spreadsheet-backed operations agents.",
            "Customer-facing document generation and delivery."
        ),
        business_case="Targets the common SMB operating system: email, calendars, docs, and spreadsheets.",
        priority_score=95,
        references=("https://developers.google.com/identity/protocols/oauth2",),
    ),
    IntegrationCapability(
        slug="hubspot-salesforce-crm-agent",
        name="HubSpot/Salesforce CRM agent",
        category="crm_support",
        status="candidate",
        auth_model="OAuth 2.0 connected app / private app token fallback",
        agent_value=(
            "Give sales/support agents controlled access to contacts, companies, deals, tickets, and activity timelines.",
            "Let agents draft follow-ups and update CRM records after verified customer interactions."
        ),
        required_scopes=("crm.objects.contacts.read", "crm.objects.deals.write", "refresh_token"),
        implementation_description=(
            "Normalize CRM entities behind a CustomerRecordAdapter Protocol. Start with read + note append, then graduate to deal/ticket updates "
            "behind approval gates and audit logging."
        ),
        future_unlocks=(
            "Revenue-assistant agents.",
            "Customer-success health scoring.",
            "Automated support-to-sales handoffs."
        ),
        business_case="Connects autonomous work to revenue workflows and customer lifecycle management.",
        priority_score=90,
        references=(
            "https://developers.hubspot.com/docs/api/oauth-quickstart-guide",
            "https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_web_server_flow.htm",
        ),
    ),
    IntegrationCapability(
        slug="zendesk-intercom-support-desk",
        name="Zendesk/Intercom support desk",
        category="crm_support",
        status="candidate",
        auth_model="OAuth 2.0 / app marketplace authorization",
        agent_value=(
            "Turn support tickets and conversations into autonomous triage, draft response, and escalation workflows.",
            "Let specialist agents collaborate on customer problems with a full audit trail."
        ),
        required_scopes=("tickets:read", "tickets:write", "users:read"),
        implementation_description=(
            "Implement a SupportTicketAdapter with ticket import, comment append, assignment, tag updates, and webhook ingress. Require human approval "
            "for public customer replies until quality gates are proven."
        ),
        future_unlocks=(
            "24/7 support triage agents.",
            "Knowledge-gap detection feeding Notion/Docs updates.",
            "Escalation routing to engineering Tasks."
        ),
        business_case="Makes Omnigent valuable to support-heavy SMBs by reducing response latency while preserving human oversight.",
        priority_score=91,
        references=(
            "https://developer.zendesk.com/documentation/ticketing/working-with-oauth/",
            "https://developers.intercom.com/docs/references/oauth/",
        ),
    ),
    IntegrationCapability(
        slug="stripe-shopify-revenue-ops",
        name="Stripe/Shopify revenue ops",
        category="commerce_billing",
        status="candidate",
        auth_model="OAuth 2.0 / restricted API keys",
        agent_value=(
            "Let agents inspect subscriptions, invoices, orders, refunds, and customer commerce context.",
            "Trigger workflows on payment failures, high-value orders, refunds, and churn risks."
        ),
        required_scopes=("read_only", "orders:read", "customers:read"),
        implementation_description=(
            "Start with read-only commerce adapters and webhook ingress. Treat refunds, cancellations, and billing mutations as risk-tiered actions "
            "requiring explicit confirmation."
        ),
        future_unlocks=(
            "Churn-prevention agents.",
            "Order exception handling.",
            "Revenue anomaly detection and finance handoffs."
        ),
        business_case="Links autonomous agents to revenue protection and customer retention workflows.",
        priority_score=84,
        references=(
            "https://docs.stripe.com/connect/oauth-reference",
            "https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/authorization-code-grant",
        ),
    ),
    IntegrationCapability(
        slug="archon-style-workflow-blueprints",
        name="Archon-style deterministic workflow blueprints",
        category="workflow_harness",
        status="candidate",
        auth_model="Internal YAML/workflow schema",
        agent_value=(
            "Let teams define repeatable, deterministic multi-agent workflows with explicit phases, typed inputs, and verification gates.",
            "Bridge Omnigent's YAML agent specs with Archon-inspired harness blueprints for repeatable AI coding and operations workflows."
        ),
        required_scopes=(),
        implementation_description=(
            "Add a workflow-blueprint layer that compiles YAML phases into Omnigent Tasks, tool steps, policies, and verification gates. Model each "
            "phase as an idempotent node with declared inputs/outputs, assigned agent role, retry policy, and completion evidence."
        ),
        future_unlocks=(
            "Marketplace-ready workflow templates.",
            "Deterministic agent QA runs.",
            "Customer-configurable autonomous feature factories.",
        ),
        business_case=(
            "Moves Omnigent from individual agent management into repeatable agent-workforce operations, which is the core middleware value proposition."
        ),
        priority_score=99,
        references=("https://github.com/coleam00/Archon",),
    ),
)


def list_integration_capabilities(
    *, category: CapabilityCategory | None = None, limit: int | None = None
) -> list[IntegrationCapability]:
    """Return catalog entries ordered by descending product priority."""

    entries = sorted(_CAPABILITIES, key=lambda item: item.priority_score, reverse=True)
    if category is not None:
        entries = [entry for entry in entries if entry.category == category]
    if limit is not None:
        entries = entries[:limit]
    return entries


def get_integration_capability(slug: str) -> IntegrationCapability | None:
    """Look up one catalog entry by slug."""

    for entry in _CAPABILITIES:
        if entry.slug == slug:
            return entry
    return None


def integration_capability_categories() -> list[str]:
    """Return the stable category names currently represented in the catalog."""

    return sorted({entry.category for entry in _CAPABILITIES})
