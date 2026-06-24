"""Tests for FU1 compaction-summary capture (BDP-2147 T10, ADR-0132)."""

from __future__ import annotations

from types import SimpleNamespace

from omnigent.entities.conversation import CompactionData, MessageData
from omnigent.runtime.memory_capture import capture_compaction_summaries
from omnigent.stores.memory_store import SqlAlchemyMemoryStore


def _store(tmp_path) -> SqlAlchemyMemoryStore:
    return SqlAlchemyMemoryStore(f"sqlite:///{tmp_path / 'm.db'}")


def _compaction(item_id: str, summary: str):
    return SimpleNamespace(
        id=item_id,
        type="compaction",
        data=CompactionData(summary=summary, last_item_id="msg_x", model="m", token_count=10),
    )


def _message(item_id: str):
    return SimpleNamespace(
        id=item_id,
        type="message",
        data=MessageData(role="user", content=[{"type": "input_text", "text": "hi"}]),
    )


def test_capture_appends_summary_and_is_recallable(tmp_path) -> None:
    store = _store(tmp_path)
    n = capture_compaction_summaries(
        store, "conv_1", "ag_maya", [_compaction("cmp_1", "User chose in-pod fastembed for memory.")]
    )
    assert n == 1
    hits = store.query(scope="agent", owner="ag_maya", name="conv:conv_1:summary", query="fastembed")
    assert len(hits) == 1
    assert "fastembed" in hits[0].content


def test_capture_dedups_by_compaction_id(tmp_path) -> None:
    store = _store(tmp_path)
    item = _compaction("cmp_1", "summary text alpha")
    assert capture_compaction_summaries(store, "conv_1", "ag_maya", [item]) == 1
    # Re-persist of the same compaction item must not double-capture.
    assert capture_compaction_summaries(store, "conv_1", "ag_maya", [item]) == 0


def test_capture_skips_non_compaction_and_empty_summary(tmp_path) -> None:
    store = _store(tmp_path)
    items = [_message("msg_1"), _compaction("cmp_empty", "")]
    assert capture_compaction_summaries(store, "conv_1", "ag_maya", items) == 0


def test_capture_skips_without_agent_id(tmp_path) -> None:
    store = _store(tmp_path)
    assert capture_compaction_summaries(store, "conv_1", None, [_compaction("cmp_1", "x")]) == 0


def test_capture_append_failure_is_logged_and_swallowed() -> None:
    class _BoomStore:
        def exists_for_compaction(self, compaction_id: str) -> bool:
            return False

        def append(self, **kwargs) -> None:
            raise RuntimeError("db down")

    n = capture_compaction_summaries(
        _BoomStore(),
        "conv_1",
        "ag_maya",
        [_compaction("cmp_1", "summary survives append failure")],
    )
    assert n == 0
