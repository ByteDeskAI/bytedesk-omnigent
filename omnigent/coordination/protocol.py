"""Coordination backplane protocol (omnigent multi-replica seam)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol, runtime_checkable

ResourceKind = Literal["runner", "host"]

KV_REGISTRY = "omnigent-coord-registry"
KV_PENDING = "omnigent-pending-index"
KV_PRESENCE = "omnigent-presence"
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
