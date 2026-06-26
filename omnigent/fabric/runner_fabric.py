"""Runner acquisition facade for session routes."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from omnigent.runner.identity import token_bound_runner_id

from .host_worker import (
    FabricHostLaunchResult,
    FabricHostWorker,
    FabricHostWorkerError,
)

BindMode = Literal["set", "replace"]


@dataclass(frozen=True)
class FabricRunnerConflict(Exception):
    session_id: str
    runner_id: str


@dataclass(frozen=True)
class HostRunnerAcquisition:
    session_id: str
    host_id: str
    workspace: str
    harness: str | None
    conversation_store: Any
    host_registry: Any
    owner: str | None = None
    runner_control_registry: Any | None = None
    bind_mode: BindMode = "replace"
    timeout_s: float = 15.0
    host_connection: Any | None = None
    before_launch: Callable[[str], Awaitable[None] | None] | None = None


@dataclass(frozen=True)
class RunnerAcquisitionResult:
    runner_id: str
    acked: bool = True
    error_code: str | None = None
    error: str | None = None
    status_code: int | None = None


class HostWorkerRunnerFabric:
    """Facade used by routes to acquire host-backed runners."""

    def __init__(self, *, host_worker: FabricHostWorker | None = None) -> None:
        self._host_worker = host_worker or FabricHostWorker()

    async def ensure_runner(
        self,
        acquisition: HostRunnerAcquisition,
    ) -> RunnerAcquisitionResult:
        binding_token = secrets.token_urlsafe(32)
        runner_id = token_bound_runner_id(binding_token)

        if acquisition.bind_mode == "set":
            bound = await asyncio.to_thread(
                acquisition.conversation_store.set_runner_id,
                acquisition.session_id,
                runner_id,
            )
            if not bound:
                raise FabricRunnerConflict(acquisition.session_id, runner_id)
        else:
            await asyncio.to_thread(
                acquisition.conversation_store.replace_runner_id,
                acquisition.session_id,
                runner_id,
            )

        if acquisition.before_launch is not None:
            result = acquisition.before_launch(runner_id)
            if result is not None:
                await result

        if (
            acquisition.owner is not None
            and acquisition.runner_control_registry is not None
        ):
            acquisition.runner_control_registry.record_launch_owner(
                runner_id,
                acquisition.owner,
                token=binding_token,
            )

        try:
            launch = await self._host_worker.launch_runner(
                host_registry=acquisition.host_registry,
                host_id=acquisition.host_id,
                binding_token=binding_token,
                workspace=acquisition.workspace,
                harness=acquisition.harness,
                timeout_s=acquisition.timeout_s,
                host_connection=acquisition.host_connection,
            )
        except FabricHostWorkerError as exc:
            return RunnerAcquisitionResult(
                runner_id=runner_id,
                acked=False,
                error=exc.message,
                status_code=exc.status_code,
            )

        return RunnerAcquisitionResult(
            runner_id=runner_id,
            acked=launch.acked,
            error_code=launch.result.get("error_code"),
            error=launch.result.get("error"),
        )


__all__ = [
    "FabricHostLaunchResult",
    "FabricRunnerConflict",
    "HostRunnerAcquisition",
    "HostWorkerRunnerFabric",
    "RunnerAcquisitionResult",
]
