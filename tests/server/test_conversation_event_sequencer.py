"""Tests for harness-neutral chat event sequencing."""

from __future__ import annotations

import pytest

from omnigent.entities import FunctionCallData, FunctionCallOutputData, NewConversationItem
from omnigent.server.conversation_event_sequencer import (
    flush_orphaned_outputs,
    persist_sequenced_item,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def _call_item(response_id: str = "resp_call") -> NewConversationItem:
    return NewConversationItem(
        type="function_call",
        response_id=response_id,
        data=FunctionCallData(
            agent="native",
            name="sys_skill_apply",
            arguments='{"preview_id":"skprev_1"}',
            call_id="call_skill_1",
        ),
    )


def _output_item(response_id: str = "resp_output") -> NewConversationItem:
    return NewConversationItem(
        type="function_call_output",
        response_id=response_id,
        data=FunctionCallOutputData(call_id="call_skill_1", output="ok"),
    )


@pytest.mark.asyncio
async def test_buffers_tool_output_until_matching_call_arrives(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation()

    early_output = await persist_sequenced_item(
        store,
        conv.id,
        _output_item("resp_wrong"),
        source="external_conversation_item",
        event_type="external_conversation_item",
        raw_payload={"type": "external_conversation_item", "order": "output-first"},
    )

    assert early_output.buffered is True
    assert early_output.persisted is None
    assert store.list_items(conv.id).data == []

    call = await persist_sequenced_item(
        store,
        conv.id,
        _call_item("resp_call"),
        source="external_conversation_item",
        event_type="external_conversation_item",
        raw_payload={"type": "external_conversation_item", "order": "call-second"},
    )

    assert call.persisted is not None
    assert call.persisted.type == "function_call"
    assert [item.type for item in call.released] == ["function_call_output"]
    assert call.released[0].response_id == "resp_call"

    items = store.list_items(conv.id, order="asc").data
    assert [item.type for item in items] == ["function_call", "function_call_output"]
    assert [item.response_id for item in items] == ["resp_call", "resp_call"]

    audits = store.list_event_audit(conv.id, order="asc")
    assert [audit.decision for audit in audits] == ["released", "persisted"]
    assert audits[0].raw_payload["order"] == "output-first"


@pytest.mark.asyncio
async def test_rewrites_output_to_persisted_call_response_id(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation()

    await persist_sequenced_item(
        store,
        conv.id,
        _call_item("resp_call"),
        source="runner_relay",
        event_type="response.output_item.done",
        raw_payload={"type": "response.output_item.done", "item": {"type": "function_call"}},
    )

    output = await persist_sequenced_item(
        store,
        conv.id,
        _output_item("resp_later_turn"),
        source="runner_relay",
        event_type="response.output_item.done",
        raw_payload={
            "type": "response.output_item.done",
            "item": {"type": "function_call_output"},
        },
    )

    assert output.buffered is False
    assert output.persisted is not None
    assert output.persisted.response_id == "resp_call"

    items = store.list_items(conv.id, order="asc").data
    assert [item.response_id for item in items] == ["resp_call", "resp_call"]
    audits = store.list_event_audit(conv.id, order="asc")
    assert audits[1].response_id == "resp_call"
    assert audits[1].decision == "persisted"


@pytest.mark.asyncio
async def test_flushes_orphaned_output_when_call_never_arrives(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation()

    early_output = await persist_sequenced_item(
        store,
        conv.id,
        _output_item("resp_orphan"),
        source="runner_relay",
        event_type="response.output_item.done",
        raw_payload={
            "type": "response.output_item.done",
            "item": {"type": "function_call_output"},
        },
    )
    assert early_output.buffered is True
    assert store.list_items(conv.id).data == []

    flushed = await flush_orphaned_outputs(store, conv.id)

    assert [item.type for item in flushed] == ["function_call_output"]
    assert flushed[0].response_id == "resp_orphan"
    assert store.list_items(conv.id, order="asc").data[0].id == flushed[0].id
    audits = store.list_event_audit(conv.id, order="asc")
    assert [audit.decision for audit in audits] == ["orphan_flushed"]
