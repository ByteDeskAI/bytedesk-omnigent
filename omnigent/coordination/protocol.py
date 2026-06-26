"""Coordination backplane protocol (omnigent multi-replica seam)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol, runtime_checkable

ResourceKind = Literal["runner", "host"]

KV_REGISTRY = "omnigent-coord-registry"
KV_PENDING = "omnigent-pending-index"
KV_PRESENCE = "omnigent-presence"
KV_LOCKS = "omnigent-coord-locks"
STREAM_COORD_EVENTS = "OMNIGENT_COORD_EVENTS"


@runtime_checkable
class CoordinationBackplane(Protocol):
    """Durable cross-replica coordination for omnigent-server."""

    @property
    def replica_id(self) -> str:
        """This server replica's stable id."""

    async def start(self) -> None:
        """Connect and ensure JetStream/KV assets (no-op for inprocess)."""

    async def stop(self) -> None:
        """Release connections and background tasks."""

    async def claim_resource(
        self,
        kind: ResourceKind,
        resource_id: str,
        *,
        ttl_s: int = 90,
    ) -> None:
        """Record that this replica owns a live tunnel resource."""

    async def resolve_resource(self, kind: ResourceKind, resource_id: str) -> str | None:
        """Return the replica_id holding the resource, or None."""

    async def release_resource(self, kind: ResourceKind, resource_id: str) -> None:
        """Drop a resource claim (idempotent)."""

    async def try_acquire(self, lock_name: str, *, ttl_s: float) -> bool:
        """Atomically acquire a named cross-replica mutex.

        Unlike :meth:`claim_resource` (last-write-wins presence), this is a real
        create-only lock: exactly one of N concurrent callers across all
        replicas gets ``True``; the rest get ``False``. A crashed holder's lock
        self-expires after ``ttl_s`` so the work is never stranded. The
        single-replica/in-process backplane always returns ``True`` (no-op
        lock). See BDP-2579 F1.
        """

    async def release(self, lock_name: str) -> None:
        """Release a lock acquired via :meth:`try_acquire` (idempotent)."""

    async def index_put(
        self,
        bucket: str,
        key: str,
        value: dict[str, Any],
        *,
        ttl_s: int | None = None,
    ) -> None:
        """Upsert a JSON-serializable value in a logical bucket."""

    async def index_get(self, bucket: str, key: str) -> dict[str, Any] | None:
        """Read a value; None when absent."""

    async def index_delete(self, bucket: str, key: str) -> None:
        """Delete a key (idempotent)."""

    async def index_list_prefix(self, bucket: str, prefix: str) -> dict[str, dict[str, Any]]:
        """Return all keys under prefix mapped to values."""

    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        durable: bool = False,
    ) -> None:
        """Fan-out an event to other replicas."""

    def subscribe(
        self,
        subject: str,
        *,
        durable_consumer: str | None = None,
    ) -> AsyncIterator[bytes]:
        """Yield payloads published by :meth:`publish`."""
