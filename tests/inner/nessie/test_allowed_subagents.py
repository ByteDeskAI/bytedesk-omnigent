"""Tests for the ``allowed_subagents`` nessie policy (BDP-2148, ADR-0133).

Native replacement for OpenClaw ``subagents.allowAgents``: gates
``sys_session_create`` to an explicit agent allow-list; discovery stays global.
"""

from __future__ import annotations

from typing import Any

from omnigent.db.utils import builtin_agent_id
from omnigent.inner.nessie.policies import allowed_subagents


def _tool_call(tool: str, **args: Any) -> dict[str, Any]:
    return {"type": "tool_call", "data": {"name": tool, "arguments": dict(args)}}


def _result(decision: dict[str, Any]) -> str:
    return decision["result"]


def test_allows_listed_slug() -> None:
    evaluate = allowed_subagents(allowed_agents=("platform-developer",))
    assert _result(evaluate(_tool_call("sys_session_create", agent_id="platform-developer"))) == "ALLOW"


def test_allows_deterministic_id_of_listed_slug() -> None:
    # sys_agent_list surfaces the ag_<hash> id; the slug allow-list still matches it.
    evaluate = allowed_subagents(allowed_agents=("platform-developer",))
    aid = builtin_agent_id("platform-developer")
    assert _result(evaluate(_tool_call("sys_session_create", agent_id=aid))) == "ALLOW"


def test_denies_unlisted_agent() -> None:
    evaluate = allowed_subagents(allowed_agents=("platform-developer",))
    assert _result(evaluate(_tool_call("sys_session_create", agent_id="rogue-admin"))) == "DENY"


def test_denies_missing_agent_id_config_path_mode() -> None:
    # A config_path/bundle-file launch bypasses the registry — denied when gated.
    evaluate = allowed_subagents(allowed_agents=("platform-developer",))
    assert _result(evaluate(_tool_call("sys_session_create", config_path="/x/agent.yaml"))) == "DENY"


def test_leaves_sys_session_send_untouched() -> None:
    # Named children on sys_session_send are bundle-local, not registry launches.
    evaluate = allowed_subagents(allowed_agents=("platform-developer",))
    assert _result(evaluate(_tool_call("sys_session_send", agent="researcher"))) == "ALLOW"


def test_ignores_non_tool_call_events() -> None:
    evaluate = allowed_subagents(allowed_agents=("platform-developer",))
    assert _result(evaluate({"type": "message"})) == "ALLOW"


def test_empty_allowlist_denies_all_launches() -> None:
    evaluate = allowed_subagents()
    assert _result(evaluate(_tool_call("sys_session_create", agent_id="anyone"))) == "DENY"
