"""Edge-case coverage for :mod:`omnigent.policies.builtins.safety`.

Exercises branches not hit by the main safety / PII / enforce_sandbox suites:
rate-limit factory, malformed event shapes, and multimodal request scanning.
"""

from __future__ import annotations

from omnigent.policies.builtins.safety import (
    ask_on_add_policy,
    ask_on_os_tools,
    block_skills,
    deny_pii_in_llm_request,
    enforce_sandbox,
    max_tool_calls_per_session,
)
from omnigent.policies.schema import PolicyEvent
from tests.policies.builtins.helpers import tool_call_event as tc


# ── max_tool_calls_per_session ───────────────────────────────────────────────


def test_max_tool_calls_ignores_non_tool_call_events() -> None:
    """Non-``tool_call`` phases pass through without touching session state."""
    policy = max_tool_calls_per_session(limit=1)
    event: PolicyEvent = {
        "type": "request",
        "target": None,
        "data": "hello",
        "context": {"actor": {}, "usage": {}},
        "session_state": {"_policy_tool_call_count": 99},
    }
    assert policy(event) == {"result": "ALLOW"}


def test_max_tool_calls_allows_under_limit_with_increment() -> None:
    """Under the limit, ALLOW includes an increment state update."""
    policy = max_tool_calls_per_session(limit=3)
    result = policy(tc("web_search", {"query": "test"}, session_state={"_policy_tool_call_count": 1}))
    assert result["result"] == "ALLOW"
    assert result["state_updates"] == [
        {"key": "_policy_tool_call_count", "action": "increment", "value": 1},
    ]


def test_max_tool_calls_denies_at_limit() -> None:
    """At or above the limit, further tool calls are denied."""
    policy = max_tool_calls_per_session(limit=2)
    result = policy(tc("web_search", {"query": "test"}, session_state={"_policy_tool_call_count": 2}))
    assert result["result"] == "DENY"
    assert "Exceeded 2 tool calls" in result["reason"]


def test_max_tool_calls_treats_missing_counter_as_zero() -> None:
    """Missing ``_policy_tool_call_count`` starts from zero."""
    policy = max_tool_calls_per_session(limit=1)
    result = policy(tc("web_search", {"query": "test"}))
    assert result["result"] == "ALLOW"
    assert "state_updates" in result


# ── Malformed tool_call data shapes ──────────────────────────────────────────


def test_ask_on_os_tools_allows_non_dict_data() -> None:
    """Non-dict ``data`` on a tool_call is treated as non-OS and allowed."""
    event: PolicyEvent = {
        "type": "tool_call",
        "target": "sys_os_read",
        "data": "not-a-dict",
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }
    assert ask_on_os_tools(event) == {"result": "ALLOW"}


def test_ask_on_add_policy_allows_non_dict_data() -> None:
    """Non-dict ``data`` on ``sys_add_policy`` tool_call is allowed."""
    event: PolicyEvent = {
        "type": "tool_call",
        "target": "sys_add_policy",
        "data": None,
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }
    assert ask_on_add_policy(event) == {"result": "ALLOW"}


def test_block_skills_allows_non_dict_data() -> None:
    """Non-dict ``data`` on tool_call passes through."""
    policy = block_skills(blocked=["deploy"])
    event: PolicyEvent = {
        "type": "tool_call",
        "target": "load_skill",
        "data": ["unexpected"],
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }
    assert policy(event) == {"result": "ALLOW"}


def test_block_skills_allows_non_dict_arguments() -> None:
    """Non-dict ``arguments`` on tool_call passes through."""
    policy = block_skills(blocked=["deploy"])
    event: PolicyEvent = {
        "type": "tool_call",
        "target": "load_skill",
        "data": {"name": "load_skill", "arguments": "bad"},
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }
    assert policy(event) == {"result": "ALLOW"}


def test_enforce_sandbox_allows_non_dict_data() -> None:
    """Non-dict ``data`` on tool_call passes through unchanged."""
    policy = enforce_sandbox()
    event: PolicyEvent = {
        "type": "tool_call",
        "target": "sys_agent_start",
        "data": 42,
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }
    result = policy(event)
    assert result["result"] == "ALLOW"
    assert "data" not in result


# ── deny_pii_in_llm_request: multimodal + malformed llm_request ──────────────


def test_deny_pii_scans_multimodal_request_blocks() -> None:
    """PII embedded in multimodal content-block lists is denied."""
    policy = deny_pii_in_llm_request()
    event: PolicyEvent = {
        "type": "request",
        "target": None,
        "data": [
            {"type": "text", "text": "benign intro"},
            {"type": "text", "text": "contact alice@example.com please"},
        ],
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }
    result = policy(event)
    assert result["result"] == "DENY"
    assert "email" in result["reason"].lower()


def test_deny_pii_allows_clean_multimodal_request() -> None:
    """Clean multimodal blocks with no PII pass through."""
    policy = deny_pii_in_llm_request()
    event: PolicyEvent = {
        "type": "request",
        "target": None,
        "data": [
            {"type": "text", "text": "hello"},
            {"type": "image", "url": "https://example.com/img.png"},
        ],
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }
    assert policy(event) == {"result": "ALLOW"}


def test_deny_pii_allows_non_dict_llm_request_data() -> None:
    """Non-dict ``data`` on ``llm_request`` is allowed."""
    policy = deny_pii_in_llm_request()
    event: PolicyEvent = {
        "type": "llm_request",
        "target": None,
        "data": "not-a-dict",
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }
    assert policy(event) == {"result": "ALLOW"}