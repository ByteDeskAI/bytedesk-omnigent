"""Unit tests for pending elicitation backplane sync."""

from __future__ import annotations

import asyncio

import pytest

from omnigent.coordination.inprocess import InProcessBackplane
from omnigent.coordination import lifecycle as coord_lifecycle
from omnigent.runtime import pending_elicitations as pe


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    pe.reset_for_tests()
    coord_lifecycle.reset_for_tests()
    yield
    pe.reset_for_tests()
    coord_lifecycle.reset_for_tests()


def test_apply_remote_upsert_and_delete() -> None:
    event = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_1",
        "params": {"message": "approve?"},
    }
    pe.apply_remote_upsert("conv_a", "elicit_1", event)
    assert pe.count_for("conv_a") == 1
    assert pe.lookup("elicit_1") == ("conv_a", event)

    pe.apply_remote_delete("conv_a", "elicit_1")
    assert pe.count_for("conv_a") == 0
    assert pe.lookup("elicit_1") is None


@pytest.mark.asyncio
async def test_fanout_listener_applies_peer_messages() -> None:
    bp = InProcessBackplane("replica-b")
    await bp.start()
    listener = asyncio.create_task(coord_lifecycle._fanout_listener(bp))  # noqa: SLF001
    await asyncio.sleep(0.1)

    import json

    payload = json.dumps(
        {
            "kind": "pending.upsert",
            "conversation_id": "conv_peer",
            "elicitation_id": "elicit_peer",
            "event": {
                "type": "response.elicitation_request",
                "elicitation_id": "elicit_peer",
            },
            "origin": "replica-a",
        }
    ).encode("utf-8")
    await bp.publish("omnigent.coord.fanout.pending.upsert", payload)
    await asyncio.sleep(0.2)
    assert pe.count_for("conv_peer") == 1

    delete_payload = json.dumps(
        {
            "kind": "pending.delete",
            "conversation_id": "conv_peer",
            "elicitation_id": "elicit_peer",
            "origin": "replica-a",
        }
    ).encode("utf-8")
    await bp.publish("omnigent.coord.fanout.pending.delete", delete_payload)
    await asyncio.sleep(0.2)
    assert pe.count_for("conv_peer") == 0

    listener.cancel()
    with pytest.raises(asyncio.CancelledError):
        await listener
    await bp.stop()