"""Tests for deterministic integration workflow harness compiler (iteration 22)."""

from __future__ import annotations

from bytedesk_omnigent.integration_harness import compile_integration_harness


def test_compile_integration_harness_builds_stable_archon_style_phases() -> None:
    plan = compile_integration_harness(
        provider="Slack",
        objective="triage support escalations",
        agent_id="support-orchestrator",
        external_object="channel:#vip-support",
    )

    assert plan.provider == "slack"
    assert plan.objective == "triage support escalations"
    assert plan.agent_id == "support-orchestrator"
    assert [phase.id for phase in plan.phases] == [
        "intake",
        "auth_readiness",
        "plan",
        "dry_run",
        "execute",
        "verify",
        "handoff",
    ]
    assert plan.phases[0].required_evidence == ("external_object", "objective")
    assert plan.phases[1].required_evidence == ("oauth_scopes", "secret_refs")
    assert "channels:history" in plan.oauth_scopes
    assert plan.webhook_events == ("message.channels", "app_mention")
    assert plan.idempotency_key == (
        "integration-harness:slack:support-orchestrator:channel-vip-support:"
        "triage-support-escalations"
    )


def test_compile_integration_harness_supports_jira_service_defaults() -> None:
    plan = compile_integration_harness(
        provider="Jira",
        objective="create incident follow-up tasks",
        agent_id="incident-manager",
        external_object="project:OPS",
    )

    assert plan.oauth_scopes == ("read:jira-work", "write:jira-work")
    assert plan.webhook_events == ("issue_created", "issue_updated")
    assert plan.phases[4].retry_policy == "retry_transient_3x_then_dead_letter"
    assert plan.to_dict()["phases"][5]["required_evidence"] == [
        "external_receipt",
        "state_snapshot",
    ]


def test_compile_integration_harness_normalizes_unknown_provider_safely() -> None:
    plan = compile_integration_harness(
        provider="  Custom CRM  ",
        objective="sync renewal risk",
        agent_id="revops-agent",
        external_object="account:Acme Inc.",
    )

    assert plan.provider == "custom-crm"
    assert plan.oauth_scopes == ()
    assert plan.webhook_events == ()
    assert plan.idempotency_key.endswith("account-acme-inc:sync-renewal-risk")
