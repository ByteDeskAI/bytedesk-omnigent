"""Tests for the spawn-breadth governor policy (BDP-2272, ADR-0142)."""
from __future__ import annotations

from omnigent.policies.builtins.spawn_governor import (
    POLICY_REGISTRY,
    spawn_breadth_governor,
)


def _spawn_event(count: int) -> dict:
    return {
        "type": "tool_call",
        "data": {"name": "sys_session_create"},
        "session_state": {"_policy_spawn_count": count},
    }


def test_governor_allows_under_limit_then_denies_at_limit() -> None:
    gov = spawn_breadth_governor(max_spawns=2)

    first = gov(_spawn_event(0))
    assert first["result"] == "ALLOW"
    # ALLOW increments the per-session spawn counter.
    assert first["state_updates"][0]["key"] == "_policy_spawn_count"
    assert first["state_updates"][0]["action"] == "increment"

    assert gov(_spawn_event(1))["result"] == "ALLOW"

    # At the limit, the next spawn is denied.
    denied = gov(_spawn_event(2))
    assert denied["result"] == "DENY"
    assert "spawn-breadth governor" in denied["reason"]


def test_governor_ignores_non_spawn_tool_calls() -> None:
    gov = spawn_breadth_governor(max_spawns=1)
    # A non-spawn tool, even with a high counter, is never governed by this policy.
    event = {
        "type": "tool_call",
        "data": {"name": "sys_os_shell"},
        "session_state": {"_policy_spawn_count": 99},
    }
    assert gov(event)["result"] == "ALLOW"
    assert "state_updates" not in gov(event)  # no spawn -> no counter bump


def test_governor_ignores_non_tool_call_phases() -> None:
    gov = spawn_breadth_governor(max_spawns=0)
    assert gov({"type": "request", "session_state": {}})["result"] == "ALLOW"


def test_governor_matches_mcp_prefixed_spawn_tool() -> None:
    gov = spawn_breadth_governor(max_spawns=0)
    event = {
        "type": "tool_call",
        "data": {"name": "mcp__omnigent__sys_session_create"},
        "session_state": {"_policy_spawn_count": 0},
    }
    assert gov(event)["result"] == "DENY"  # prefixed form still matched + governed


def test_registry_entry_is_a_well_formed_factory() -> None:
    entry = POLICY_REGISTRY[0]
    assert entry["kind"] == "factory"
    assert entry["handler"].endswith("spawn_breadth_governor")
    assert entry["params_schema"]["properties"]["max_spawns"]["default"] == 16
