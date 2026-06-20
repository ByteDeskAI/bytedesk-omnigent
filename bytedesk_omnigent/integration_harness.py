"""Deterministic third-party integration workflow harness compiler.

The compiler turns a requested service integration (Slack/Jira/etc.) into a
stable Archon-style phase contract: every phase names its gate, evidence, retry
posture, and audit event before an agent touches the external system. It is pure
and dependency-free so routes, tools, and platform adapters can reuse the same
contract.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class IntegrationHarnessPhase:
    """One deterministic phase in an external integration workflow."""

    id: str
    title: str
    gate: str
    required_evidence: tuple[str, ...]
    retry_policy: str
    audit_event: str


@dataclass(frozen=True)
class IntegrationHarnessPlan:
    """Compiled workflow harness for one provider/object/objective tuple."""

    provider: str
    objective: str
    agent_id: str
    external_object: str
    oauth_scopes: tuple[str, ...]
    webhook_events: tuple[str, ...]
    idempotency_key: str
    phases: tuple[IntegrationHarnessPhase, ...]

    def to_dict(self) -> dict:
        """Return a JSON-serializable representation for routes/PR bodies."""
        data = asdict(self)
        data["oauth_scopes"] = list(self.oauth_scopes)
        data["webhook_events"] = list(self.webhook_events)
        data["phases"] = [
            {
                **asdict(phase),
                "required_evidence": list(phase.required_evidence),
            }
            for phase in self.phases
        ]
        return data


_PROVIDER_DEFAULTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "slack": (
        ("channels:history", "chat:write", "app_mentions:read"),
        ("message.channels", "app_mention"),
    ),
    "notion": (("read", "insert", "update"), ("page.created", "page.updated")),
    "trello": (("read", "write"), ("card.created", "card.updated")),
    "github": (("repo", "read:org"), ("issues", "pull_request")),
    "linear": (("read", "write"), ("Issue", "Comment")),
    "jira": (("read:jira-work", "write:jira-work"), ("issue_created", "issue_updated")),
    "google-workspace": (
        ("https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/calendar.events"),
        ("drive.file.changed", "calendar.event.changed"),
    ),
    "hubspot": (
        ("crm.objects.contacts.read", "crm.objects.contacts.write"),
        ("contact.creation", "contact.propertyChange"),
    ),
    "salesforce": (("api", "refresh_token"), ("AccountChangeEvent", "OpportunityChangeEvent")),
    "zendesk": (("read", "write"), ("ticket.created", "ticket.updated")),
    "intercom": (
        ("read_conversations", "write_conversations"),
        ("conversation.user.created", "conversation.admin.replied"),
    ),
    "stripe": (("read_write",), ("checkout.session.completed", "invoice.payment_failed")),
    "shopify": (("read_orders", "write_orders"), ("orders/create", "orders/updated")),
    "microsoft-teams": (
        ("ChannelMessage.Read.All", "ChannelMessage.Send"),
        ("channelMessage.created",),
    ),
    "discord": (("bot", "applications.commands"), ("MESSAGE_CREATE", "INTERACTION_CREATE")),
    "asana": (("default",), ("task.added", "task.changed")),
    "monday": (("boards:read", "boards:write"), ("create_pulse", "change_column_value")),
    "airtable": (
        ("data.records:read", "data.records:write"),
        ("record.created", "record.updated"),
    ),
}


def compile_integration_harness(
    *,
    provider: str,
    objective: str,
    agent_id: str,
    external_object: str,
) -> IntegrationHarnessPlan:
    """Compile a deterministic workflow harness for a third-party integration.

    The returned contract is intentionally conservative: no execution phase can
    run before auth readiness, planning, and dry-run evidence exists; post-write
    verification and human/platform handoff are explicit phases rather than
    implicit agent narration.
    """

    normalized_provider = _slug(provider)
    scopes, events = _PROVIDER_DEFAULTS.get(normalized_provider, ((), ()))
    idempotency_key = ":".join(
        (
            "integration-harness",
            normalized_provider,
            _slug(agent_id),
            _slug(external_object),
            _slug(objective),
        )
    )
    phases = (
        IntegrationHarnessPhase(
            id="intake",
            title="Capture external target and success criteria",
            gate="inputs_present",
            required_evidence=("external_object", "objective"),
            retry_policy="no_retry_fix_inputs",
            audit_event="integration.intake.captured",
        ),
        IntegrationHarnessPhase(
            id="auth_readiness",
            title="Confirm OAuth scopes and secret references",
            gate="credentials_ready",
            required_evidence=("oauth_scopes", "secret_refs"),
            retry_policy="no_retry_missing_secret",
            audit_event="integration.auth.ready",
        ),
        IntegrationHarnessPhase(
            id="plan",
            title="Build deterministic work plan",
            gate="plan_reviewed",
            required_evidence=("intended_mutations", "rollback_plan"),
            retry_policy="no_retry_fix_plan",
            audit_event="integration.plan.compiled",
        ),
        IntegrationHarnessPhase(
            id="dry_run",
            title="Validate reads and proposed writes without mutation",
            gate="dry_run_passed",
            required_evidence=("read_probe", "write_preview"),
            retry_policy="retry_transient_2x_then_block",
            audit_event="integration.dry_run.passed",
        ),
        IntegrationHarnessPhase(
            id="execute",
            title="Apply approved external-system mutation",
            gate="approved_execution",
            required_evidence=("approval", "idempotency_key"),
            retry_policy="retry_transient_3x_then_dead_letter",
            audit_event="integration.execute.applied",
        ),
        IntegrationHarnessPhase(
            id="verify",
            title="Verify external receipt and durable state snapshot",
            gate="verification_passed",
            required_evidence=("external_receipt", "state_snapshot"),
            retry_policy="retry_transient_3x_then_escalate",
            audit_event="integration.verify.passed",
        ),
        IntegrationHarnessPhase(
            id="handoff",
            title="Publish platform handoff for humans and downstream agents",
            gate="handoff_recorded",
            required_evidence=("summary", "next_actions"),
            retry_policy="retry_transient_2x_then_escalate",
            audit_event="integration.handoff.recorded",
        ),
    )
    return IntegrationHarnessPlan(
        provider=normalized_provider,
        objective=objective.strip(),
        agent_id=agent_id.strip(),
        external_object=external_object.strip(),
        oauth_scopes=scopes,
        webhook_events=events,
        idempotency_key=idempotency_key,
        phases=phases,
    )


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "unknown"
