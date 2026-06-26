"""Deterministic fabric policies for capacity, placement, quarantine, and replay."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .models import CapacityRecord, DlqRecord, PlacementDecision, RunnerJob


def _unix_ms() -> int:
    return int(time.time() * 1000)


class FabricCapacityRejected(Exception):
    def __init__(self, *, scope: str, key: str, reason: str) -> None:
        super().__init__(reason)
        self.scope = scope
        self.key = key
        self.reason = reason


class ReplaySimulationRequired(Exception):
    def __init__(self, dlq_id: str) -> None:
        super().__init__(f"simulate replay before replaying DLQ record {dlq_id}")
        self.dlq_id = dlq_id


@dataclass(frozen=True)
class QuarantineRecord:
    resource_type: str
    resource_id: str
    reason: str
    failures: int
    quarantined_unix_ms: int
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "reason": self.reason,
            "failures": self.failures,
            "quarantined_unix_ms": self.quarantined_unix_ms,
            "metadata": dict(self.metadata),
        }


class InMemoryFabricCapacityPolicy:
    """Tenant/lane budget policy with explicit reserve/release records."""

    def __init__(
        self,
        *,
        tenant_limits: dict[str, int] | None = None,
        lane_limits: dict[str, int] | None = None,
        default_tenant_limit: int = 10,
        default_lane_limit: int = 100,
        now_ms: Callable[[], int] = _unix_ms,
    ) -> None:
        self._tenant_limits = dict(tenant_limits or {})
        self._lane_limits = dict(lane_limits or {})
        self._default_tenant_limit = default_tenant_limit
        self._default_lane_limit = default_lane_limit
        self._records: dict[tuple[str, str], CapacityRecord] = {}
        self._reservations: dict[str, tuple[str, str]] = {}
        self._lock = asyncio.Lock()
        self._now_ms = now_ms

    async def reserve(self, job: RunnerJob) -> CapacityRecord:
        async with self._lock:
            tenant = self._current(
                scope="tenant",
                key=job.tenant_id,
                limit=self._tenant_limits.get(job.tenant_id, self._default_tenant_limit),
            )
            lane = self._current(
                scope="lane",
                key=job.lane,
                limit=self._lane_limits.get(job.lane, self._default_lane_limit),
            )
            self._ensure_available(tenant)
            self._ensure_available(lane)
            tenant = self._with_used(tenant, tenant.used + 1)
            lane = self._with_used(lane, lane.used + 1)
            self._records[("tenant", job.tenant_id)] = tenant
            self._records[("lane", job.lane)] = lane
            self._reservations[job.job_id] = (job.tenant_id, job.lane)
            return CapacityRecord(
                scope="runner_job",
                key=job.job_id,
                limit=1,
                used=1,
                updated_unix_ms=self._now_ms(),
                metadata={"tenant_id": job.tenant_id, "lane": job.lane},
            )

    async def release(self, record: CapacityRecord) -> None:
        tenant_id = record.metadata.get("tenant_id")
        lane = record.metadata.get("lane")
        if not isinstance(tenant_id, str) or not isinstance(lane, str):
            reservation = self._reservations.get(record.key)
            if reservation is None:
                return
            tenant_id, lane = reservation
        async with self._lock:
            self._decrement("tenant", tenant_id)
            self._decrement("lane", lane)
            self._reservations.pop(record.key, None)

    def records(self) -> list[CapacityRecord]:
        return sorted(
            self._records.values(),
            key=lambda record: (record.scope, record.key),
        )

    def open_circuit(self, *, scope: str, key: str, reason: str) -> None:
        current = self._current(scope=scope, key=key, limit=self._limit(scope, key))
        self._records[(scope, key)] = CapacityRecord(
            scope=scope,
            key=key,
            limit=current.limit,
            used=current.used,
            updated_unix_ms=self._now_ms(),
            circuit_open=True,
            metadata={"reason": reason},
        )

    def close_circuit(self, *, scope: str, key: str) -> None:
        current = self._current(scope=scope, key=key, limit=self._limit(scope, key))
        self._records[(scope, key)] = CapacityRecord(
            scope=scope,
            key=key,
            limit=current.limit,
            used=current.used,
            updated_unix_ms=self._now_ms(),
            circuit_open=False,
            metadata={},
        )

    def _current(self, *, scope: str, key: str, limit: int) -> CapacityRecord:
        return self._records.get(
            (scope, key),
            CapacityRecord(
                scope=scope,
                key=key,
                limit=limit,
                used=0,
                updated_unix_ms=self._now_ms(),
            ),
        )

    def _limit(self, scope: str, key: str) -> int:
        if scope == "tenant":
            return self._tenant_limits.get(key, self._default_tenant_limit)
        if scope == "lane":
            return self._lane_limits.get(key, self._default_lane_limit)
        return 1

    def _ensure_available(self, record: CapacityRecord) -> None:
        if record.circuit_open:
            raise FabricCapacityRejected(
                scope=record.scope,
                key=record.key,
                reason="capacity circuit open",
            )
        if record.used >= record.limit:
            raise FabricCapacityRejected(
                scope=record.scope,
                key=record.key,
                reason="capacity budget exhausted",
            )

    def _with_used(self, record: CapacityRecord, used: int) -> CapacityRecord:
        return CapacityRecord(
            scope=record.scope,
            key=record.key,
            limit=record.limit,
            used=max(0, used),
            updated_unix_ms=self._now_ms(),
            circuit_open=record.circuit_open,
            metadata=dict(record.metadata),
        )

    def _decrement(self, scope: str, key: str) -> None:
        current = self._current(scope=scope, key=key, limit=self._limit(scope, key))
        self._records[(scope, key)] = self._with_used(current, current.used - 1)


class InMemoryQuarantinePolicy:
    """Quarantine resources after repeated fabric failures."""

    def __init__(
        self,
        *,
        threshold: int = 3,
        reasons: Iterable[str] = (
            "crash",
            "timeout",
            "auth",
            "max-delivery",
            "stale-heartbeat",
        ),
        now_ms: Callable[[], int] = _unix_ms,
    ) -> None:
        self._threshold = threshold
        self._reasons = set(reasons)
        self._failures: dict[tuple[str, str, str], int] = defaultdict(int)
        self._records: dict[tuple[str, str], QuarantineRecord] = {}
        self._now_ms = now_ms

    def record_failure(
        self,
        *,
        resource_type: str,
        resource_id: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> QuarantineRecord | None:
        key = (resource_type, resource_id, reason)
        self._failures[key] += 1
        failures = self._failures[key]
        if reason not in self._reasons or failures < self._threshold:
            return None
        record = QuarantineRecord(
            resource_type=resource_type,
            resource_id=resource_id,
            reason=reason,
            failures=failures,
            quarantined_unix_ms=self._now_ms(),
            metadata=dict(metadata or {}),
        )
        self._records[(resource_type, resource_id)] = record
        return record

    def apply(
        self,
        *,
        resource_type: str,
        resource_id: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> QuarantineRecord:
        record = QuarantineRecord(
            resource_type=resource_type,
            resource_id=resource_id,
            reason=reason,
            failures=self._failures.get((resource_type, resource_id, reason), 0),
            quarantined_unix_ms=self._now_ms(),
            metadata=dict(metadata or {}),
        )
        self._records[(resource_type, resource_id)] = record
        return record

    def release(self, *, resource_type: str, resource_id: str) -> None:
        self._records.pop((resource_type, resource_id), None)

    def is_quarantined(self, resource_type: str, resource_id: str | None) -> bool:
        if resource_id is None:
            return False
        return (resource_type, resource_id) in self._records

    def records(self) -> list[QuarantineRecord]:
        return sorted(
            self._records.values(),
            key=lambda record: (record.resource_type, record.resource_id),
        )


class WarmFirstPlacementStrategy:
    """Place warm runners before cold host spawn, then reject with a reason."""

    def __init__(
        self,
        *,
        capacity: InMemoryFabricCapacityPolicy,
        quarantine: InMemoryQuarantinePolicy | None = None,
        warm_runners: dict[str, Iterable[str]] | None = None,
        host_ids: Iterable[str] = (),
        now_ms: Callable[[], int] = _unix_ms,
    ) -> None:
        self._capacity = capacity
        self._quarantine = quarantine or InMemoryQuarantinePolicy()
        self._warm_runners = {
            lane: deque(runners) for lane, runners in (warm_runners or {}).items()
        }
        self._host_ids = list(host_ids)
        self._now_ms = now_ms

    async def place(self, job: RunnerJob) -> PlacementDecision:
        if self._quarantine.is_quarantined("lane", job.lane):
            return self._decision(
                job,
                mode="rejected",
                reason="lane quarantined",
            )
        try:
            reservation = await self._capacity.reserve(job)
        except FabricCapacityRejected as exc:
            return self._decision(
                job,
                mode="rejected",
                reason=f"{exc.scope}:{exc.key} {exc.reason}",
            )

        runner_id = self._next_warm_runner(job.lane)
        if runner_id is not None:
            return self._decision(
                job,
                mode="warm_hit",
                runner_id=runner_id,
                reason="warm runner available",
            )

        host_id = self._next_host()
        if host_id is not None:
            return self._decision(
                job,
                mode="cold_spawn",
                host_id=host_id,
                reason="no warm runner available",
            )

        await self._capacity.release(reservation)
        return self._decision(job, mode="rejected", reason="no eligible host")

    def _next_warm_runner(self, lane: str) -> str | None:
        runners = self._warm_runners.setdefault(lane, deque())
        while runners:
            runner_id = runners.popleft()
            if not self._quarantine.is_quarantined("runner", runner_id):
                return runner_id
        return None

    def _next_host(self) -> str | None:
        for host_id in self._host_ids:
            if not self._quarantine.is_quarantined("host", host_id):
                return host_id
        return None

    def _decision(
        self,
        job: RunnerJob,
        *,
        mode: str,
        reason: str,
        host_id: str | None = None,
        runner_id: str | None = None,
    ) -> PlacementDecision:
        return PlacementDecision(
            decision_id=f"place_{job.job_id}_{job.epoch}",
            runner_job_id=job.job_id,
            lane=job.lane,
            mode=mode,
            host_id=host_id,
            runner_id=runner_id,
            reason=reason,
            decided_unix_ms=self._now_ms(),
            metadata={"tenant_id": job.tenant_id, "org_id": job.org_id},
        )


ReplayPublisher = Callable[[DlqRecord], None | Awaitable[None]]


class InMemoryFabricRecoveryPolicy:
    """DLQ replay policy that requires simulation before mutation."""

    def __init__(
        self,
        *,
        publisher: ReplayPublisher | None = None,
    ) -> None:
        self._publisher = publisher
        self._simulated: set[str] = set()
        self._replayed: set[str] = set()
        self._discarded: set[str] = set()

    async def simulate_replay(self, record: DlqRecord) -> list[str]:
        operations = [
            f"load claim-check payload {record.payload_ref}",
            f"republish {record.idempotency_key} to {record.source_subject}",
            f"append audit event for {record.dlq_id}",
        ]
        self._simulated.add(record.dlq_id)
        return operations

    async def replay(self, record: DlqRecord) -> None:
        if record.dlq_id not in self._simulated:
            raise ReplaySimulationRequired(record.dlq_id)
        if self._publisher is not None:
            result = self._publisher(record)
            if result is not None:
                await result
        self._replayed.add(record.dlq_id)

    def discard(self, record: DlqRecord) -> None:
        self._discarded.add(record.dlq_id)

    def replayed(self, dlq_id: str) -> bool:
        return dlq_id in self._replayed

    def discarded(self, dlq_id: str) -> bool:
        return dlq_id in self._discarded
