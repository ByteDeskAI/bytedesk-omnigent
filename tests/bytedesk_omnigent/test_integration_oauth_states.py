"""Tests for deterministic integration OAuth state tokens."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_oauth_states import (
    issue_oauth_state,
    verify_oauth_state,
)
from bytedesk_omnigent.routes.integration_oauth_states import (
    create_integration_oauth_states_router,
)


def test_issue_oauth_state_is_stable_signed_and_does_not_leak_secret() -> None:
    issued = issue_oauth_state(
        provider="Google Workspace",
        workspace_id="ws_123",
        redirect_uri="https://platform.bytedesk.test/oauth/callback",
        scopes=["drive.file", "gmail.send", "drive.file"],
        install_id="install_abc",
        nonce="nonce_123",
        secret="state-secret",
        now=1_700_000_000,
        ttl_seconds=900,
    )

    assert issued.state.startswith("omni-oauth-v1.")
    assert "state-secret" not in issued.state
    assert issued.claims.provider == "google-workspace"
    assert issued.claims.scopes == ("drive.file", "gmail.send")
    assert issued.claims.expires_at == 1_700_000_900

    verified = verify_oauth_state(
        issued.state,
        secret="state-secret",
        expected_provider="google-workspace",
        expected_workspace_id="ws_123",
        now=1_700_000_899,
    )

    assert verified.valid is True
    assert verified.reason == "ok"
    assert verified.claims == issued.claims


@pytest.mark.parametrize(
    ("state_mutator", "secret", "now", "reason"),
    [
        (
            lambda state: state[:-1] + ("A" if state[-1] != "A" else "B"),
            "state-secret",
            1_700_000_100,
            "bad_signature",
        ),
        (lambda state: state, "wrong-secret", 1_700_000_100, "bad_signature"),
        (lambda state: state, "state-secret", 1_700_001_000, "expired"),
    ],
)
def test_verify_oauth_state_rejects_tamper_wrong_secret_and_expiry(
    state_mutator, secret: str, now: int, reason: str
) -> None:
    issued = issue_oauth_state(
        provider="notion",
        workspace_id="ws_123",
        redirect_uri="https://platform.bytedesk.test/oauth/callback",
        scopes=["pages:read"],
        nonce="nonce_456",
        secret="state-secret",
        now=1_700_000_000,
        ttl_seconds=300,
    )

    verified = verify_oauth_state(state_mutator(issued.state), secret=secret, now=now)

    assert verified.valid is False
    assert verified.reason == reason
    assert verified.claims is None


def test_oauth_state_router_issues_and_verifies_with_env_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_OAUTH_STATE_SECRET", "route-secret")
    app = FastAPI()
    app.include_router(create_integration_oauth_states_router(), prefix="/v1")
    client = TestClient(app)

    issue_response = client.post(
        "/v1/integration-oauth-states/issue",
        json={
            "provider": "HubSpot",
            "workspace_id": "ws_route",
            "redirect_uri": "https://platform.bytedesk.test/oauth/callback",
            "scopes": ["crm.objects.contacts.read"],
            "install_id": "install_route",
            "nonce": "nonce_route",
            "now": 1_700_000_000,
        },
    )

    assert issue_response.status_code == 200
    state = issue_response.json()["state"]

    verify_response = client.post(
        "/v1/integration-oauth-states/verify",
        json={
            "state": state,
            "expected_provider": "hubspot",
            "expected_workspace_id": "ws_route",
            "now": 1_700_000_001,
        },
    )

    assert verify_response.status_code == 200
    assert verify_response.json()["valid"] is True
    assert verify_response.json()["claims"]["provider"] == "hubspot"
