"""In-process coordination backplane (default, zero deps)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from typing import Any

from omnigent.coordination.protocol import ResourceKind


class InProcessBackplane:
    """Single-replica coordination — mirrors today's in-memory posture."""

    def __init__(self, replica_id: str) -> None:
        self._replica_id = replica_id
        self._lock = threading.RLock()
        self._registry: dict[str, str] = {}
        self._index: dict[str, dict[str, dict[str, Any]]] = {}
        self._pub_sub: dict[str, list[asyncio.Queue[bytes]]] = {}
        self._started = False

    @property
    def replica_id(self) -> str:
        return self._replica_id

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False
        with self._lock:
            self._pub_sub.clear()

    def _reg_key(self, kind: ResourceKind, resource_id: str) -> str:
        return f"{kind}:{resource_id}"

    async def claim_resource(
        self,
        kind: ResourceKind,
        resource_id: str,
        *,
        ttl_s: int = 90,
    ) -> None:
        del ttl_s  # in-process has no TTL eviction
        with self._lock:
            self._registry[self._reg_key(kind, resource_id)] = self._replica_id

    async def resolve_resource(self, kind: ResourceKind, resource_id: str) -> str | None:
        with self._lock:
            return self._registry.get(self._reg_key(kind, resource_id))

    async def release_resource(self, kind: ResourceKind, resource_id: str) -> None:
        with self._lock:
            self._registry.pop(self._reg_key(kind, resource_id), None)

    async def try_acquire(self, lock_name: str, *, ttl_s: float) -> bool:
        # Single-replica: there is no second replica to coordinate with, so the
        # lock always succeeds. In-process concurrency is coalesced by the
        # caller's own single-flight (BDP-2579 F1).
        del lock_name, ttl_s
        return True

    async def release(self, lock_name: str) -> None:
        del lock_name

    async def index_put(
        self,
        bucket: str,
        key: str,
        value: dict[str, Any],
        *,
        ttl_s: int | None = None,
    ) -> None:
        del ttl_s
        with self._lock:
            self._index.setdefault(bucket, {})[key] = value

    async def index_get(self, bucket: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            return self._index.get(bucket, {}).get(key)

    async def index_delete(self, bucket: str, key: str) -> None:
        with self._lock:
            bucket_map = self._index.get(bucket)
            if bucket_map is not None:
                bucket_map.pop(key, None)

    async def index_list_prefix(self, bucket: str, prefix: str) -> dict[str, dict[str, Any]]:
        with self._lock:
            bucket_map = self._index.get(bucket, {})
            return {k: v for k, v in bucket_map.items() if k.startswith(prefix)}

    def _matching_queues(self, subject: str) -> list[asyncio.Queue[bytes]]:
        with self._lock:
            exact = list(self._pub_sub.get(subject, []))
            wildcard: list[asyncio.Queue[bytes]] = []
            for pattern, queues in self._pub_sub.items():
                if pattern.endswith(".>") and subject.startswith(pattern[:-2]):
                    wildcard.extend(queues)
            return exact + wildcard

    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        durable: bool = False,
    ) -> None:
        del durable
        queues = self._matching_queues(subject)
        for q in queues:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    async def subscribe(
        self,
        subject: str,
        *,
        durable_consumer: str | None = None,
    ) -> AsyncIterator[bytes]:
        del durable_consumer
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)
        with self._lock:
            self._pub_sub.setdefault(subject, []).append(q)
        try:
            while True:
                yield await q.get()
        finally:
            with self._lock:
                subs = self._pub_sub.get(subject, [])
                if q in subs:
                    subs.remove(q)