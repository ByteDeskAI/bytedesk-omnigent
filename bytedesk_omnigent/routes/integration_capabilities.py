"""Read API for high-value third-party integration capability blueprints."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from bytedesk_omnigent.integration_capabilities import (
    CapabilityCategory,
    get_integration_capability,
    integration_capability_categories,
    list_integration_capabilities,
)
from bytedesk_omnigent.integration_remediation_playbook import (
    compile_integration_remediation_playbook,
)
from bytedesk_omnigent.integration_verification_matrix import (
    compile_integration_verification_matrix,
)
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user

_FAILED_GATE_ID_QUERY = Query(default_factory=list)


def create_integration_capabilities_router(
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the integration capability catalog router.

    The catalog is read-only product metadata. It is still authenticated in
    multi-user mode because entries expose platform roadmap intent and business
    prioritization; single-user/local mode keeps it open like sibling ByteDesk
    extension read routes.
    """

    router = APIRouter()

    @router.get("/integration-capabilities")
    async def list_capabilities(
        request: Request,
        category: CapabilityCategory | None = None,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> JSONResponse:
        """List integration blueprints ordered by product priority."""

        require_user(request, auth_provider)
        entries = list_integration_capabilities(category=category, limit=limit)
        return JSONResponse(
            {
                "object": "list",
                "data": [entry.to_dict() for entry in entries],
                "categories": integration_capability_categories(),
            }
        )

    @router.get("/integration-capabilities/{slug}")
    async def get_capability(request: Request, slug: str) -> JSONResponse:
        """Read one integration blueprint by slug."""

        require_user(request, auth_provider)
        entry = get_integration_capability(slug)
        if entry is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(entry.to_dict())

    @router.get("/integration-capabilities/{slug}/verification-matrix")
    async def get_capability_verification_matrix(
        request: Request, slug: str
    ) -> JSONResponse:
        """Compile rollout verification gates for one integration blueprint."""

        require_user(request, auth_provider)
        matrix = compile_integration_verification_matrix(slug)
        if matrix is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(matrix)

    @router.get("/integration-capabilities/{slug}/remediation-playbook")
    async def get_capability_remediation_playbook(
        request: Request,
        slug: str,
        failed_gate_id: list[str] = _FAILED_GATE_ID_QUERY,
    ) -> JSONResponse:
        """Compile repair steps for failed rollout verification gates."""

        require_user(request, auth_provider)
        playbook = compile_integration_remediation_playbook(
            slug, failed_gate_ids=tuple(failed_gate_id)
        )
        if playbook is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(playbook)

    return router
