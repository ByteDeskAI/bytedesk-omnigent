"""Edge-case coverage for :mod:`omnigent.policies.builtins.risk_score`."""

from __future__ import annotations

from omnigent.policies.builtins.risk_score import (
    _collect_labels,
    _parse_result_payload,
    risk_score_policy,
)
from omnigent.policies.schema import PolicyEvent
from tests.policies.builtins.helpers import tool_call_event as tc
from tests.policies.builtins.helpers import tool_result_event as tr


def test_parse_result_payload_returns_plain_string_when_not_json() -> None:
    """Non-JSON string results are returned as-is."""
    assert _parse_result_payload({"result": "plain-text-output"}) == "plain-text-output"


def test_collect_labels_stops_at_zero_depth() -> None:
    """Depth budget of zero returns no labels."""
    payload = {"label_classification": "internal"}
    assert _collect_labels(payload, ("label_classification",), 0) == set()


def test_collect_labels_walks_list_payloads() -> None:
    """Labels nested inside lists are collected."""
    payload = [{"label_classification": "Highly Confidential"}]
    labels = _collect_labels(payload, ("label_classification",), 5)
    assert labels == {"highly confidential"}


def test_sensitive_label_in_list_increments_score() -> None:
    """A label inside a list in the tool result accrues risk."""
    import json

    policy = risk_score_policy(sensitive_labels={"highly confidential": 15})
    result = policy(
        tr(
            "fetch",
            json.dumps([{"label_classification": "Highly Confidential"}]),
        )
    )
    assert result is not None
    assert result["state_updates"][0]["value"] == 15


def test_tool_call_non_dict_data_abstains() -> None:
    """Non-dict ``data`` on tool_call abstains."""
    policy = risk_score_policy(tool_points={"web_search": 10})
    event: PolicyEvent = {
        "type": "tool_call",
        "target": "web_search",
        "data": "bad",
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }
    assert policy(event) is None


def test_tool_call_missing_name_abstains() -> None:
    """Tool call with empty ``name`` abstains."""
    policy = risk_score_policy(tool_points={"web_search": 10})
    event: PolicyEvent = {
        "type": "tool_call",
        "target": "",
        "data": {"name": "", "arguments": {}},
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }
    assert policy(event) is None