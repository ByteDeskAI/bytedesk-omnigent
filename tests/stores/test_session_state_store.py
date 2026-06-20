"""Tests for :class:`SqlAlchemySessionStateStore` (Phase 6d, BDP-2342).

The store is an additive facade over the existing
``conversations.session_state`` / ``conversations.session_usage`` columns —
no new table. These tests prove the facade round-trips both JSON blobs,
defaults absent columns to ``{}``, leaves the conversation store working
unchanged (the canonical writer and the facade see the same column), and is
a no-op for unknown conversations.
"""

from __future__ import annotations

import pytest

from bytedesk_omnigent.session_state_store import (
    SessionStateSnapshot,
    SqlAlchemySessionStateStore,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemySessionStateStore:
    """A fresh facade store backed by the test SQLite DB.

    :param db_uri: Per-test SQLite URI from the root conftest fixture.
    :returns: A ready-to-use :class:`SqlAlchemySessionStateStore`.
    """
    return SqlAlchemySessionStateStore(db_uri)


@pytest.fixture()
def conv_store(db_uri: str) -> SqlAlchemyConversationStore:
    """The canonical conversation store sharing the same DB.

    :param db_uri: Per-test SQLite URI.
    :returns: A :class:`SqlAlchemyConversationStore`.
    """
    return SqlAlchemyConversationStore(db_uri)


@pytest.fixture()
def conversation_id(conv_store: SqlAlchemyConversationStore) -> str:
    """Create a real conversation row and return its ID.

    The facade reads/writes the ``conversations`` table, so a real row
    must exist for an UPDATE to match.

    :param conv_store: The canonical conversation store.
    :returns: A conversation ID, e.g. ``"conv_abc123"``.
    """
    return conv_store.create_conversation().id


# ── defaults: absent columns decode to {} ─────────────────────────────────────


def test_fresh_conversation_has_empty_state_and_usage(
    store: SqlAlchemySessionStateStore,
    conversation_id: str,
) -> None:
    """A brand-new conversation reads back empty dicts (NULL columns → {})."""
    assert store.get_state(conversation_id) == {}
    assert store.get_usage(conversation_id) == {}


def test_snapshot_of_fresh_conversation(
    store: SqlAlchemySessionStateStore,
    conversation_id: str,
) -> None:
    """``get_snapshot`` returns an empty-but-present snapshot for a fresh row."""
    snap = store.get_snapshot(conversation_id)
    assert snap == SessionStateSnapshot(
        conversation_id=conversation_id, state={}, usage={}
    )


# ── state round-trip ──────────────────────────────────────────────────────────


def test_set_state_round_trips(
    store: SqlAlchemySessionStateStore,
    conversation_id: str,
) -> None:
    """``set_state`` then ``get_state`` returns the same dict."""
    state = {"approved_pushes": 2, "last_actor": "alice", "flags": {"strict": True}}
    store.set_state(conversation_id, state)
    assert store.get_state(conversation_id) == state


def test_set_state_overwrites_previous(
    store: SqlAlchemySessionStateStore,
    conversation_id: str,
) -> None:
    """``set_state`` is a full overwrite, not a merge."""
    store.set_state(conversation_id, {"a": 1, "b": 2})
    store.set_state(conversation_id, {"c": 3})
    assert store.get_state(conversation_id) == {"c": 3}


# ── usage round-trip ──────────────────────────────────────────────────────────


def test_set_usage_round_trips_with_nested_by_model(
    store: SqlAlchemySessionStateStore,
    conversation_id: str,
) -> None:
    """``set_usage`` round-trips, including a nested ``by_model`` sub-dict."""
    usage = {
        "input_tokens": 1500,
        "output_tokens": 350,
        "total_tokens": 1850,
        "by_model": {"claude-opus-4-8": {"input_tokens": 1500, "total_cost_usd": 0.42}},
    }
    store.set_usage(conversation_id, usage)
    assert store.get_usage(conversation_id) == usage


# ── snapshot reflects both columns ────────────────────────────────────────────


def test_snapshot_reflects_state_and_usage(
    store: SqlAlchemySessionStateStore,
    conversation_id: str,
) -> None:
    """``get_snapshot`` carries both the state and usage blobs together."""
    store.set_state(conversation_id, {"k": "v"})
    store.set_usage(conversation_id, {"total_tokens": 99})
    snap = store.get_snapshot(conversation_id)
    assert snap is not None
    assert snap.conversation_id == conversation_id
    assert snap.state == {"k": "v"}
    assert snap.usage == {"total_tokens": 99}


# ── interop with the canonical conversation store ─────────────────────────────


def test_facade_reads_what_conversation_store_wrote(
    store: SqlAlchemySessionStateStore,
    conv_store: SqlAlchemyConversationStore,
    conversation_id: str,
) -> None:
    """The facade sees state written by the canonical ConversationStore.

    Proves the facade wraps the *same* columns — ConversationStore keeps
    working unchanged; there is no second source of truth.
    """
    conv_store.set_session_state(conversation_id, {"written_by": "conversation_store"})
    conv_store.set_session_usage(conversation_id, {"total_tokens": 7})
    assert store.get_state(conversation_id) == {"written_by": "conversation_store"}
    assert store.get_usage(conversation_id) == {"total_tokens": 7}


def test_conversation_store_reads_what_facade_wrote(
    store: SqlAlchemySessionStateStore,
    conv_store: SqlAlchemyConversationStore,
    conversation_id: str,
) -> None:
    """The ConversationStore entity reflects state written through the facade."""
    store.set_state(conversation_id, {"written_by": "facade"})
    store.set_usage(conversation_id, {"total_tokens": 11})
    conv = conv_store.get_conversation(conversation_id)
    assert conv is not None
    assert conv.session_state == {"written_by": "facade"}
    assert conv.session_usage == {"total_tokens": 11}


# ── unknown conversation ──────────────────────────────────────────────────────


def test_get_snapshot_returns_none_for_unknown(
    store: SqlAlchemySessionStateStore,
) -> None:
    """``get_snapshot`` returns ``None`` for a non-existent conversation."""
    assert store.get_snapshot("conv_does_not_exist") is None


def test_get_state_empty_for_unknown(
    store: SqlAlchemySessionStateStore,
) -> None:
    """``get_state`` / ``get_usage`` return ``{}`` for a non-existent conversation."""
    assert store.get_state("conv_does_not_exist") == {}
    assert store.get_usage("conv_does_not_exist") == {}


def test_set_state_is_noop_for_unknown(
    store: SqlAlchemySessionStateStore,
) -> None:
    """``set_state`` on an unknown conversation matches zero rows (no error, no row)."""
    store.set_state("conv_does_not_exist", {"x": 1})
    assert store.get_snapshot("conv_does_not_exist") is None


def test_token_usage_typeddict_shape_and_default(
    store: SqlAlchemySessionStateStore,
    conversation_id: str,
) -> None:
    """BDP-2358: ``SessionStateSnapshot.usage`` is the ``TokenUsage`` TypedDict.

    It is a plain dict at runtime (no behavior change) — defaults to ``{}`` and
    round-trips a usage blob unchanged — while documenting the token-usage shape.
    """
    import typing

    from bytedesk_omnigent.session_state_store import TokenUsage

    # All keys optional (total=False) so an empty blob and a partial blob both fit.
    assert TokenUsage.__required_keys__ == frozenset()
    assert {"input_tokens", "output_tokens", "total_tokens", "by_model"} <= set(
        typing.get_type_hints(TokenUsage)
    )

    # Default snapshot usage is an empty dict (runtime-identical to before).
    assert SessionStateSnapshot(conversation_id="c1").usage == {}

    # A real usage blob round-trips byte-for-byte through the store facade.
    blob = {"input_tokens": 1500, "output_tokens": 350, "total_tokens": 1850}
    store.set_usage(conversation_id, blob)
    snap = store.get_snapshot(conversation_id)
    assert snap is not None
    assert snap.usage == blob
