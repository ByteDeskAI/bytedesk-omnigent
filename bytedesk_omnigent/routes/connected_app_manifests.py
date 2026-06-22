"""Connected-app manifest route for third-party Omnigent integrations."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


def create_connected_app_manifests_router(
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the connected-app manifest compiler router.

    The route is open in single-user mode and requires identity in multi-user mode,
    matching the other ByteDesk extension management/read-model surfaces.
    """

    router = APIRouter()

    @router.post("/connected-app-manifests/compile")
    async def compile_manifest(request: Request) -> JSONResponse:
        """Compile OAuth/webhook/task setup for a provider/workspace pair."""

        require_user(request, auth_provider)
        from bytedesk_omnigent.connected_app_manifests import (
            compile_connected_app_manifest,
            connected_app_manifest_to_dict,
            provider_slugs,
        )

        payload = await request.json()
        try:
            manifest = compile_connected_app_manifest(
                provider=str(payload.get("provider", "")),
                workspace_id=str(payload.get("workspace_id", "")),
                public_base_url=str(payload.get("public_base_url", "")),
                desired_capabilities=payload.get("desired_capabilities"),
                tenant_id=payload.get("tenant_id"),
                writeback_enabled=bool(payload.get("writeback_enabled", False)),
            )
        except ValueError as exc:
            return JSONResponse(
                {"error": str(exc), "supported_providers": provider_slugs()},
                status_code=400,
            )
        return JSONResponse({"manifest": connected_app_manifest_to_dict(manifest)})

    return router
