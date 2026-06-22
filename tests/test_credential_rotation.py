"""Tests for deterministic third-party credential rotation plans."""

from __future__ import annotations

from bytedesk_omnigent.credential_rotation import (
    CredentialRotationTarget,
    compile_credential_rotation_plan,
)


def test_compile_credential_rotation_plan_is_deterministic_and_secret_free() -> None:
    """Rotation plans must be stable runbooks that never carry secret material."""
    target = CredentialRotationTarget(
        service="Slack Enterprise",
        app_id="app_123",
        credential_kind="webhook_secret",
        environment="production",
        current_version="v1",
        next_version="v2",
        vault_ref="infisical://bytedesk/prod/slack/webhook",
        owner_team="platform-integrations",
    )

    first = compile_credential_rotation_plan(target)
    second = compile_credential_rotation_plan(target)

    assert first == second
    assert first["service"] == "slack-enterprise"
    assert first["credential_kind"] == "webhook_secret"
    assert first["idempotency_key"] == (
        "credential-rotation:slack-enterprise:app_123:webhook_secret:production:v1->v2"
    )
    assert first["vault_ref"] == "infisical://bytedesk/prod/slack/webhook"
    assert first["requires_human_approval"] is True
    assert first["secret_material_included"] is False
    assert [step["id"] for step in first["steps"]] == [
        "prepare",
        "install_next_credential",
        "verify_shadow_auth",
        "cutover",
        "revoke_previous_credential",
        "audit",
    ]
    serialized = repr(first).lower()
    assert "v2-secret-value" not in serialized
    assert "client_secret" not in serialized


def test_compile_credential_rotation_plan_bounds_retry_and_rolls_back() -> None:
    """Agents need deterministic retry/rollback instructions before cutover."""
    plan = compile_credential_rotation_plan(
        CredentialRotationTarget(
            service="Google Workspace",
            app_id="workspace-sync",
            credential_kind="oauth_client_secret",
            environment="staging",
            current_version="2026-06-a",
            next_version="2026-06-b",
            vault_ref="vault://google/workspace/oauth-client-secret",
        ),
        max_attempts=12,
    )

    assert plan["service"] == "google-workspace"
    assert plan["max_attempts"] == 8
    assert plan["requires_human_approval"] is True
    assert plan["approval_reasons"] == ["oauth_client_secret_rotation"]
    assert plan["rollback"]["safe_until_step"] == "revoke_previous_credential"
    assert plan["rollback"]["actions"] == [
        "restore_provider_reference_to_current_version",
        "restore_omnigent_binding_to_current_version",
        "re-run_shadow_auth_probe",
        "record_rotation_aborted",
    ]
