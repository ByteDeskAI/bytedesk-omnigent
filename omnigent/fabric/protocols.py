"""Internal fabric seams for scheduling, placement, credentials, and recovery."""

from __future__ import annotations

from typing import Protocol

from .models import (
    CapacityRecord,
    CredentialReference,
    DlqRecord,
    PlacementDecision,
    RunnerJob,
    SchedulerJob,
)


class RunnerFabric(Protocol):
    async def ensure_runner(self, session: object) -> PlacementDecision:
        """Acquire or launch the runner for a session."""


class SchedulerDispatch(Protocol):
    async def dispatch(self, job: SchedulerJob) -> None:
        """Publish a claimed schedule fire into the fabric."""


class RunnerPlacementStrategy(Protocol):
    async def place(self, job: RunnerJob) -> PlacementDecision:
        """Choose warm runner, cold spawn, or rejection for a runner job."""


class RunnerPoolPolicy(Protocol):
    async def reconcile(self, lane: str) -> None:
        """Reconcile warm and persistent runner pool state for a lane."""


class RunnerCredentialStore(Protocol):
    async def mint(self, job: RunnerJob) -> CredentialReference:
        """Mint a short-lived runner credential reference."""

    async def revoke(self, ref: str) -> None:
        """Revoke a previously minted credential reference."""

    async def lookup(self, ref: str) -> CredentialReference | None:
        """Look up a non-revoked runner credential reference."""


class FabricCapacityPolicy(Protocol):
    async def reserve(self, job: RunnerJob) -> CapacityRecord:
        """Reserve tenant and lane capacity for a runner job."""

    async def release(self, record: CapacityRecord) -> None:
        """Release a previous capacity reservation."""


class FabricRecoveryPolicy(Protocol):
    async def simulate_replay(self, record: DlqRecord) -> list[str]:
        """Return the operations a replay would perform without mutating state."""

    async def replay(self, record: DlqRecord) -> None:
        """Replay a DLQ record after simulation has been accepted."""
