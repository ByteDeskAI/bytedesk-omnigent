"""Edge-case coverage for :mod:`omnigent.runtime.policies.builder`."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from omnigent.entities import Conversation
from omnigent.runtime.policies import builder as builder_mod
from omnigent.runtime.policies.builder import (
    _build_noop_engine,
    _build_policy_llm_client,
    _instantiate_policy,
    _load_tree_conversations,
    _load_user_daily_cost,
    _merge_by_model,
    _resolve_session_owner_cached,
    _subtree_conversation_ids,
    load_session_usage,
)
from omnigent.spec.types import FunctionPolicySpec, FunctionRef, LLMConfig, PolicySpec
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


@pytest.fixture(autouse=True)
def _clear_session_owner_cache() -> None:
    """Isolate owner-cache tests from each other."""
    builder_mod._SESSION_OWNER_CACHE.clear()
    yield  # type: ignore[misc]
    builder_mod._SESSION_OWNER_CACHE.clear()


def test_resolve_session_owner_cached_hits_cache(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> None:
    """The owner cache avoids repeat store lookups for the same session."""
    conv = conversation_store.create_conversation()
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user("owner@example.com")
    perms.grant("owner@example.com", conv.id, 4)

    first = _resolve_session_owner_cached(conv.id, conversation_store)
    store = MagicMock(wraps=conversation_store)
    second = _resolve_session_owner_cached(conv.id, store)
    assert first == "owner@example.com"
    assert second == "owner@example.com"
    store.get_session_owner.assert_not_called()


def test_load_user_daily_cost_without_owner_returns_zeros(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Single-user sessions without an owner grant seed zero daily cost."""
    conv = conversation_store.create_conversation()
    state = _load_user_daily_cost(conv.id, conversation_store)
    assert state == {"cost_usd": 0.0, "ask_approved_usd": 0.0}


def test_load_user_daily_cost_includes_owner(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> None:
    """Daily-cost seed includes the session owner when one is granted."""
    conv = conversation_store.create_conversation()
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user("daily@example.com")
    perms.grant("daily@example.com", conv.id, 4)

    state = _load_user_daily_cost(conv.id, conversation_store)
    assert state["user_id"] == "daily@example.com"
    assert "cost_usd" in state
    assert "ask_approved_usd" in state


def test_build_policy_llm_client_prefixes_databricks_model() -> None:
    """Databricks model ids gain the ``databricks/`` provider prefix."""
    client = _build_policy_llm_client(
        LLMConfig(model="databricks-claude-sonnet-4-6"),
        connection={"base_url": "https://example/serving-endpoints", "api_key": "tok"},
    )
    assert client is not None
    assert client._model == "databricks/databricks-claude-sonnet-4-6"


def test_instantiate_policy_rejects_unknown_spec_type() -> None:
    """Only :class:`FunctionPolicySpec` is instantiable today."""
    unknown = PolicySpec(name="unknown", on=None)
    with pytest.raises(NotImplementedError, match="not a known subclass"):
        _instantiate_policy(unknown, agent_llm=None)


def test_build_noop_engine_seeds_existing_state(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """The noop builder still reads persisted labels and session state."""
    conv = conversation_store.create_conversation()
    conversation_store.set_labels(conv.id, {"risk": "low"})
    conversation_store.set_session_state(conv.id, {"calls": 2})

    engine = _build_noop_engine(
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    assert engine.labels["risk"] == "low"
    assert engine.session_state["calls"] == 2


def test_merge_by_model_skips_non_dict_buckets() -> None:
    """Malformed per-model buckets are ignored instead of crashing."""
    aggregate: dict[str, dict[str, float]] = {}
    _merge_by_model(aggregate, {"good": {"input_tokens": 10}, "bad": "nope"})
    assert aggregate == {"good": {"input_tokens": 10.0}}


def test_load_session_usage_missing_conversation_returns_empty(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Unknown conversation ids yield an empty usage dict."""
    assert load_session_usage("conv_does_not_exist", conversation_store) == {}


def test_load_session_usage_skips_outside_subtree(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Only the requested subtree contributes to the usage sum."""
    root = conversation_store.create_conversation()
    child = conversation_store.create_conversation(parent_conversation_id=root.id)
    sibling = conversation_store.create_conversation(parent_conversation_id=root.id)
    conversation_store.set_session_usage(
        child.id,
        {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
    )
    conversation_store.set_session_usage(
        sibling.id,
        {"input_tokens": 999, "output_tokens": 999, "total_tokens": 1998},
    )

    usage = load_session_usage(child.id, conversation_store)
    assert usage.get("input_tokens") == 100
    assert usage.get("output_tokens") == 50


def test_load_tree_conversations_paginates(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Tree loading follows store pagination until ``has_more`` is false."""
    root = conversation_store.create_conversation()
    first = Conversation(
        id="conv_page_1",
        created_at=0,
        updated_at=0,
        root_conversation_id=root.root_conversation_id,
    )
    second = Conversation(
        id="conv_page_2",
        created_at=0,
        updated_at=0,
        root_conversation_id=root.root_conversation_id,
    )
    page_one = MagicMock()
    page_one.data = [first]
    page_one.has_more = True
    page_one.last_id = "conv_page_1"
    page_two = MagicMock()
    page_two.data = [second]
    page_two.has_more = False
    page_two.last_id = "conv_page_2"

    store = MagicMock()
    store.list_conversations.side_effect = [page_one, page_two]
    tree = _load_tree_conversations(root.root_conversation_id, store)
    assert [c.id for c in tree] == ["conv_page_1", "conv_page_2"]


def test_subtree_conversation_ids_skips_revisited_nodes() -> None:
    """Cycle-safe walk ignores nodes already collected."""
    tree = [
        Conversation(
            id="root",
            created_at=0,
            updated_at=0,
            root_conversation_id="root",
        ),
        Conversation(
            id="child",
            created_at=0,
            updated_at=0,
            root_conversation_id="root",
            parent_conversation_id="root",
        ),
        Conversation(
            id="root",
            created_at=0,
            updated_at=0,
            root_conversation_id="root",
            parent_conversation_id="child",
        ),
    ]
    ids = _subtree_conversation_ids(tree, "root")
    assert ids == {"root", "child"}