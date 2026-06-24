"""Edge-case coverage for malformed usage fields in :mod:`omnigent.policies.builtins.cost`."""

from __future__ import annotations

from omnigent.policies.builtins.cost import cost_budget, user_daily_cost_budget
from omnigent.policies.schema import PolicyEvent


def _tool_with_usage(usage: dict) -> PolicyEvent:
    return {
        "type": "tool_call",
        "target": "sys_os_shell",
        "data": {"name": "sys_os_shell", "arguments": {}},
        "context": {"actor": {}, "usage": usage, "model": "databricks-claude-opus-4-8"},
        "session_state": {},
    }


def _daily_tool(daily: dict) -> PolicyEvent:
    return {
        "type": "tool_call",
        "target": "sys_os_shell",
        "data": {"name": "sys_os_shell", "arguments": {}},
        "context": {
            "actor": {},
            "usage": {},
            "model": "databricks-claude-opus-4-8",
            "user_daily_cost": daily,
        },
        "session_state": {},
    }


def test_malformed_session_cost_treated_as_zero() -> None:
    """Non-numeric ``total_cost_usd`` is treated as 0.0 (never blocks)."""
    policy = cost_budget(max_cost_usd=1.0)
    result = policy(_tool_with_usage({"total_cost_usd": "not-a-number"}))
    assert result == {"result": "ALLOW"}


def test_malformed_daily_cost_treated_as_zero() -> None:
    """Non-numeric ``cost_usd`` in daily rollup is treated as 0.0."""
    policy = user_daily_cost_budget(max_cost_usd=1.0)
    result = policy(_daily_tool({"cost_usd": "bad", "ask_approved_usd": 0.0}))
    assert result == {"result": "ALLOW"}


def test_malformed_daily_ask_approved_treated_as_zero() -> None:
    """Non-numeric ``ask_approved_usd`` is treated as 0.0."""
    policy = user_daily_cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    result = policy(_daily_tool({"cost_usd": 3.0, "ask_approved_usd": "nope"}))
    assert result["result"] == "ASK"