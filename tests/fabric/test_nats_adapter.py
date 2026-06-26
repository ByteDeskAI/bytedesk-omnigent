from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from omnigent.fabric.manifest import DEFAULT_FABRIC_MANIFEST
from omnigent.fabric.models import FabricEnvelope, SchedulerJob
from omnigent.fabric.nats_adapter import (
    NATS_MSG_ID_HEADER,
    NatsFabricAdapter,
    _NatsPyJetStreamClient,
)
from omnigent.fabric.service_host import NatsServiceEndpoint, NatsServiceHost


@dataclass
class _FakeJetStream:
    streams: list[dict] = field(default_factory=list)
    kv: list[dict] = field(default_factory=list)
    object_stores: list[dict] = field(default_factory=list)
    published: list[tuple[str, bytes, dict[str, str]]] = field(default_factory=list)
    kv_records: dict[tuple[str, str], bytes] = field(default_factory=dict)

    async def ensure_stream(self, config: dict) -> None:
        self.streams.append(config)

    async def ensure_kv_bucket(self, config: dict) -> None:
        self.kv.append(config)

    async def ensure_object_store(self, config: dict) -> None:
        self.object_stores.append(config)

    async def publish(self, subject: str, payload: bytes, headers: dict[str, str]) -> None:
        self.published.append((subject, payload, headers))

    async def request(self, subject: str, payload: bytes, timeout_s: float) -> bytes:
        del payload, timeout_s
        return f"reply:{subject}".encode()

    async def kv_put(self, bucket: str, key: str, payload: bytes) -> None:
        self.kv_records[(bucket, key)] = payload

    async def kv_get(self, bucket: str, key: str) -> bytes | None:
        return self.kv_records.get((bucket, key))

    async def kv_delete(self, bucket: str, key: str) -> None:
        self.kv_records.pop((bucket, key), None)


class _Config:
    def __init__(self, **kwargs) -> None:
        self.kwargs = dict(kwargs)


@dataclass
class _FakeNatsPyJetStream:
    streams: set[str] = field(default_factory=set)
    kv_buckets: set[str] = field(default_factory=set)
    object_stores: set[str] = field(default_factory=set)
    added_streams: list[_Config] = field(default_factory=list)
    updated_streams: list[_Config] = field(default_factory=list)
    created_kv: list[_Config] = field(default_factory=list)
    created_object_stores: list[_Config] = field(default_factory=list)

    async def stream_info(self, name: str) -> None:
        if name not in self.streams:
            raise KeyError(name)

    async def add_stream(self, *, config: _Config) -> None:
        self.added_streams.append(config)
        self.streams.add(config.kwargs["name"])

    async def update_stream(self, *, config: _Config) -> None:
        self.updated_streams.append(config)

    async def key_value(self, bucket: str) -> None:
        if bucket not in self.kv_buckets:
            raise KeyError(bucket)

    async def create_key_value(self, *, config: _Config) -> None:
        self.created_kv.append(config)
        self.kv_buckets.add(config.kwargs["bucket"])

    async def object_store(self, bucket: str) -> None:
        if bucket not in self.object_stores:
            raise KeyError(bucket)

    async def create_object_store(self, *, config: _Config) -> None:
        self.created_object_stores.append(config)
        self.object_stores.add(config.kwargs["bucket"])


@dataclass
class _FakeSubscription:
    subject: str
    queue: str | None
    drained: bool = False

    async def drain(self) -> None:
        self.drained = True


@dataclass
class _FakeMessage:
    subject: str
    data: bytes = b""
    replies: list[bytes] = field(default_factory=list)

    async def respond(self, payload: bytes) -> None:
        self.replies.append(payload)


@dataclass
class _FakeNatsConnection:
    subscriptions: list[tuple[_FakeSubscription, object]] = field(default_factory=list)

    async def subscribe(
        self,
        subject: str,
        *,
        queue: str | None = None,
        cb=None,
    ) -> _FakeSubscription:
        subscription = _FakeSubscription(subject=subject, queue=queue)
        self.subscriptions.append((subscription, cb))
        return subscription


@pytest.mark.asyncio
async def test_adapter_reconciles_manifest_assets_without_exposing_raw_nats() -> None:
    fake = _FakeJetStream()
    adapter = NatsFabricAdapter("nats://test", client=fake)

    await adapter.ensure_assets(DEFAULT_FABRIC_MANIFEST)

    assert [stream["name"] for stream in fake.streams] == [
        "OMNIGENT_SCHEDULER_JOBS",
        "OMNIGENT_RUNNER_JOBS",
        "OMNIGENT_RUNNER_EVENTS",
        "OMNIGENT_RUNNER_DLQ",
        "OMNIGENT_FABRIC_AUDIT",
    ]
    assert "omnigent-fabric-leases" in {bucket["bucket"] for bucket in fake.kv}
    assert "omnigent-fabric-replay-packs" in {
        store["bucket"] for store in fake.object_stores
    }


@pytest.mark.asyncio
async def test_nats_py_client_asset_reconcile_is_idempotent() -> None:
    fake = _FakeNatsPyJetStream(
        streams={"OMNIGENT_RUNNER_JOBS"},
        kv_buckets={"omnigent-fabric-leases"},
        object_stores={"omnigent-fabric-snapshots"},
    )
    client = _NatsPyJetStreamClient(
        nc=object(),
        js=fake,
        key_value_config_cls=_Config,
        stream_config_cls=_Config,
        object_store_config_cls=_Config,
    )

    await client.ensure_stream(
        {
            "name": "OMNIGENT_RUNNER_JOBS",
            "subjects": ["omnigent.runner.jobs.default"],
        }
    )
    await client.ensure_stream(
        {
            "name": "OMNIGENT_RUNNER_EVENTS",
            "subjects": ["omnigent.runner.events.>"],
        }
    )
    await client.ensure_kv_bucket(
        {"bucket": "omnigent-fabric-leases", "description": "leases"}
    )
    await client.ensure_kv_bucket(
        {"bucket": "omnigent-fabric-capacity", "description": "capacity"}
    )
    await client.ensure_object_store(
        {"bucket": "omnigent-fabric-snapshots", "description": "snapshots"}
    )
    await client.ensure_object_store(
        {"bucket": "omnigent-fabric-replay-packs", "description": "replay packs"}
    )

    assert [config.kwargs["name"] for config in fake.updated_streams] == [
        "OMNIGENT_RUNNER_JOBS"
    ]
    assert [config.kwargs["name"] for config in fake.added_streams] == [
        "OMNIGENT_RUNNER_EVENTS"
    ]
    assert [config.kwargs["bucket"] for config in fake.created_kv] == [
        "omnigent-fabric-capacity"
    ]
    assert [config.kwargs["bucket"] for config in fake.created_object_stores] == [
        "omnigent-fabric-replay-packs"
    ]


@pytest.mark.asyncio
async def test_nats_py_client_registers_service_controls_and_endpoints() -> None:
    nc = _FakeNatsConnection()
    client = _NatsPyJetStreamClient(
        nc=nc,
        js=_FakeNatsPyJetStream(),
        key_value_config_cls=_Config,
        stream_config_cls=_Config,
        object_store_config_cls=_Config,
    )
    service = NatsServiceHost(
        name="omnigent.fabric.test",
        version="1.2.3",
        service_id="service_1",
    )
    service.add_endpoint(
        NatsServiceEndpoint(
            name="echo",
            subject="omnigent.fabric.test.echo",
            handler=lambda payload: b"echo:" + payload,
        )
    )

    registration = await client.serve_service_host(service)

    callbacks = {
        subscription.subject: (subscription, callback)
        for subscription, callback in nc.subscriptions
    }
    ping = _FakeMessage("$SRV.PING")
    await callbacks["$SRV.PING"][1](ping)
    assert json.loads(ping.replies[0])["type"] == "io.nats.micro.v1.ping_response"

    endpoint_subscription, endpoint_callback = callbacks["omnigent.fabric.test.echo"]
    assert endpoint_subscription.queue == "q.omnigent.fabric.test"
    endpoint = _FakeMessage("omnigent.fabric.test.echo", b"payload")
    await endpoint_callback(endpoint)
    assert endpoint.replies == [b"echo:payload"]

    stats = _FakeMessage("$SRV.STATS.omnigent.fabric.test")
    await callbacks["$SRV.STATS.omnigent.fabric.test"][1](stats)
    stat_payload = json.loads(stats.replies[0])
    assert stat_payload["endpoints"][0]["num_requests"] == 1

    await registration.drain()

    assert all(subscription.drained for subscription in registration.subscriptions)


@pytest.mark.asyncio
async def test_publish_envelope_sets_nats_msg_id_for_idempotency() -> None:
    fake = _FakeJetStream()
    adapter = NatsFabricAdapter("nats://test", client=fake)
    job = SchedulerJob(
        job_id="sched_1",
        schedule_id="schedule_1",
        tenant_id="tenant_1",
        org_id="org_1",
        lane="default",
        fire_at_unix_ms=1,
        idempotency_key="schedule:schedule_1:1",
        payload_ref="sql-outbox:1",
    )
    envelope = FabricEnvelope.wrap(
        subject="omnigent.scheduler.jobs",
        idempotency_key=job.idempotency_key,
        payload=job,
    )

    await adapter.publish_envelope(envelope)

    assert len(fake.published) == 1
    subject, payload, headers = fake.published[0]
    assert subject == "omnigent.scheduler.jobs"
    assert FabricEnvelope.from_json(payload, SchedulerJob).payload == job
    assert headers[NATS_MSG_ID_HEADER] == "schedule:schedule_1:1"


@pytest.mark.asyncio
async def test_request_service_uses_adapter_request_reply() -> None:
    fake = _FakeJetStream()
    adapter = NatsFabricAdapter("nats://test", client=fake)

    reply = await adapter.request("omnigent.fabric.control.preflight", b"{}", timeout_s=0.5)

    assert reply == b"reply:omnigent.fabric.control.preflight"


@pytest.mark.asyncio
async def test_kv_helpers_delegate_through_adapter() -> None:
    fake = _FakeJetStream()
    adapter = NatsFabricAdapter("nats://test", client=fake)

    await adapter.kv_put("bucket", "runner_1", b"secret")

    assert await adapter.kv_get("bucket", "runner_1") == b"secret"

    await adapter.kv_delete("bucket", "runner_1")

    assert await adapter.kv_get("bucket", "runner_1") is None
