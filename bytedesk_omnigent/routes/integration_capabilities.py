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
from bytedesk_omnigent.integration_gap_analysis import (
    IntegrationImplementationSignal,
    analyze_integration_capability_gaps,
)
from bytedesk_omnigent.integration_verification_matrix import (
    compile_integration_verification_matrix,
)
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


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

    @router.post("/integration-capability-gaps/analyze")
    async def analyze_capability_gaps(request: Request) -> JSONResponse:
        """Compile a catalog gap report from Platform-supplied evidence."""

        require_user(request, auth_provider)
        payload = await request.json()
        implemented_slugs = set(payload.get("implemented_slugs") or [])
        open_signals = tuple(
            IntegrationImplementationSignal(
                slug=item.get("slug"),
                source=item["source"],
                title=item["title"],
                url=item.get("url"),
            )
            for item in payload.get("open_signals") or []
        )
        report = analyze_integration_capability_gaps(
            implemented_slugs=implemented_slugs,
            open_signals=open_signals,
        )
        return JSONResponse(report.to_dict())

    return router
