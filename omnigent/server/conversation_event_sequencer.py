"""Shared chat event sequencing helpers for session ingress paths."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from omnigent.entities import (
    ConversationItem,
    FunctionCallData,
    FunctionCallOutputData,
    NewConversationItem,
    parse_item_data,
)
from omnigent.stores.conversation_store import (
    ConversationEventAudit,
    ConversationStore,
    NewConversationEventAudit,
)

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SequencedPersistResult:
    """Outcome from a sequenced conversation-item projection."""

    persisted: ConversationItem | None
    audit: ConversationEventAudit
    buffered: bool = False
    released: list[ConversationItem] = field(default_factory=list)


def call_id_for_item(item: NewConversationItem) -> str | None:
    """Return the function-call correlation id carried by *item*, if any."""
    if isinstance(item.data, FunctionCallData | FunctionCallOutputData):
        return item.data.call_id
    return None


def _dump_new_item(item: NewConversationItem) -> dict[str, Any]:
    return {
        "shape": "new_conversation_item",
        "type": item.type,
        "response_id": item.response_id,
        "data": item.data.model_dump(exclude_none=True),
        "created_by": item.created_by,
    }


def _load_new_item(payload: dict[str, Any]) -> NewConversationItem:
    item_type = payload.get("type")
    response_id = payload.get("response_id")
    raw_data = payload.get("data")
    if not isinstance(item_type, str) or not isinstance(response_id, str):
        raise ValueError("buffered item payload is missing type/response_id")
    if not isinstance(raw_data, dict):
        raise ValueError("buffered item payload is missing data")
    created_by = payload.get("created_by")
    return NewConversationItem(
        type=item_type,
        response_id=response_id,
        data=parse_item_data(item_type, {"type": item_type, **raw_data}),
        created_by=created_by if isinstance(created_by, str) else None,
    )


def _dump_persisted_item(item: ConversationItem) -> dict[str, Any]:
    return {
        "shape": "conversation_item",
        "item": item.to_api_dict(),
    }


def _record_event_audit(
    conversation_store: ConversationStore,
    audit: NewConversationEventAudit,
) -> ConversationEventAudit:
    record = getattr(conversation_store, "record_event_audit", None)
    if not callable(record):
        return ConversationEventAudit(**audit.__dict__, id="", created_at=0)
    return record(audit)


def _update_event_audit(
    conversation_store: ConversationStore,
    audit_id: str,
    **updates: Any,
) -> ConversationEventAudit | None:
    if not audit_id:
        return None
    update_audit = getattr(conversation_store, "update_event_audit", None)
    if not callable(update_audit):
        return None
    return update_audit(audit_id, **updates)


def _list_event_audit(
    conversation_store: ConversationStore,
    session_id: str,
    **filters: Any,
) -> list[ConversationEventAudit]:
    list_audit = getattr(conversation_store, "list_event_audit", None)
    if not callable(list_audit):
        return []
    return list_audit(session_id, **filters)


def _find_persisted_call_response_id(
    conversation_store: ConversationStore,
    session_id: str,
    call_id: str,
) -> str | None:
    items = conversation_store.list_items(session_id, limit=1000, order="desc").data
    for item in items:
        if item.type == "function_call" and isinstance(item.data, FunctionCallData):
            if item.data.call_id == call_id:
                return item.response_id
    return None


async def persist_sequenced_item(
    conversation_store: ConversationStore,
    session_id: str,
    item: NewConversationItem,
    *,
    source: str,
    event_type: str,
    raw_payload: dict[str, Any],
    provider_event_id: str | None = None,
    message_id: str | None = None,
) -> SequencedPersistResult:
    """
    Audit and append a chat item, buffering orphan tool outputs until their call.

    The function is harness-neutral: external transcript bridges and runner
    relays both pass through this helper before writing ``conversation_items``.
    """
    call_id = call_id_for_item(item)
    audit = await asyncio.to_thread(
        _record_event_audit,
        conversation_store,
        NewConversationEventAudit(
            conversation_id=session_id,
            source=source,
            event_type=event_type,
            provider_event_id=provider_event_id,
            response_id=item.response_id,
            call_id=call_id,
            message_id=message_id,
            raw_payload=raw_payload,
            canonical_payload=_dump_new_item(item),
        ),
    )
    audit_enabled = bool(audit.id)

    if (
        audit_enabled
        and item.type == "function_call_output"
        and isinstance(item.data, FunctionCallOutputData)
    ):
        matching_response_id = await asyncio.to_thread(
            _find_persisted_call_response_id,
            conversation_store,
            session_id,
            item.data.call_id,
        )
        if matching_response_id is None:
            updated = await asyncio.to_thread(
                _update_event_audit,
                conversation_store,
                audit.id,
                decision="buffered",
                canonical_payload=_dump_new_item(item),
            )
            return SequencedPersistResult(
                persisted=None,
                audit=updated or audit,
                buffered=True,
            )
        if item.response_id != matching_response_id:
            item = item.model_copy(update={"response_id": matching_response_id})

    persisted_items = await asyncio.to_thread(conversation_store.append, session_id, [item])
    persisted = persisted_items[0]
    updated_audit = await asyncio.to_thread(
        _update_event_audit,
        conversation_store,
        audit.id,
        decision="persisted",
        canonical_payload=_dump_persisted_item(persisted),
        conversation_item_id=persisted.id,
        response_id=persisted.response_id,
    )

    released: list[ConversationItem] = []
    if item.type == "function_call" and isinstance(item.data, FunctionCallData):
        released = await release_buffered_outputs(
            conversation_store,
            session_id,
            call_id=item.data.call_id,
            response_id=persisted.response_id,
        )

    return SequencedPersistResult(
        persisted=persisted,
        audit=updated_audit or audit,
        released=released,
    )


async def release_buffered_outputs(
    conversation_store: ConversationStore,
    session_id: str,
    *,
    call_id: str,
    response_id: str,
) -> list[ConversationItem]:
    """
    Persist buffered outputs for a completed function call.

    :returns: Persisted output items in audit arrival order.
    """
    audits = await asyncio.to_thread(
        _list_event_audit,
        conversation_store,
        session_id,
        decision="buffered",
        call_id=call_id,
        limit=100,
        order="asc",
    )
    released: list[ConversationItem] = []
    for audit in audits:
        if audit.canonical_payload is None:
            await asyncio.to_thread(
                _update_event_audit,
                conversation_store,
                audit.id,
                decision="ignored",
            )
            continue
        try:
            item = _load_new_item(audit.canonical_payload).model_copy(
                update={"response_id": response_id}
            )
        except Exception:
            _logger.exception(
                "Failed to load buffered conversation event audit=%s session=%s",
                audit.id,
                session_id,
            )
            await asyncio.to_thread(
                _update_event_audit,
                conversation_store,
                audit.id,
                decision="ignored",
            )
            continue
        try:
            persisted = (
                await asyncio.to_thread(
                    conversation_store.append,
                    session_id,
                    [item],
                )
            )[0]
        except Exception:
            _logger.exception(
                "Failed to release buffered conversation event audit=%s session=%s",
                audit.id,
                session_id,
            )
            continue
        released.append(persisted)
        await asyncio.to_thread(
            _update_event_audit,
            conversation_store,
            audit.id,
            decision="released",
            canonical_payload=_dump_persisted_item(persisted),
            conversation_item_id=persisted.id,
            response_id=persisted.response_id,
        )
    return released


async def flush_orphaned_outputs(
    conversation_store: ConversationStore,
    session_id: str,
) -> list[ConversationItem]:
    """
    Persist buffered outputs whose matching function call never arrived.

    Called at terminal/status boundaries so accepted tool-output data is never
    stranded in the audit buffer. The item keeps its original response id; the
    audit decision records that it was orphan-flushed rather than paired.
    """
    audits = await asyncio.to_thread(
        _list_event_audit,
        conversation_store,
        session_id,
        decision="buffered",
        limit=100,
        order="asc",
    )
    flushed: list[ConversationItem] = []
    for audit in audits:
        if audit.canonical_payload is None:
            await asyncio.to_thread(
                _update_event_audit,
                conversation_store,
                audit.id,
                decision="ignored",
            )
            continue
        try:
            item = _load_new_item(audit.canonical_payload)
        except Exception:
            _logger.exception(
                "Failed to load orphaned conversation event audit=%s session=%s",
                audit.id,
                session_id,
            )
            await asyncio.to_thread(
                _update_event_audit,
                conversation_store,
                audit.id,
                decision="ignored",
            )
            continue
        try:
            persisted = (
                await asyncio.to_thread(
                    conversation_store.append,
                    session_id,
                    [item],
                )
            )[0]
        except Exception:
            _logger.exception(
                "Failed to flush orphaned conversation event audit=%s session=%s",
                audit.id,
                session_id,
            )
            continue
        flushed.append(persisted)
        await asyncio.to_thread(
            _update_event_audit,
            conversation_store,
            audit.id,
            decision="orphan_flushed",
            canonical_payload=_dump_persisted_item(persisted),
            conversation_item_id=persisted.id,
            response_id=persisted.response_id,
        )
    return flushed
