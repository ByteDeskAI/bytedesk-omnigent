"""Tests for the verify-as-gate policy (BDP-2276 E3, ADR-0142).

Covers:

- Abstains on non-``tool_result`` events and non-gated tools.
- Denies a gated tool's success result that lacks machine verification
  (``claimed_unverified``).
- Allows a gated tool's result that carries a truthy ``verified`` field.
- An explicit error result is not a success claim — abstain.
- A gated tool with a non-verifiable (non-object) result fails CLOSED.
- A custom ``verified_field`` is honored.
- The registry entry is well-formed.
"""

from __future__ import annotations

import json

from omnigent.policies.builtins.verify_gate import POLICY_REGISTRY, verify_as_gate

from .helpers import tool_result_event

_GATED = ["release_trigger", "drive_delete"]


async def test_abstains_on_non_tool_result_event() -> None:
    gate = verify_as_gate(gated_tools=_GATED)
    event = {"type": "tool_call", "target": "release_trigger", "data": {}}
    assert await gate(event) is None  # type: ignore[arg-type]


async def test_abstains_on_non_gated_tool() -> None:
    gate = verify_as_gate(gated_tools=_GATED)
    event = tool_result_event("memory_append", json.dumps({"memory_id": "m1"}))
    assert await gate(event) is None


async def test_denies_gated_success_without_verification() -> None:
    gate = verify_as_gate(gated_tools=_GATED)
    event = tool_result_event("release_trigger", json.dumps({"status": "ok"}))
    resp = await gate(event)
    assert resp is not None
    assert resp["result"] == "DENY"
    assert "claimed_unverified" in resp["reason"]


async def test_allows_gated_success_with_verification() -> None:
    gate = verify_as_gate(gated_tools=_GATED)
    event = tool_result_event(
        "release_trigger", json.dumps({"status": "ok", "verified": True})
    )
    assert await gate(event) is None


async def test_abstains_on_gated_error_result() -> None:
    # An explicit error is a failure, not an unverified success claim — let the
    # harness surface the error to the agent normally.
    gate = verify_as_gate(gated_tools=_GATED)
    event = tool_result_event("drive_delete", json.dumps({"error": "boom"}))
    assert await gate(event) is None


async def test_denies_gated_non_object_result_fail_closed() -> None:
    # A destructive tool whose result is not a verifiable object cannot be
    # trusted to have done what it claimed — fail closed.
    gate = verify_as_gate(gated_tools=_GATED)
    event = tool_result_event("drive_delete", "deleted!")
    resp = await gate(event)
    assert resp is not None
    assert resp["result"] == "DENY"


async def test_custom_verified_field() -> None:
    gate = verify_as_gate(gated_tools=_GATED, verified_field="confirmed")
    ok = tool_result_event("release_trigger", json.dumps({"confirmed": True}))
    assert await gate(ok) is None
    # The default field name no longer satisfies the gate.
    bad = tool_result_event("release_trigger", json.dumps({"verified": True}))
    resp = await gate(bad)
    assert resp is not None
    assert resp["result"] == "DENY"


def test_registry_entry_is_well_formed() -> None:
    assert len(POLICY_REGISTRY) == 1
    entry = POLICY_REGISTRY[0]
    assert entry["handler"] == "omnigent.policies.builtins.verify_gate.verify_as_gate"
    assert entry["kind"] == "factory"
    assert "gated_tools" in entry["params_schema"]["required"]
