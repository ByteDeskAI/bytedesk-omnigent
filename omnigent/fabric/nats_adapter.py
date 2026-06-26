"""Internal nats-py adapter for the runner fabric."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .manifest import FabricManifest
from .models import FabricEnvelope
from .service_host import NatsServiceHost

NATS_MSG_ID_HEADER = "Nats-Msg-Id"


class NatsFabricAdapter:
    """Small adapter that keeps raw nats-py calls out of fabric callers."""

    def __init__(
        self,
        nats_url: str,
        *,
        client: Any | None = None,
        connect_timeout_s: float = 2.0,
    ) -> None:
        self._nats_url = nats_url
        self._client = client
        self._connect_timeout_s = connect_timeout_s
        self._nc: Any | None = None

    async def ensure_assets(self, manifest: FabricManifest) -> None:
        client = await self._ensure_client()
        for stream in manifest.streams:
            await client.ensure_stream(stream.to_config())
        for bucket in manifest.kv_buckets:
            await client.ensure_kv_bucket(bucket.to_config())
        for store in manifest.object_stores:
            await client.ensure_object_store(store.to_config())

    async def publish_envelope(self, envelope: FabricEnvelope) -> None:
        client = await self._ensure_client()
        await client.publish(
            envelope.subject,
            envelope.to_json(),
            headers={NATS_MSG_ID_HEADER: envelope.idempotency_key, **envelope.headers},
        )

    async def request(self, subject: str, payload: bytes, *, timeout_s: float = 1.0) -> bytes:
        client = await self._ensure_client()
        return await client.request(subject, payload, timeout_s)

    async def serve_service_host(
        self,
        host: NatsServiceHost,
    ) -> NatsServiceRegistration:
        client = await self._ensure_client()
        return await client.serve_service_host(host)

    async def close(self) -> None:
        nc = self._nc
        self._nc = None
        self._client = None
        if nc is not None:
            await nc.close()

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import nats
            from nats.js.api import KeyValueConfig, ObjectStoreConfig, StreamConfig
        except ImportError as exc:  # pragma: no cover - exercised only without nats-py
            raise RuntimeError("nats-py is required for the NATS fabric adapter") from exc
        nc = await nats.connect(
            servers=[self._nats_url],
            connect_timeout=self._connect_timeout_s,
        )
        self._nc = nc
        js = nc.jetstream()
        self._client = _NatsPyJetStreamClient(
            nc=nc,
            js=js,
            key_value_config_cls=KeyValueConfig,
            stream_config_cls=StreamConfig,
            object_store_config_cls=ObjectStoreConfig,
        )
        return self._client


@dataclass(frozen=True)
class NatsServiceRegistration:
    subscriptions: tuple[Any, ...]

    async def drain(self) -> None:
        for subscription in self.subscriptions:
            drain = getattr(subscription, "drain", None)
            if drain is not None:
                await drain()
                continue
            unsubscribe = getattr(subscription, "unsubscribe", None)
            if unsubscribe is not None:
                await unsubscribe()


def _is_not_found_error(exc: Exception) -> bool:
    if isinstance(exc, KeyError):
        return True
    class_name = exc.__class__.__name__
    if class_name in {"NotFoundError", "BucketNotFoundError", "ObjectNotFoundError"}:
        return True
    for attr in ("code", "status_code", "err_code"):
        value = getattr(exc, attr, None)
        if value == 404:
            return True
    return False


class _NatsPyJetStreamClient:
    def __init__(
        self,
        *,
        nc: Any,
        js: Any,
        key_value_config_cls: type,
        stream_config_cls: type,
        object_store_config_cls: type,
    ) -> None:
        self._nc = nc
        self._js = js
        self._key_value_config_cls = key_value_config_cls
        self._stream_config_cls = stream_config_cls
        self._object_store_config_cls = object_store_config_cls

    async def ensure_stream(self, config: dict[str, Any]) -> None:
        stream_config = self._stream_config_cls(**config)
        try:
            await self._js.stream_info(config["name"])
        except Exception as exc:
            if not _is_not_found_error(exc):
                raise
            await self._js.add_stream(config=stream_config)
            return
        await self._js.update_stream(config=stream_config)

    async def ensure_kv_bucket(self, config: dict[str, Any]) -> None:
        bucket = config["bucket"]
        try:
            await self._js.key_value(bucket)
        except Exception as exc:
            if not _is_not_found_error(exc):
                raise
            await self._js.create_key_value(
                config=self._key_value_config_cls(**config)
            )

    async def ensure_object_store(self, config: dict[str, Any]) -> None:
        bucket = config["bucket"]
        try:
            await self._js.object_store(bucket)
        except Exception as exc:
            if not _is_not_found_error(exc):
                raise
            await self._js.create_object_store(
                config=self._object_store_config_cls(**config)
            )

    async def serve_service_host(
        self,
        host: NatsServiceHost,
    ) -> NatsServiceRegistration:
        subscriptions: list[Any] = []

        async def _respond_control(message: Any) -> None:
            payload = await host.handle_control(message.subject)
            await message.respond(payload)

        for subject in host.control_subjects():
            subscriptions.append(
                await self._nc.subscribe(subject, cb=_respond_control)
            )

        for endpoint in host.endpoints():
            async def _respond_endpoint(
                message: Any,
                *,
                subject: str = endpoint.subject,
            ) -> None:
                payload = await host.handle_endpoint(subject, bytes(message.data))
                await message.respond(payload)

            subscriptions.append(
                await self._nc.subscribe(
                    endpoint.subject,
                    queue=endpoint.queue_group,
                    cb=_respond_endpoint,
                )
            )

        return NatsServiceRegistration(subscriptions=tuple(subscriptions))

    async def publish(self, subject: str, payload: bytes, headers: dict[str, str]) -> None:
        await self._js.publish(subject, payload, headers=headers)

    async def request(self, subject: str, payload: bytes, timeout_s: float) -> bytes:
        msg = await self._nc.request(subject, payload, timeout=timeout_s)
        return bytes(msg.data)
