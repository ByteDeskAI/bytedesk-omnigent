"""Edge-case coverage for :mod:`omnigent.policies.function`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from omnigent.policies.function import (
    FunctionPolicy,
    _build_event,
    _callable_arity,
    _coerce_state_updates,
    _coerce_to_policy_result,
    _policy_result_from_dict,
    make_fixed_action_callable,
)
from omnigent.policies.types import EvaluationContext, PolicyResult
from omnigent.spec.types import (
    FunctionPolicySpec,
    Phase,
    PolicyAction,
    StateUpdate,
    StateUpdateAction,
)


def test_build_event_includes_request_data_on_tool_result() -> None:
    """TOOL_RESULT events carry the original tool-call payload."""
    ctx = EvaluationContext(
        phase=Phase.TOOL_RESULT,
        content='{"ok": true}',
        tool_name="grep",
        request_data={"tool": "grep", "args": {"pattern": "x"}},
    )
    event = _build_event(ctx)
    assert event["request_data"] == {"tool": "grep", "args": {"pattern": "x"}}


def test_callable_arity_defaults_to_one_when_introspection_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _policy(event: dict[str, Any]) -> dict[str, Any]:
        del event
        return {"result": "allow"}

    def _raise(_fn: Any) -> Any:
        raise TypeError("no signature")

    monkeypatch.setattr("omnigent.policies.function.inspect.signature", _raise)
    assert _callable_arity(_policy) == 1


def test_make_fixed_action_callable_abstains_on_non_matching_phase() -> None:
    fixed = make_fixed_action_callable(action="deny", on_phases=["tool_call"])
    assert fixed({"type": "request", "target": None, "data": "hi"}) is None


def test_coerce_to_policy_result_returns_policy_result_unchanged() -> None:
    original = PolicyResult(action=PolicyAction.DENY, reason="blocked")
    assert _coerce_to_policy_result(original, spec_name="p") is original


def test_coerce_to_policy_result_accepts_foreign_policy_result_shape() -> None:
    @dataclass
    class ForeignResult:
        action: PolicyAction
        reason: str | None = None
        set_labels: dict[str, str] | None = None
        state_updates: list[StateUpdate] | None = None

    foreign = ForeignResult(
        action=PolicyAction.ASK,
        reason="confirm",
        set_labels={"risk": "high"},
        state_updates=[StateUpdate(key="n", action=StateUpdateAction.SET, value=1)],
    )
    result = _coerce_to_policy_result(foreign, spec_name="foreign")
    assert result.action == PolicyAction.ASK
    assert result.reason == "confirm"
    assert result.set_labels == {"risk": "high"}
    assert result.state_updates == [
        StateUpdate(key="n", action=StateUpdateAction.SET, value=1),
    ]


def test_coerce_to_policy_result_rejects_foreign_invalid_action() -> None:
    class BadForeign:
        action = "maybe"

    with pytest.raises(ValueError, match=r"invalid action"):
        _coerce_to_policy_result(BadForeign(), spec_name="bad")


def test_coerce_to_policy_result_rejects_unsupported_type() -> None:
    with pytest.raises(TypeError, match=r"unsupported type"):
        _coerce_to_policy_result(42, spec_name="bad")


def test_policy_result_from_dict_accepts_policy_action_enum() -> None:
    result = _policy_result_from_dict(
        {"result": PolicyAction.DENY, "reason": "nope"},
        spec_name="enum",
    )
    assert result.action == PolicyAction.DENY


def test_policy_result_from_dict_requires_result_key() -> None:
    with pytest.raises(ValueError, match=r"missing 'result' key"):
        _policy_result_from_dict({"reason": "x"}, spec_name="missing")


def test_policy_result_from_dict_rejects_invalid_decision() -> None:
    with pytest.raises(ValueError, match=r"invalid decision result"):
        _policy_result_from_dict({"result": "maybe"}, spec_name="invalid")


def test_coerce_state_updates_passthrough_existing_state_update_objects() -> None:
    existing = StateUpdate(key="k", action=StateUpdateAction.SET, value=1)
    assert _coerce_state_updates([existing], spec_name="p") == [existing]


def test_coerce_state_updates_rejects_non_dict_entries() -> None:
    with pytest.raises(TypeError, match=r"must be a dict"):
        _coerce_state_updates(["bad"], spec_name="p")


def test_coerce_state_updates_requires_key_and_action() -> None:
    with pytest.raises(ValueError, match=r"missing required 'key' or 'action'"):
        _coerce_state_updates([{"key": "only_key"}], spec_name="p")


def test_coerce_state_updates_rejects_invalid_action_string() -> None:
    with pytest.raises(ValueError, match=r"invalid state_updates action"):
        _coerce_state_updates(
            [{"key": "k", "action": "explode"}],
            spec_name="p",
        )


def test_coerce_state_updates_returns_none_for_unsupported_raw_type() -> None:
    assert _coerce_state_updates("not-a-collection", spec_name="p") is None


@pytest.mark.asyncio
async def test_function_policy_evaluate_coerces_dict_with_state_updates() -> None:
    def _policy(event: dict[str, Any]) -> dict[str, Any]:
        del event
        return {
            "result": "allow",
            "state_updates": [{"key": "count", "action": "increment", "value": 1}],
        }

    policy = FunctionPolicy(FunctionPolicySpec(name="counter", on=None), _policy)
    result = await policy.evaluate(
        EvaluationContext(phase=Phase.REQUEST, content="hello"),
        {},
    )
    assert result.action == PolicyAction.ALLOW
    assert result.state_updates == [
        StateUpdate(key="count", action=StateUpdateAction.INCREMENT, value=1),
    ]