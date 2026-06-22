"""Live NATS JetStream integration tests for the coordination backplane.

Skipped unless ``OMNIGENT_TEST_NATS_URL`` (or ``NATS_URL``) points at a
reachable broker, e.g. after ``kubectl -n bytedesk port-forward svc/omnigent-nats 4222:4222``:

    OMNIGENT_TEST_NATS_URL=nats://127.0.0.1:4222 \\
        pytest tests/coordination/test_nats_backplane_integration.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

pytest.importorskip("nats")

from omnigent.coordination.lifecycle import _fanout_listener  # noqa: PLC2701
from omnigent.coordination.nats_backplane import NatsBackplane
from omnigent.runtime import pending_elicitations as pe

_INTEGRATION = pytest.mark.integration


def _nats_test_url() -> str | None:
    for key in ("OMNIGENT_TEST_NATS_URL", "NATS_URL"):
        value = os.getenv(key, "").strip()
        if value:
            return value
    return None


async def _broker_reachable(url: str) -> bool:
    try:
        import nats

        nc = await nats.connect(servers=[url], connect_timeout=2)
        await nc.close()
        return True
    except Exception:
        return False


@pytest.fixture
async def nats_url() -> str:
    url = _nats_test_url()
    if url is None:
        pytest.skip("OMNIGENT_TEST_NATS_URL or NATS_URL not set")
    if not await _broker_reachable(url):
        pytest.skip(f"NATS broker unreachable at {url}")
    return url


@pytest.fixture(autouse=True)
def _reset_pending() -> None:
    pe.reset_for_tests()
    yield
    pe.reset_for_tests()


def _unique_replica(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@_INTEGRATION
@pytest.mark.asyncio
async def test_nats_claim_and_resolve_runner(nats_url: str) -> None:
    replica = _unique_replica("claim")
    bp = NatsBackplane(nats_url, replica_id=replica)
    await bp.start()
    try:
        runner_id = f"runner-{uuid.uuid4().hex[:8]}"
        await bp.claim_resource("runner", runner_id)
        assert await bp.resolve_resource("runner", runner_id) == replica
        await bp.release_resource("runner", runner_id)
        assert await bp.resolve_resource("runner", runner_id) is None
    finally:
        await bp.stop()


@_INTEGRATION
@pytest.mark.asyncio
async def test_nats_pending_kv_put_get(nats_url: str) -> None:
    replica = _unique_replica("pending")
    bp = NatsBackplane(nats_url, replica_id=replica)
    await bp.start()
    try:
        key = f"conv-{uuid.uuid4().hex[:8]}/elicit-{uuid.uuid4().hex[:8]}"
        event = {
            "type": "response.elicitation_request",
            "elicitation_id": key.split("/", 1)[1],
            "params": {"message": "integration?"},
        }
        await bp.index_put("pending", key, {"event": event})
        loaded = await bp.index_get("pending", key)
        assert loaded is not None
        assert loaded.get("event") == event
        await bp.index_delete("pending", key)
        assert await bp.index_get("pending", key) is None
    finally:
        await bp.stop()


@_INTEGRATION
@pytest.mark.asyncio
async def test_nats_fanout_between_two_replicas(nats_url: str) -> None:
    replica_a = _unique_replica("fan-a")
    replica_b = _unique_replica("fan-b")
    bp_a = NatsBackplane(nats_url, replica_id=replica_a)
    bp_b = NatsBackplane(nats_url, replica_id=replica_b)
    await bp_a.start()
    await bp_b.start()
    listener = asyncio.create_task(_fanout_listener(bp_b))
    await asyncio.sleep(0.2)
    try:
        conversation_id = f"conv-{uuid.uuid4().hex[:8]}"
        elicitation_id = f"elicit-{uuid.uuid4().hex[:8]}"
        payload = json.dumps(
            {
                "kind": "pending.upsert",
                "conversation_id": conversation_id,
                "elicitation_id": elicitation_id,
                "event": {
                    "type": "response.elicitation_request",
                    "elicitation_id": elicitation_id,
                    "params": {"message": "peer fanout"},
                },
                "origin": replica_a,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        await bp_a.publish("omnigent.coord.fanout.pending.upsert", payload)
        for _ in range(20):
            if pe.count_for(conversation_id) == 1:
                break
            await asyncio.sleep(0.1)
        assert pe.count_for(conversation_id) == 1
    finally:
        listener.cancel()
        with pytest.raises(asyncio.CancelledError):
            await listener
        await bp_a.stop()
        await bp_b.stop()