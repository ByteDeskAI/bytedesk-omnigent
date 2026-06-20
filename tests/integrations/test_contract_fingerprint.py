"""Tests for deterministic third-party integration contract fingerprints."""

from __future__ import annotations

from bytedesk_omnigent.integration_contracts import (
    IntegrationContract,
    compile_integration_contract_fingerprint,
)


def test_contract_fingerprint_is_stable_across_input_order_and_case() -> None:
    """Equivalent GitHub app contracts get the same reviewable fingerprint."""
    left = IntegrationContract(
        source="GitHub",
        auth="OAuth2",
        events=["issues.opened", "pull_request.closed"],
        scopes=["repo:status", "read:org"],
        webhook_headers={
            "X-Hub-Signature-256": "required",
            "X-GitHub-Event": "required",
        },
        actions=["create_task", "wake_agent"],
    )
    right = IntegrationContract(
        source=" github ",
        auth=" oauth2 ",
        events=["pull_request.closed", "issues.opened", "issues.opened"],
        scopes=["read:org", "repo:status"],
        webhook_headers={
            "x-github-event": "required",
            "x-hub-signature-256": "required",
        },
        actions=["wake_agent", "create_task"],
    )

    left_summary = compile_integration_contract_fingerprint(left)
    right_summary = compile_integration_contract_fingerprint(right)

    assert left_summary.fingerprint == right_summary.fingerprint
    assert left_summary.canonical == right_summary.canonical
    assert left_summary.canonical == {
        "actions": ["create_task", "wake_agent"],
        "auth": "oauth2",
        "events": ["issues.opened", "pull_request.closed"],
        "scopes": ["read:org", "repo:status"],
        "source": "github",
        "webhook_headers": {
            "x-github-event": "required",
            "x-hub-signature-256": "required",
        },
    }
    assert left_summary.review_tags == [
        "source:github",
        "auth:oauth2",
        "events:2",
        "scopes:2",
        "actions:2",
    ]


def test_contract_fingerprint_changes_when_permissions_expand() -> None:
    """Scope expansion must be visible before an agent integration is activated."""
    base = IntegrationContract(
        source="notion",
        auth="oauth2",
        events=["page.updated"],
        scopes=["read_content"],
    )
    expanded = IntegrationContract(
        source="notion",
        auth="oauth2",
        events=["page.updated"],
        scopes=["read_content", "insert_content"],
    )

    assert (
        compile_integration_contract_fingerprint(base).fingerprint
        != compile_integration_contract_fingerprint(expanded).fingerprint
    )
