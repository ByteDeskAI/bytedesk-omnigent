"""Tests for the two-key approval gate (BDP-2277 F2, ADR-0142)."""
from __future__ import annotations

from bytedesk_omnigent.policies.two_key import _APPROVERS_KEY, two_key_required


def _tool_call(name: str, state: dict | None = None) -> dict:
    return {"type": "tool_call", "data": {"name": name}, "session_state": state or {}}


def test_asks_when_no_approvers_recorded() -> None:
    evaluate = two_key_required(["deploy\\.run"])
    result = evaluate(_tool_call("deploy.run"))
    assert result["result"] == "ASK"
    assert "2 distinct" in result["reason"]


def test_allows_when_min_distinct_approvers_present() -> None:
    evaluate = two_key_required(["deploy\\.run"], min_approvers=2)
    state = {_APPROVERS_KEY: ["alice@x.com", "bob@x.com"]}
    assert evaluate(_tool_call("deploy.run", state))["result"] == "ALLOW"


def test_asks_when_one_distinct_approver_even_if_duplicated() -> None:
    # The same approver signing twice is NOT two keys.
    evaluate = two_key_required(["deploy\\.run"], min_approvers=2)
    state = {_APPROVERS_KEY: ["alice@x.com", "alice@x.com"]}
    assert evaluate(_tool_call("deploy.run", state))["result"] == "ASK"


def test_allows_non_matching_tool() -> None:
    evaluate = two_key_required(["deploy\\.run"])
    assert evaluate(_tool_call("read.file"))["result"] == "ALLOW"


def test_allows_non_tool_call_events() -> None:
    evaluate = two_key_required(["deploy\\.run"])
    assert evaluate({"type": "llm_call"})["result"] == "ALLOW"
