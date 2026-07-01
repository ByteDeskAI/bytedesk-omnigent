"""Read API for high-value third-party integration capability blueprints."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse

from bytedesk_omnigent.integration_capabilities import (
    CapabilityCategory,
    get_integration_capability,
    integration_capability_categories,
    list_integration_capabilities,
)
from bytedesk_omnigent.integration_verification_assessment import (
    assess_integration_verification_evidence,
)
from bytedesk_omnigent.integration_verification_matrix import (
    compile_integration_verification_matrix,
)
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user

_VERIFICATION_ASSESSMENT_BODY = Body(default_factory=dict)


def _invalid_evidence_payload(detail: str) -> JSONResponse:
    return JSONResponse(
        {"error": "invalid_evidence_payload", "detail": detail}, status_code=422
    )


def _extract_provided_evidence(payload: dict) -> Mapping[str, Sequence[str]] | JSONResponse:
    provided_evidence = payload.get("provided_evidence", {})
    if not isinstance(provided_evidence, Mapping):
        return _invalid_evidence_payload("provided_evidence must be an object")

    for gate_id, evidence_items in provided_evidence.items():
        if not isinstance(gate_id, str):
            return _invalid_evidence_payload("provided_evidence keys must be gate id strings")
        if isinstance(evidence_items, str) or not isinstance(evidence_items, Sequence):
            return _invalid_evidence_payload(
                "provided_evidence values must be arrays of evidence strings"
            )
        if not all(isinstance(item, str) for item in evidence_items):
            return _invalid_evidence_payload(
                "provided_evidence values must be arrays of evidence strings"
            )
    return provided_evidence


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

    @router.post("/integration-capabilities/{slug}/verification-assessment")
    async def assess_capability_verification_evidence(
        request: Request,
        slug: str,
        payload: dict = _VERIFICATION_ASSESSMENT_BODY,
    ) -> JSONResponse:
        """Assess submitted rollout evidence against the verification matrix."""

        require_user(request, auth_provider)
        provided_evidence = _extract_provided_evidence(payload)
        if isinstance(provided_evidence, JSONResponse):
            return provided_evidence

        assessment = assess_integration_verification_evidence(
            slug, provided_evidence=provided_evidence
        )
        if assessment is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"unknown integration capability: {slug}"},
                status_code=404,
            )
        return JSONResponse(assessment)

    return router
