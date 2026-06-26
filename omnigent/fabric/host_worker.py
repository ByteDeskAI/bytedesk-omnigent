"""Fabric-owned host worker operations."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FabricHostWorkerError(Exception):
    status_code: int
    message: str


@dataclass(frozen=True)
class FabricHostLaunchResult:
    result: dict[str, str | None]
    acked: bool = True


class FabricHostWorker:
    """Adapter for host launch operations owned by the runner fabric."""

    async def launch_runner(
        self,
        *,
        host_registry: Any,
        host_id: str,
        binding_token: str,
        workspace: str,
        harness: str | None,
        timeout_s: float,
        host_connection: Any | None = None,
    ) -> FabricHostLaunchResult:
        if host_connection is not None:
            return await self._launch_runner_on_connection(
                host_registry=host_registry,
                host_connection=host_connection,
                binding_token=binding_token,
                workspace=workspace,
                harness=harness,
                timeout_s=timeout_s,
            )
        return await self._launch_runner_on_host_id(
            host_registry=host_registry,
            host_id=host_id,
            binding_token=binding_token,
            workspace=workspace,
            harness=harness,
            timeout_s=timeout_s,
        )

    async def _launch_runner_on_connection(
        self,
        *,
        host_registry: Any,
        host_connection: Any,
        binding_token: str,
        workspace: str,
        harness: str | None,
        timeout_s: float,
    ) -> FabricHostLaunchResult:
        from omnigent.host.frames import HostLaunchRunnerFrame, encode_host_frame

        request_id = secrets.token_hex(8)
        launch_future: asyncio.Future[dict[str, str | None]] = (
            asyncio.get_running_loop().create_future()
        )
        host_connection.pending_launches[request_id] = launch_future
        launch_frame = encode_host_frame(
            HostLaunchRunnerFrame(
                request_id=request_id,
                binding_token=binding_token,
                workspace=workspace,
                harness=harness,
            )
        )
        try:
            host_registry.send_text(host_connection, launch_frame)
        except ConnectionError:
            host_connection.pending_launches.pop(request_id, None)
            return FabricHostLaunchResult(
                result={"status": "ok", "runner_id": None, "error": None, "error_code": None}
            )
        try:
            result = await asyncio.wait_for(launch_future, timeout=timeout_s)
        except asyncio.TimeoutError:
            host_connection.pending_launches.pop(request_id, None)
            return FabricHostLaunchResult(
                result={
                    "status": "failed",
                    "runner_id": None,
                    "error": "host launch timed out",
                    "error_code": None,
                },
                acked=False,
            )
        return FabricHostLaunchResult(
            result={
                "status": result.get("status"),
                "runner_id": result.get("runner_id"),
                "error": result.get("error"),
                "error_code": result.get("error_code"),
            }
        )

    async def _launch_runner_on_host_id(
        self,
        *,
        host_registry: Any,
        host_id: str,
        binding_token: str,
        workspace: str,
        harness: str | None,
        timeout_s: float,
    ) -> FabricHostLaunchResult:
        from omnigent.server.host_control import HostControlError, request_host_launch_runner

        try:
            launch = await request_host_launch_runner(
                host_registry=host_registry,
                host_id=host_id,
                binding_token=binding_token,
                workspace=workspace,
                harness=harness,
                timeout_s=timeout_s,
            )
        except HostControlError as exc:
            raise FabricHostWorkerError(exc.status_code, exc.message) from exc
        return FabricHostLaunchResult(result=launch.result, acked=launch.acked)
