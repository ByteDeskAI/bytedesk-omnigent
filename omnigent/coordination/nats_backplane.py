"""NATS JetStream coordination backplane."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from omnigent.coordination.protocol import (
    KV_LOCKS,
    KV_PENDING,
    KV_PRESENCE,
    KV_REGISTRY,
    STREAM_COORD_EVENTS,
    ResourceKind,
)

_logger = logging.getLogger(__name__)

_FANOUT_PREFIX = "omnigent.coord.fanout"
_LOGICAL_BUCKETS: dict[str, str] = {
    "registry": KV_REGISTRY,
    "pending": KV_PENDING,
    "presence": KV_PRESENCE,
}


class NatsBackplane:
    """Cross-replica coordination via NATS JetStream KV + core pub/sub."""

    def __init__(self, nats_url: str, *, replica_id: str) -> None:
        self._nats_url = nats_url
        self._replica_id = replica_id
        self._nc: Any = None
        self._js: Any = None
        self._kv: dict[str, Any] = {}
        self._locks_kv: Any = None
        self._started = False

    @property
    def replica_id(self) -> str:
        return self._replica_id

    async def start(self) -> None:
        if self._started:
            return
        try:
            import nats
            from nats.js.api import KeyValueConfig, StreamConfig
        except ImportError as exc:
            raise RuntimeError(
                "nats-py is required for coordination_backplane=nats; "
                "install omnigent[coordination]"
            ) from exc

        self._nc = await nats.connect(
            servers=[self._nats_url],
            name=f"omnigent-server-{self._replica_id}",
            max_reconnect_attempts=-1,
        )
        self._js = self._nc.jetstream()
        for logical, bucket in _LOGICAL_BUCKETS.items():
            try:
                kv = await self._js.key_value(bucket)
            except Exception:  # noqa: BLE001 — bucket may not exist yet
                kv = await self._js.create_key_value(
                    config=KeyValueConfig(bucket=bucket, history=1)
                )
            self._kv[logical] = kv
        try:
            await self._js.stream_info(STREAM_COORD_EVENTS)
        except Exception:  # noqa: BLE001
            await self._js.add_stream(
                config=StreamConfig(
                    name=STREAM_COORD_EVENTS,
                    subjects=[f"{_FANOUT_PREFIX}.>", "omnigent.coord.durable.>"],
                    max_age=86_400,  # 24h retention
                    max_msgs=100_000,
                )
            )
        self._started = True
        _logger.info(
            "coordination backplane started (nats, replica=%s)",
            self._replica_id,
        )

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        if self._nc is not None:
            await self._nc.drain()
            await self._nc.close()
        self._nc = None
        self._js = None
        self._kv.clear()
        self._locks_kv = None

    def _kv_for(self, bucket: str) -> Any:
        for logical, name in _LOGICAL_BUCKETS.items():
            if bucket in (logical, name):
                kv = self._kv.get(logical)
                if kv is not None:
                    return kv
        raise KeyError(f"unknown coordination bucket {bucket!r}")

    @staticmethod
    def _encode(value: dict[str, Any]) -> bytes:
        return json.dumps(value, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _decode(raw: bytes | None) -> dict[str, Any] | None:
        if raw is None:
            return None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _lock_key(lock_name: str) -> str:
        """Map arbitrary logical lock names onto NATS KV-safe keys."""
        return f"lock.{hashlib.sha256(lock_name.encode('utf-8')).hexdigest()}"

    async def claim_resource(
        self,
        kind: ResourceKind,
        resource_id: str,
        *,
        ttl_s: int = 90,
    ) -> None:
        del ttl_s  # JetStream KV TTL is bucket-level; refresh via re-claim
        await self.index_put(
            "registry",
            f"{kind}.{resource_id}",
            {"replica_id": self._replica_id},
        )

    async def resolve_resource(self, kind: ResourceKind, resource_id: str) -> str | None:
        entry = await self.index_get("registry", f"{kind}.{resource_id}")
        if entry is None:
            return None
        replica = entry.get("replica_id")
        return replica if isinstance(replica, str) and replica else None

    async def release_resource(self, kind: ResourceKind, resource_id: str) -> None:
        await self.index_delete("registry", f"{kind}.{resource_id}")

    async def _locks_bucket(self, ttl_s: float) -> Any:
        """Lazily open/create the dedicated locks KV bucket.

        The bucket carries a key TTL (``KeyValueConfig(ttl=...)``) so a crashed
        lock holder's key self-expires — releasing the mutex without an explicit
        ``release`` (BDP-2579 F1). Created on first acquire with that call's
        ``ttl_s``; an already-existing bucket keeps its creation-time TTL.
        """
        if self._locks_kv is None:
            from nats.js.api import KeyValueConfig

            try:
                self._locks_kv = await self._js.key_value(KV_LOCKS)
            except Exception:  # noqa: BLE001 — bucket may not exist yet
                self._locks_kv = await self._js.create_key_value(
                    config=KeyValueConfig(bucket=KV_LOCKS, history=1, ttl=ttl_s)
                )
        return self._locks_kv

    async def try_acquire(self, lock_name: str, *, ttl_s: float) -> bool:
        kv = await self._locks_bucket(ttl_s)
        key = self._lock_key(lock_name)
        try:
            # create() adds the key iff it does not exist; an existing (live)
            # lock raises, so exactly one concurrent caller wins.
            await kv.create(key, self._replica_id.encode("utf-8"))
            return True
        except Exception:  # noqa: BLE001
            # Key already held → not acquired. (A NATS outage also lands here →
            # nobody heals, but the runner transport is down anyway then.)
            # ponytail: any-exception=False; tighten to KeyWrongLastSequenceError
            # only if a transient KV error must not block a heal.
            return False

    async def release(self, lock_name: str) -> None:
        if self._locks_kv is None:
            return
        with contextlib.suppress(Exception):
            await self._locks_kv.delete(self._lock_key(lock_name))

    async def index_put(
        self,
        bucket: str,
        key: str,
        value: dict[str, Any],
        *,
        ttl_s: int | None = None,
    ) -> None:
        del ttl_s
        kv = self._kv_for(bucket)
        await kv.put(key, self._encode(value))

    async def index_get(self, bucket: str, key: str) -> dict[str, Any] | None:
        kv = self._kv_for(bucket)
        try:
            entry = await kv.get(key)
        except Exception:  # noqa: BLE001 — nats-py raises on missing key
            return None
        return self._decode(entry.value)

    async def index_delete(self, bucket: str, key: str) -> None:
        kv = self._kv_for(bucket)
        with contextlib.suppress(Exception):
            await kv.delete(key)

    async def index_list_prefix(
        self,
        bucket: str,
        prefix: str,
    ) -> dict[str, dict[str, Any]]:
        kv = self._kv_for(bucket)
        out: dict[str, dict[str, Any]] = {}
        try:
            keys = await kv.keys()
        except Exception:  # noqa: BLE001
            return out
        for key in keys:
            if not isinstance(key, str) or not key.startswith(prefix):
                continue
            entry = await self.index_get(bucket, key)
            if entry is not None:
                out[key] = entry
        return out

    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        durable: bool = False,
    ) -> None:
        if self._nc is None:
            return
        if durable:
            await self._js.publish(subject, payload)
        else:
            await self._nc.publish(subject, payload)

    async def subscribe(
        self,
        subject: str,
        *,
        durable_consumer: str | None = None,
    ) -> AsyncIterator[bytes]:
        del durable_consumer  # core fan-out uses ephemeral subs per replica
        if self._nc is None:
            if False:  # pragma: no cover — keep this an async generator
                yield b""
            return
        sub = await self._nc.subscribe(subject)
        try:
            async for msg in sub.messages:
                yield msg.data
        finally:
            await sub.unsubscribe()


__all__ = ["NatsBackplane"]
