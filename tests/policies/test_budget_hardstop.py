"""Tests for the hard-stop budget circuit breaker (BDP-2271, ADR-0142)."""
from __future__ import annotations

from bytedesk_omnigent.policies.budget import POLICY_REGISTRY, cost_hard_stop


def _event(spent: float, phase: str = "request", model: str = "opus") -> dict:
    return {
        "type": phase,
        "context": {"usage": {"total_cost_usd": spent}, "model": model},
    }


def test_hard_stop_allows_under_ceiling_denies_at_or_above() -> None:
    breaker = cost_hard_stop(max_cost_usd=5.0)
    assert breaker(_event(0.0))["result"] == "ALLOW"
    assert breaker(_event(4.99))["result"] == "ALLOW"
    # At and above the ceiling: unconditional DENY.
    assert breaker(_event(5.0))["result"] == "DENY"
    over = breaker(_event(7.5))
    assert over["result"] == "DENY"
    assert "hard budget ceiling" in over["reason"]


def test_hard_stop_denies_regardless_of_model_unlike_downgrade_gate() -> None:
    breaker = cost_hard_stop(max_cost_usd=1.0)
    # Even on a cheap model — no downgrade escape (this is the difference from cost_budget).
    assert breaker(_event(2.0, model="haiku"))["result"] == "DENY"


def test_hard_stop_gates_both_request_and_tool_call_phases() -> None:
    breaker = cost_hard_stop(max_cost_usd=1.0)
    assert breaker(_event(2.0, phase="request"))["result"] == "DENY"
    assert breaker(_event(2.0, phase="tool_call"))["result"] == "DENY"


def test_hard_stop_ignores_other_phases_and_unpriced_turns() -> None:
    breaker = cost_hard_stop(max_cost_usd=0.01)
    # A non-budgeted phase passes even over budget.
    assert breaker(_event(99.0, phase="response"))["result"] == "ALLOW"
    # Missing usage -> 0.0 spent -> cannot trip what it cannot price.
    assert breaker({"type": "request"})["result"] == "ALLOW"


def test_registry_entry_is_a_well_formed_factory() -> None:
    entry = POLICY_REGISTRY[0]
    assert entry["kind"] == "factory"
    assert entry["handler"].endswith("cost_hard_stop")
    assert "max_cost_usd" in entry["params_schema"]["properties"]
