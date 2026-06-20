"""Tests for deterministic OAuth refresh plan compilation.

The compiler turns a connected-app record into a safe, replayable refresh
workflow so Omnigent agents can keep third-party integrations online without
hardcoding provider-specific secrets or mutating state during planning.
"""
from __future__ import annotations

from bytedesk_omnigent.oauth_refresh import compile_oauth_refresh_plan


def test_compile_oauth_refresh_plan_for_connected_app() -> None:
    plan = compile_oauth_refresh_plan(
        provider="slack",
        connected_app_id="app_slack_123",
        token_url="https://slack.com/api/oauth.v2.access",
        refresh_token_secret_ref="infisical://omnigent/slack/app_slack_123/refresh_token",
        scopes=("channels:read", "chat:write"),
    )

    assert plan["provider"] == "slack"
    assert plan["connected_app_id"] == "app_slack_123"
    assert plan["token_url"] == "https://slack.com/api/oauth.v2.access"
    assert plan["secret_refs"] == {
        "refresh_token": "infisical://omnigent/slack/app_slack_123/refresh_token"
    }
    assert plan["lock_key"] == "oauth-refresh:slack:app_slack_123"
    assert plan["idempotency_scope"] == "oauth-refresh:slack"
    assert plan["steps"] == [
        "claim_refresh_lock",
        "resolve_refresh_token_secret",
        "request_token_refresh",
        "persist_rotated_token_material",
        "run_provider_health_probe",
        "release_refresh_lock",
    ]
    assert plan["required_scopes"] == ["channels:read", "chat:write"]
    assert plan["rollback"] == {
        "on_refresh_failure": "keep_existing_access_token_until_expiry",
        "on_persist_failure": "dead_letter_refresh_attempt_for_operator_review",
    }


def test_compile_oauth_refresh_plan_normalizes_and_deduplicates_scopes() -> None:
    plan = compile_oauth_refresh_plan(
        provider=" Google Workspace ",
        connected_app_id="workspace-prod",
        token_url="https://oauth2.googleapis.com/token",
        refresh_token_secret_ref="infisical://omnigent/google/refresh",
        scopes=("gmail.readonly", "", "calendar.events", "gmail.readonly"),
    )

    assert plan["provider"] == "google-workspace"
    assert plan["required_scopes"] == ["gmail.readonly", "calendar.events"]
    assert plan["idempotency_scope"] == "oauth-refresh:google-workspace"
    assert plan["lock_key"] == "oauth-refresh:google-workspace:workspace-prod"


def test_compile_oauth_refresh_plan_rejects_missing_required_fields() -> None:
    try:
        compile_oauth_refresh_plan(
            provider="",
            connected_app_id="app_123",
            token_url="https://example.test/token",
            refresh_token_secret_ref="secret://refresh",
        )
    except ValueError as exc:
        assert "provider" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected provider validation failure")
