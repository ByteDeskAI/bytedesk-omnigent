"""Tests for deterministic connected-app OAuth scope review (iteration 51).

The review is intentionally pure: it lets ByteDesk Platform and autonomous
integration agents vet requested third-party scopes before an OAuth install is
created, without reading secrets or calling provider APIs.
"""

from __future__ import annotations

from bytedesk_omnigent.integration_scope_review import (
    IntegrationScopeRisk,
    review_integration_scopes,
)


def test_low_risk_slack_read_scopes_are_approved_without_human_gate() -> None:
    review = review_integration_scopes(
        service="slack",
        requested_scopes=["channels:history", "chat:write", "channels:history"],
    )

    assert review.service == "slack"
    assert review.risk is IntegrationScopeRisk.LOW
    assert review.requires_human_approval is False
    assert review.approved_scopes == ("channels:history", "chat:write")
    assert review.high_risk_scopes == ()
    assert review.unknown_scopes == ()


def test_high_risk_google_scopes_require_two_key_and_dry_run_policy() -> None:
    review = review_integration_scopes(
        service="google-workspace",
        requested_scopes=[
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/admin.directory.user",
        ],
    )

    assert review.risk is IntegrationScopeRisk.HIGH
    assert review.requires_human_approval is True
    assert review.high_risk_scopes == (
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/admin.directory.user",
    )
    assert review.policy_recommendations[0]["policy"] == "two_key_approval"
    assert review.policy_recommendations[1]["policy"] == "dry_run_write_actions"


def test_unknown_service_and_scope_fail_closed_with_recommendation() -> None:
    review = review_integration_scopes(
        service="custom-crm",
        requested_scopes=["contacts.read", "contacts.delete"],
    )

    assert review.risk is IntegrationScopeRisk.HIGH
    assert review.requires_human_approval is True
    assert review.approved_scopes == ()
    assert review.unknown_scopes == ("contacts.read", "contacts.delete")
    assert any("not in the built-in catalog" in item for item in review.recommendations)
