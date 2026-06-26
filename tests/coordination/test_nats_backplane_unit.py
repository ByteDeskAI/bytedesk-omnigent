"""Unit tests for NatsBackplane encode/decode and KV helpers."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.coordination.nats_backplane import NatsBackplane


def test_encode_decode_round_trip() -> None:
    payload = {"replica_id": "replica-a", "kind": "runner"}
    raw = NatsBackplane._encode(payload)
    assert NatsBackplane._decode(raw) == payload


def test_decode_returns_none_for_invalid_payload() -> None:
    assert NatsBackplane._decode(b"not-json") is None
    assert NatsBackplane._decode(json.dumps([]).encode("utf-8")) is None
    assert NatsBackplane._decode(None) is None


@pytest.mark.asyncio
async def test_start_raises_when_nats_py_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "nats", None)
    bp = NatsBackplane("nats://127.0.0.1:4222", replica_id="r1")
    with pytest.raises(RuntimeError, match="nats-py is required"):
        await bp.start()


@pytest.mark.asyncio
async def test_index_get_put_delete_and_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("nats")
    bp = NatsBackplane("nats://127.0.0.1:4222", replica_id="replica-x")
    kv = MagicMock()
    kv.put = AsyncMock()
    encoded = NatsBackplane._encode({"replica_id": "replica-x"})

    async def _get(key: str):
        if key == "runner.runner_1":
            return MagicMock(value=encoded)
        raise Exception("missing")

    kv.get = AsyncMock(side_effect=_get)
    kv.delete = AsyncMock()
    kv.keys = AsyncMock(return_value=["runner.runner_1"])
    bp._kv = {"registry": kv}
    bp._started = True
    bp._nc = MagicMock()
    bp._js = MagicMock()

    await bp.index_put("registry", "runner.runner_1", {"replica_id": "replica-x"})
    assert await bp.index_get("registry", "runner.runner_1") == {"replica_id": "replica-x"}
    assert await bp.index_get("registry", "runner.missing") is None
    assert await bp.resolve_resource("runner", "runner_1") == "replica-x"
    listed = await bp.index_list_prefix("registry", "runner.")
    assert listed == {"runner.runner_1": {"replica_id": "replica-x"}}
    await bp.index_delete("registry", "runner.runner_1")


@pytest.mark.asyncio
async def test_publish_and_subscribe_without_connection() -> None:
    bp = NatsBackplane("nats://127.0.0.1:4222", replica_id="r1")
    await bp.publish("omnigent.coord.fanout.pending.upsert", b"{}")
    messages = [msg async for msg in bp.subscribe("omnigent.coord.fanout.>")]
    assert messages == []


@pytest.mark.asyncio
async def test_try_acquire_is_create_only_mutex() -> None:
    """Real create-only lock: first acquire wins, a second (key live) loses,
    release deletes so a later acquire wins again (BDP-2579 F1)."""

    class _FakeKv:
        def __init__(self) -> None:
            self._keys: set[str] = set()

        async def create(self, key: str, value: bytes) -> int:
            del value
            if key in self._keys:
                raise RuntimeError("wrong last sequence: key exists")
            self._keys.add(key)
            return 1

        async def delete(self, key: str) -> bool:
            self._keys.discard(key)
            return True

    bp = NatsBackplane("nats://127.0.0.1:4222", replica_id="replica-a")
    bp._locks_kv = _FakeKv()  # bypass lazy bucket creation (no live NATS)

    assert await bp.try_acquire("session-heal:conv_1", ttl_s=30.0) is True
    # Second concurrent acquirer on the same lock loses.
    assert await bp.try_acquire("session-heal:conv_1", ttl_s=30.0) is False
    # A different lock is independent.
    assert await bp.try_acquire("session-heal:conv_2", ttl_s=30.0) is True
    # Release frees it for the next holder.
    await bp.release("session-heal:conv_1")
    assert await bp.try_acquire("session-heal:conv_1", ttl_s=30.0) is True
