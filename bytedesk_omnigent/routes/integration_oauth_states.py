"""Connected-app OAuth state token routes.

``POST /v1/integration-oauth-states/issue`` creates a short-lived, HMAC-bound
state token for a provider install. ``POST /v1/integration-oauth-states/verify``
validates the callback state before Platform exchanges an OAuth code.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


def create_integration_oauth_states_router(
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the integration OAuth state router.

    :param auth_provider: when set (multi-user) the handler requires a valid
        identity; ``None`` (single-user) leaves it open.
    """
    router = APIRouter()

    @router.post("/integration-oauth-states/issue")
    async def issue(request: Request) -> JSONResponse:
        """Issue a signed OAuth state token for a connected-app install."""
        require_user(request, auth_provider)
        from bytedesk_omnigent.integration_oauth_states import issue_oauth_state

        secret = _state_secret()
        if secret is None:
            return JSONResponse(
                {
                    "status": "missing_state_secret",
                    "detail": "set OMNIGENT_OAUTH_STATE_SECRET to issue OAuth states",
                },
                status_code=503,
            )
        body = await request.json()
        try:
            issued = issue_oauth_state(
                provider=str(body.get("provider", "")),
                workspace_id=str(body.get("workspace_id", "")),
                redirect_uri=str(body.get("redirect_uri", "")),
                scopes=body.get("scopes") or (),
                install_id=body.get("install_id"),
                nonce=body.get("nonce"),
                secret=secret,
                now=body.get("now"),
                ttl_seconds=int(body.get("ttl_seconds", 600)),
            )
        except (TypeError, ValueError) as exc:
            return JSONResponse({"status": "invalid_request", "detail": str(exc)}, 400)
        return JSONResponse(issued.to_dict())

    @router.post("/integration-oauth-states/verify")
    async def verify(request: Request) -> JSONResponse:
        """Verify a signed OAuth state token from a provider callback."""
        require_user(request, auth_provider)
        from bytedesk_omnigent.integration_oauth_states import verify_oauth_state

        secret = _state_secret()
        if secret is None:
            return JSONResponse(
                {
                    "valid": False,
                    "reason": "missing_state_secret",
                    "claims": None,
                },
                status_code=503,
            )
        body = await request.json()
        try:
            result = verify_oauth_state(
                str(body.get("state", "")),
                secret=secret,
                expected_provider=body.get("expected_provider"),
                expected_workspace_id=body.get("expected_workspace_id"),
                now=body.get("now"),
            )
        except ValueError as exc:
            return JSONResponse(
                {"valid": False, "reason": str(exc), "claims": None},
                status_code=400,
            )
        return JSONResponse(result.to_dict(), status_code=200 if result.valid else 400)

    return router


def _state_secret() -> str | None:
    secret = os.environ.get("OMNIGENT_OAUTH_STATE_SECRET")
    return secret if secret and secret.strip() else None
