"""Tests for the forever-gated tool registry policy (BDP-2271 F7, ADR-0142)."""
from __future__ import annotations

from bytedesk_omnigent.policies.forever_gate import POLICY_REGISTRY, forever_denied


def _call(name: str) -> dict:
    return {"type": "tool_call", "data": {"name": name}}


def test_forever_denied_blocks_matching_tools() -> None:
    gate = forever_denied(
        ["promote\\.production", "deploy\\.run", "billing\\.(refund|charge)"]
    )
    denied = gate(_call("promote.production"))
    assert denied["result"] == "DENY"
    assert "forever-denied" in denied["reason"]
    assert gate(_call("deploy.run"))["result"] == "DENY"
    assert gate(_call("billing.refund"))["result"] == "DENY"
    assert gate(_call("billing.charge"))["result"] == "DENY"


def test_forever_denied_allows_non_matching_tools() -> None:
    gate = forever_denied(["promote\\.production", "deploy\\.run"])
    # A normal write tool not on the deny-list passes (it's governed elsewhere).
    assert gate(_call("sales.opportunity_advance"))["result"] == "ALLOW"
    assert gate(_call("billing.plan_change"))["result"] == "ALLOW"


def test_forever_denied_ignores_non_tool_call_phases() -> None:
    gate = forever_denied(["anything"])
    assert gate({"type": "request"})["result"] == "ALLOW"


def test_registry_entry_is_a_well_formed_factory() -> None:
    entry = POLICY_REGISTRY[0]
    assert entry["kind"] == "factory"
    assert entry["handler"].endswith("forever_denied")
    assert entry["params_schema"]["properties"]["patterns"]["type"] == "array"
