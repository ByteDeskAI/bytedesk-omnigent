"""Tests for D6 auto-episodic memory capture wiring (BDP-2276, ADR-0132/0142).

The reusable core (``capture_compaction_summaries``) is covered in
``tests/runtime/test_memory_capture.py``; here we cover the server-side
``_rescue_compaction_to_memory`` wrapper that resolves the conversation's owning
agent and stays cheap on the hot path.
"""

from __future__ import annotations

from types import SimpleNamespace

import omnigent.runtime as runtime
from omnigent.entities.conversation import CompactionData
from omnigent.server.routes.sessions import _rescue_compaction_to_memory
from omnigent.stores.memory_store import SqlAlchemyMemoryStore


class _FakeConvStore:
    """Minimal conversation store exposing only ``get_conversation``."""

    def __init__(self, agent_id: str | None) -> None:
        self._agent_id = agent_id
        self.lookups = 0

    def get_conversation(self, session_id: str):
        self.lookups += 1
        return SimpleNamespace(agent_id=self._agent_id)


def _compaction_item(item_id: str, summary: str):
    return SimpleNamespace(
        id=item_id,
        type="compaction",
        data=CompactionData(summary=summary, last_item_id="x", model="m", token_count=5),
    )


def _message_item(item_id: str):
    return SimpleNamespace(id=item_id, type="message", data=None)


async def test_rescues_compaction_into_owning_agent_memory(tmp_path, monkeypatch) -> None:
    store = SqlAlchemyMemoryStore(f"sqlite:///{tmp_path / 'm.db'}")
    monkeypatch.setattr(runtime, "get_memory_store", lambda: store)

    conv = _FakeConvStore("ag_maya")
    await _rescue_compaction_to_memory(
        conv, "conv_1", [_compaction_item("cmp_1", "chose in-pod fastembed for memory")]
    )

    hits = store.query(
        scope="agent", owner="ag_maya", name="conv:conv_1:summary", query="fastembed"
    )
    assert len(hits) == 1
    assert "fastembed" in hits[0].content


async def test_noop_for_non_compaction_skips_lookup_and_store(tmp_path, monkeypatch) -> None:
    # Hot path: a normal message must not resolve the agent or touch the memory
    # store at all (the type gate runs first).
    def _boom():
        raise AssertionError("get_memory_store must not run for non-compaction items")

    monkeypatch.setattr(runtime, "get_memory_store", _boom)
    conv = _FakeConvStore("ag_maya")

    await _rescue_compaction_to_memory(conv, "conv_1", [_message_item("msg_1")])

    assert conv.lookups == 0


async def test_capture_failure_is_swallowed(tmp_path, monkeypatch) -> None:
    # A capture failure must never propagate out of the persist path.
    def _boom():
        raise RuntimeError("memory store unavailable")

    monkeypatch.setattr(runtime, "get_memory_store", _boom)
    conv = _FakeConvStore("ag_maya")

    # Must not raise.
    await _rescue_compaction_to_memory(conv, "conv_1", [_compaction_item("cmp_1", "x")])
