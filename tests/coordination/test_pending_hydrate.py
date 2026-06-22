"""Tests for pending-elicitation KV hydrate on coordination cold start.

Two-replica manual smoke (after land):
1. Scale ``omnigent-server`` to 2; create a pending elicitation on replica A.
2. Confirm ``omnigent-pending-index`` KV has ``{conversation_id}/{elicitation_id}``.
3. Delete replica A pod; wait for replacement Ready.
4. ``GET /v1/sessions/{id}`` on any replica still replays the pending prompt
   (hydrated from KV before fan-out listener accepts traffic).
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.coordination import lifecycle as coord_lifecycle
from omnigent.coordination.inprocess import InProcessBackplane
from omnigent.runtime import pending_elicitations as pe


class _KvStubBackplane:
    """Non-inprocess stub so hydrate exercises the KV scan path."""

    def __init__(self, replica_id: str, entries: dict[str, dict[str, Any]]) -> None:
        self._replica_id = replica_id
        self._entries = entries

    @property
    def replica_id(self) -> str:
        return self._replica_id

    async def index_list_prefix(
        self,
        bucket: str,
        prefix: str,
    ) -> dict[str, dict[str, Any]]:
        del bucket
        return {
            key: value
            for key, value in self._entries.items()
            if key.startswith(prefix)
        }


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    pe.reset_for_tests()
    coord_lifecycle.reset_for_tests()
    yield
    pe.reset_for_tests()
    coord_lifecycle.reset_for_tests()


@pytest.mark.asyncio
async def test_hydrate_pending_index_noop_for_inprocess() -> None:
    bp = InProcessBackplane("replica-a")
    await bp.start()
    assert await coord_lifecycle.hydrate_pending_index(bp) == 0
    await bp.stop()


@pytest.mark.asyncio
async def test_hydrate_pending_index_loads_kv_without_fanout() -> None:
    event_a = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_a",
        "params": {"message": "approve A?"},
    }
    event_b = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_b",
        "params": {"message": "approve B?"},
    }
    bp = _KvStubBackplane(
        "replica-a",
        {
            "conv_one/elicit_a": {"event": event_a},
            "conv_two/elicit_b": {"event": event_b},
        },
    )
    loaded = await coord_lifecycle.hydrate_pending_index(bp)
    assert loaded == 2
    assert pe.count_for("conv_one") == 1
    assert pe.count_for("conv_two") == 1
    assert pe.count_for("conv_stale") == 0
    assert len(pe.snapshot_for("conv_one")) == 1
    assert len(pe.snapshot_for("conv_two")) == 1


@pytest.mark.asyncio
async def test_start_coordination_hydrates_before_fanout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_h",
        "params": {"message": "hydrate me"},
    }
    stub = _KvStubBackplane(
        "replica-hydrate",
        {"conv_h/elicit_h": {"event": event}},
    )

    async def _start(self) -> None:
        return None

    async def _stop(self) -> None:
        return None

    async def _subscribe(self, subject: str, *, durable_consumer: str | None = None):
        del subject, durable_consumer
        if False:
            yield b""

    stub.start = _start  # type: ignore[method-assign]
    stub.stop = _stop  # type: ignore[method-assign]
    stub.subscribe = _subscribe  # type: ignore[method-assign]

    import omnigent.coordination.factory as factory

    monkeypatch.setattr(factory, "resolve_coordination_backplane", lambda: stub)
    await coord_lifecycle.start_coordination()
    try:
        assert pe.count_for("conv_h") == 1
        assert len(pe.snapshot_for("conv_h")) == 1
    finally:
        await coord_lifecycle.stop_coordination()