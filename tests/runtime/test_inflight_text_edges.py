"""Edge-case coverage for :mod:`omnigent.runtime.inflight_text`."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.runtime import inflight_text


@pytest.fixture(autouse=True)
def _clean_inflight_text_index() -> None:
    inflight_text.reset_for_tests()
    yield
    inflight_text.reset_for_tests()


def test_committed_message_text_rejects_non_list_content() -> None:
    """Committed items without a list ``content`` yield no matchable text."""
    assert inflight_text._committed_message_text({"content": "bad"}) is None


def test_committed_message_text_returns_none_without_output_text_blocks() -> None:
    """Message items with only non-text blocks do not buffer fingerprints."""
    item = {"content": [{"type": "tool_use", "id": "tu_1"}]}
    assert inflight_text._committed_message_text(item) is None


def test_record_publish_ignores_malformed_lifecycle_response() -> None:
    """Lifecycle events without a usable response id are no-ops."""
    cid = "conv_bad_lifecycle"
    inflight_text.record_publish(cid, {"type": "response.created", "response": "bad"})
    inflight_text.record_publish(cid, {"type": "response.in_progress", "response": {"id": ""}})
    assert inflight_text.snapshot_for(cid) == []


def test_record_publish_ignores_empty_text_delta() -> None:
    """Blank deltas do not create replayable text."""
    cid = "conv_empty_delta"
    inflight_text.record_publish(
        cid,
        {
            "type": "response.created",
            "response": {"id": "resp_1", "model": "nessie", "status": "queued", "created_at": 1},
        },
    )
    inflight_text.record_publish(cid, {"type": "response.output_text.delta", "delta": ""})
    assert inflight_text.snapshot_for(cid) == []


def test_snapshot_skips_native_messages_with_no_accumulated_text() -> None:
    """Native previews with an empty parts list are omitted from replay."""
    cid = "conv_empty_native"
    inflight_text._native_inflight[cid] = {
        "m_empty": inflight_text._NativeMessage(),
    }
    assert inflight_text.snapshot_for(cid) == []


def test_consume_committed_fingerprint_noop_for_empty_timestamp_list() -> None:
    """An empty timestamp bucket does not suppress unrelated deltas."""
    cid = "conv_empty_ts"
    fingerprint = inflight_text._text_fingerprint("Hello")
    inflight_text._native_committed[cid] = {fingerprint: []}
    assert inflight_text._consume_committed_fingerprint(cid, ["Hello"]) is False


def test_retire_native_message_evicts_oldest_retired_ids_at_cap() -> None:
    """Retiring many messages bounds the retired-id map."""
    cid = "conv_retire_cap"
    cap = inflight_text._MAX_NATIVE_RETIRED_PER_CONV
    for i in range(cap + 1):
        inflight_text._retire_native_message(cid, f"m{i}")
    assert len(inflight_text._native_retired[cid]) == cap
    assert "m0" not in inflight_text._native_retired[cid]


def test_buffer_committed_fingerprint_evicts_oldest_at_cap() -> None:
    """Buffered commit fingerprints are bounded per conversation."""
    cid = "conv_commit_cap"
    cap = inflight_text._MAX_NATIVE_COMMITTED_PER_CONV
    for i in range(cap + 1):
        inflight_text._buffer_committed_fingerprint(cid, f"fp{i}")
    assert len(inflight_text._native_committed[cid]) == cap
    assert "fp0" not in inflight_text._native_committed[cid]