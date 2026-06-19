"""Tests for the delegation-graph authority gate (BDP-2269 C1, ADR-0142)."""
from __future__ import annotations

from omnigent.policies.builtins.delegation import delegation_authority


def _spawn(target_key: str | None = None, target_value: str | None = None) -> dict:
    args = {}
    if target_key is not None:
        args[target_key] = target_value
    return {"type": "tool_call", "data": {"name": "sys_session_create", "arguments": args}}


def test_allows_spawn_of_a_report() -> None:
    # sys_session_create carries the target as agent_id (BDP-2288 #4).
    evaluate = delegation_authority(["ag_dev", "ag_qa"])
    assert evaluate(_spawn("agent_id", "ag_dev"))["result"] == "ALLOW"


def test_denies_spawn_of_a_non_report() -> None:
    evaluate = delegation_authority(["ag_dev", "ag_qa"])
    result = evaluate(_spawn("agent_id", "ag_ceo"))
    assert result["result"] == "DENY"
    assert "ag_ceo" in result["reason"]


def test_phantom_agent_name_arg_does_not_grant_a_non_report() -> None:
    # agent_name is NOT a real sys_session_create arg; keying on it used to make
    # the gate never fire. A non-report passed only as agent_name must NOT slip
    # through as a named ALLOW — with no agent_id it's an unnamed spawn (BDP-2288 #4).
    evaluate = delegation_authority(["ag_dev"])
    # No agent_id → treated as an unnamed spawn, NOT a named bypass.
    assert evaluate(_spawn("agent_name", "ag_ceo"))["result"] == "ALLOW"


def test_denies_config_path_spawn_that_bypasses_the_org_chart() -> None:
    # Launching a brand-new agent from a local bundle bypasses the org chart →
    # DENY (BDP-2288 #3 — this previously fell through to ALLOW).
    evaluate = delegation_authority(["ag_dev"])
    result = evaluate(_spawn("config_path", "/bundles/rogue/config.yaml"))
    assert result["result"] == "DENY"
    assert "org chart" in result["reason"]


def test_allows_when_target_addressed_by_agent_id() -> None:
    evaluate = delegation_authority(["ag_dev"])
    assert evaluate(_spawn("agent_id", "ag_dev"))["result"] == "ALLOW"


def test_allows_unnamed_local_bundle_spawn() -> None:
    # No agent_id and no config_path is out of scope — governed elsewhere.
    evaluate = delegation_authority(["ag_dev"])
    assert evaluate(_spawn())["result"] == "ALLOW"


def test_allows_non_spawn_tool_and_non_tool_events() -> None:
    evaluate = delegation_authority(["ag_dev"])
    other_tool = {"type": "tool_call", "data": {"name": "read.file", "arguments": {}}}
    assert evaluate(other_tool)["result"] == "ALLOW"
    assert evaluate({"type": "llm_call"})["result"] == "ALLOW"
