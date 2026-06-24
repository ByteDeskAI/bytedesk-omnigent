"""Edge-case coverage for :mod:`omnigent.runtime.policies.engine` helpers."""

from __future__ import annotations

import pytest

from omnigent.runtime.policies.engine import (
    PolicyEngine,
    _apply_one,
    _condition_matches,
    _fail_closed,
    _monotonic_ok,
)
from omnigent.spec.types import (
    FunctionPolicySpec,
    FunctionRef,
    LabelDef,
    PolicyAction,
    StateUpdate,
    StateUpdateAction,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


def test_apply_one_increment_delete_and_append_type_error() -> None:
    """State-update ops cover INCREMENT, DELETE, and APPEND validation."""
    state: dict[str, object] = {"count": 2, "tags": ["a"], "temp": 1}
    _apply_one(state, StateUpdate(key="count", action=StateUpdateAction.INCREMENT, value=3))
    assert state["count"] == 5
    _apply_one(state, StateUpdate(key="temp", action=StateUpdateAction.DELETE, value=None))
    assert "temp" not in state
    _apply_one(state, StateUpdate(key="new_list", action=StateUpdateAction.APPEND, value="x"))
    assert state["new_list"] == ["x"]
    _apply_one(state, StateUpdate(key="tags", action=StateUpdateAction.APPEND, value="b"))
    assert state["tags"] == ["a", "b"]
    with pytest.raises(TypeError, match="expected list"):
        _apply_one(state, StateUpdate(key="count", action=StateUpdateAction.APPEND, value="bad"))


def test_record_user_daily_ask_approved_noops_on_bad_values(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> None:
    """Invalid or missing approval values are ignored."""
    conv = conversation_store.create_conversation()
    engine = PolicyEngine(
        policies=[],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        initial_user_daily_cost={"cost_usd": 1.0, "ask_approved_usd": 0.0},
        conversation_store=conversation_store,
    )
    engine._record_user_daily_ask_approved(None)
    engine._record_user_daily_ask_approved("not-a-number")
    assert engine._user_daily_cost["ask_approved_usd"] == 0.0


def test_record_user_daily_ask_approved_noops_without_owner(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Single-user sessions without an owner grant do not persist daily approvals."""
    conv = conversation_store.create_conversation()
    engine = PolicyEngine(
        policies=[],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        initial_user_daily_cost={"cost_usd": 1.0, "ask_approved_usd": 0.0},
        conversation_store=conversation_store,
    )
    engine._record_user_daily_ask_approved(0.05)
    assert engine._user_daily_cost["ask_approved_usd"] == 0.0


def test_record_user_daily_ask_approved_updates_snapshot(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> None:
    """A valid approval refreshes the in-memory daily-cost snapshot."""
    conv = conversation_store.create_conversation()
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user("daily@example.com")
    perms.grant("daily@example.com", conv.id, 4)
    engine = PolicyEngine(
        policies=[],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        initial_user_daily_cost={"cost_usd": 1.0, "ask_approved_usd": 0.0},
        conversation_store=conversation_store,
    )
    engine._record_user_daily_ask_approved(0.07)
    assert engine._user_daily_cost["ask_approved_usd"] == pytest.approx(0.07)


def test_fail_closed_ask_only_returns_ask() -> None:
    """Approval-gate specs park for ASK when evaluation fails."""
    spec = FunctionPolicySpec(
        name="gate",
        on=None,
        action=[PolicyAction.ASK],
        function=FunctionRef(path="tests.runtime.policies.conftest._always_allow"),
    )
    result = _fail_closed(spec, reason="boom")
    assert result.action == PolicyAction.ASK
    assert result.reason == "boom"


def test_monotonic_ok_unknown_direction_rejects() -> None:
    """Unknown monotonic directions fail closed at runtime."""
    ldef = LabelDef(values=["a", "b"], monotonic="sideways")  # type: ignore[arg-type]
    assert _monotonic_ok(ldef, "a", "b") is False


def test_condition_matches_list_or_rejects_missing_value() -> None:
    """List conditions are OR checks; missing labels never match."""
    labels = {"tier": "gold"}
    assert _condition_matches({"tier": ["gold", "silver"]}, labels) is True
    assert _condition_matches({"tier": ["silver", "bronze"]}, labels) is False
    assert _condition_matches({"missing": "x"}, labels) is False