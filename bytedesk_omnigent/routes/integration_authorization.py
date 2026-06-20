"""OAuth authorization URL API for third-party integration installs."""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


def create_integration_authorization_router(
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the integration authorization control-plane router."""
    router = APIRouter()

    @router.post("/integration-authorizations/authorize-url")
    async def authorize_url(request: Request) -> JSONResponse:
        """Compile a deterministic third-party OAuth authorization URL."""
        require_user(request, auth_provider)
        from bytedesk_omnigent.integration_authorization import (
            InvalidOAuthAuthorizationRequest,
            UnknownOAuthProviderError,
            compile_oauth_authorization_url,
        )

        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse(
                {"status": "invalid_request", "detail": "JSON object body required"},
                status_code=400,
            )
        try:
            result = compile_oauth_authorization_url(
                provider=str(body.get("provider", "")),
                client_id=str(body.get("client_id", "")),
                redirect_uri=str(body.get("redirect_uri", "")),
                state=str(body.get("state", "")),
                scopes=_string_list(body.get("scopes")),
                extra_params=_string_mapping(body.get("extra_params")),
            )
        except (InvalidOAuthAuthorizationRequest, UnknownOAuthProviderError) as exc:
            return JSONResponse(
                {"status": "invalid_request", "detail": str(exc)},
                status_code=400,
            )
        return JSONResponse(
            {"provider": result.provider, "url": result.url, "scopes": list(result.scopes)}
        )

    return router


def _string_list(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _string_mapping(value: object) -> Mapping[str, str] | None:
    if value is None or not isinstance(value, dict):
        return None
    return {str(key): str(item) for key, item in value.items()}
