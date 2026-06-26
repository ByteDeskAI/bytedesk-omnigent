"""REST facade for runtime feature flags."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.etag import parse_if_match
from omnigent.server.routes._auth_helpers import require_user

from .models import EvaluationContext, FlagDefinition, FlagValidationError
from .store import RuntimeFlagStore, runtime_flag_store_from_env


def create_runtime_flags_router(
    *,
    auth_provider: AuthProvider | None = None,
    store: RuntimeFlagStore | None = None,
) -> APIRouter:
    router = APIRouter()
    flag_store = store or runtime_flag_store_from_env()

    @router.get("/flags")
    async def list_flags(request: Request) -> JSONResponse:
        require_user(request, auth_provider)
        data = [revision.to_dict() for revision in await flag_store.list()]
        return JSONResponse({"data": data})

    @router.post("/flags", status_code=status.HTTP_201_CREATED)
    async def create_flag(request: Request, response: Response) -> JSONResponse:
        require_user(request, auth_provider)
        definition = _definition_from_payload(await request.json())
        revision = await flag_store.upsert(definition)
        response.headers["ETag"] = f'"{revision.revision}"'
        return JSONResponse(
            revision.to_dict(),
            status_code=status.HTTP_201_CREATED,
            headers={"ETag": f'"{revision.revision}"'},
        )

    @router.get("/flags/{key}")
    async def get_flag(request: Request, key: str, response: Response) -> JSONResponse:
        require_user(request, auth_provider)
        revision = await flag_store.get_revision(key)
        response.headers["ETag"] = f'"{revision.revision}"'
        return JSONResponse(revision.to_dict(), headers={"ETag": f'"{revision.revision}"'})

    @router.patch("/flags/{key}")
    async def patch_flag(request: Request, response: Response, key: str) -> JSONResponse:
        require_user(request, auth_provider)
        current = await flag_store.get_revision(key)
        patch = await request.json()
        if not isinstance(patch, dict):
            raise OmnigentError("body must be a JSON object", code=ErrorCode.INVALID_INPUT)
        merged = current.definition.to_dict()
        merged.update(patch)
        merged["key"] = key
        definition = _definition_from_payload(merged)
        revision = await flag_store.upsert(
            definition,
            if_match=parse_if_match(request.headers.get("if-match")),
        )
        response.headers["ETag"] = f'"{revision.revision}"'
        return JSONResponse(revision.to_dict(), headers={"ETag": f'"{revision.revision}"'})

    @router.post("/flags/{key}/evaluate")
    async def evaluate_flag(request: Request, key: str) -> JSONResponse:
        require_user(request, auth_provider)
        body = await request.json()
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise OmnigentError("body must be a JSON object", code=ErrorCode.INVALID_INPUT)
        result = await flag_store.evaluate(key, EvaluationContext.from_dict(body))
        return JSONResponse(result.to_dict())

    @router.get("/flags/{key}/history")
    async def flag_history(request: Request, key: str) -> JSONResponse:
        require_user(request, auth_provider)
        return JSONResponse(
            {"data": [revision.to_dict() for revision in await flag_store.history(key)]}
        )

    @router.post("/flags/{key}/rollback")
    async def rollback_flag(request: Request, response: Response, key: str) -> JSONResponse:
        require_user(request, auth_provider)
        body = await request.json()
        if not isinstance(body, dict) or "revision" not in body:
            raise OmnigentError(
                'body must be a JSON object {"revision": <int>}',
                code=ErrorCode.INVALID_INPUT,
            )
        target = int(body["revision"])
        revisions = await flag_store.history(key)
        match = next((entry for entry in revisions if entry.revision == target), None)
        if match is None:
            raise OmnigentError(
                f"runtime flag {key!r} has no revision {target}",
                code=ErrorCode.NOT_FOUND,
            )
        current = await flag_store.get_revision(key)
        written = await flag_store.upsert(match.definition, if_match=current.revision)
        response.headers["ETag"] = f'"{written.revision}"'
        return JSONResponse(written.to_dict(), headers={"ETag": f'"{written.revision}"'})

    return router


def _definition_from_payload(payload: Any) -> FlagDefinition:
    if not isinstance(payload, dict):
        raise OmnigentError("body must be a JSON object", code=ErrorCode.INVALID_INPUT)
    try:
        return FlagDefinition.from_dict(payload)
    except (FlagValidationError, KeyError, TypeError, ValueError) as exc:
        raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
