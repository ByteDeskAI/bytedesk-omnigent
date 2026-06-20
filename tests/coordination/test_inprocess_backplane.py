"""Unit tests for InProcessBackplane."""

from __future__ import annotations

import asyncio

import pytest

from omnigent.coordination.inprocess import InProcessBackplane


@pytest.mark.asyncio
async def test_claim_and_resolve_resource() -> None:
    bp = InProcessBackplane("replica-a")
    await bp.start()
    await bp.claim_resource("runner", "runner_1")
    assert await bp.resolve_resource("runner", "runner_1") == "replica-a"
    await bp.release_resource("runner", "runner_1")
    assert await bp.resolve_resource("runner", "runner_1") is None
    await bp.stop()


@pytest.mark.asyncio
async def test_index_put_get_delete_and_prefix() -> None:
    bp = InProcessBackplane("replica-a")
    await bp.start()
    await bp.index_put("pending", "conv/e1", {"event": {"type": "x"}})
    assert await bp.index_get("pending", "conv/e1") == {"event": {"type": "x"}}
    listed = await bp.index_list_prefix("pending", "conv/")
    assert listed == {"conv/e1": {"event": {"type": "x"}}}
    await bp.index_delete("pending", "conv/e1")
    assert await bp.index_get("pending", "conv/e1") is None
    await bp.stop()


@pytest.mark.asyncio
async def test_publish_subscribe_fanout() -> None:
    bp = InProcessBackplane("replica-a")
    await bp.start()

    async def _collect() -> list[bytes]:
        chunks: list[bytes] = []
        async for item in bp.subscribe("omnigent.coord.fanout.test"):
            chunks.append(item)
            if len(chunks) >= 2:
                break
        return chunks

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0.01)
    await bp.publish("omnigent.coord.fanout.test", b"one")
    await bp.publish("omnigent.coord.fanout.test", b"two")
    received = await asyncio.wait_for(task, timeout=2.0)
    assert received == [b"one", b"two"]
    await bp.stop()