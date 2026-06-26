"""HTTP runner management routes backed by the runner control registry."""

from __future__ import annotations

from typing import Any, Protocol

from fastapi import APIRouter, Request

from omnigent.server.auth import AuthProvider
from omnigent.server.host_registry import RunnerExitReports
from omnigent.server.routes._auth_helpers import require_user


class _RunnerControlRegistry(Protocol):
    def online_runner_ids(self) -> list[str]: ...

    def get(self, runner_id: str) -> Any | None: ...

    def runner_owner(self, runner_id: str) -> str | None: ...

    def launch_owner(self, runner_id: str) -> str | None: ...


def create_runners_router(
    registry: _RunnerControlRegistry,
    *,
    auth_provider: AuthProvider | None = None,
    runner_exit_reports: RunnerExitReports | None = None,
) -> APIRouter:
    """Build runner list/status routes without exposing a WS tunnel."""
    router = APIRouter()

    def _get_user_id_from_request(request: Request) -> str | None:
        return require_user(request, auth_provider)

    def _owner_for(runner_id: str) -> str | None:
        return registry.runner_owner(runner_id) or registry.launch_owner(runner_id)

    @router.get("/runners")
    async def list_runners(request: Request) -> dict[str, list[dict[str, object]]]:
        user_id = _get_user_id_from_request(request)
        data: list[dict[str, object]] = []
        for runner_id in registry.online_runner_ids():
            owner = _owner_for(runner_id)
            if user_id is not None and owner is not None and owner != user_id:
                continue
            session = registry.get(runner_id)
            harnesses = list(getattr(getattr(session, "hello", None), "harnesses", []))
            data.append(
                {
                    "runner_id": runner_id,
                    "online": True,
                    "harnesses": harnesses,
                }
            )
        return {"data": data}

    @router.get("/runners/{runner_id}/status")
    async def runner_status(request: Request, runner_id: str) -> dict[str, str | bool]:
        user_id = _get_user_id_from_request(request)
        owner = _owner_for(runner_id)
        online = runner_id in registry.online_runner_ids()
        if online and user_id is not None and owner is not None and owner != user_id:
            online = False
        result: dict[str, str | bool] = {"runner_id": runner_id, "online": online}
        if not online and runner_exit_reports is not None:
            error = runner_exit_reports.get_visible(runner_id, user_id)
            if error is not None:
                result["error"] = error
        return result

    return router
