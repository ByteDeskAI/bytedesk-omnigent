"""Edge-case coverage for :mod:`omnigent.policies.builtins.google`."""

from __future__ import annotations

import json

from omnigent.policies.builtins.google import (
    CREATED_DRAFTS_STATE_KEY,
    CREATED_FILES_STATE_KEY,
    _append_updates,
    _extract_created_ids,
    _normalize_file_ref,
    _parse_result_payload,
    gcalendar_policy,
    gdrive_policy,
    gmail_policy,
)
from tests.policies.builtins.helpers import tool_call_event as tc
from tests.policies.builtins.helpers import tool_result_event as tr

_DOC_ID = "1AbCdefGHIjklMNOpqrSTUvwxyz0123456789"


def test_normalize_file_ref_empty_and_drive_query_url() -> None:
    """Empty refs and drive.google.com ``?id=`` URLs normalize correctly."""
    assert _normalize_file_ref("") == ""
    url = f"https://drive.google.com/open?id={_DOC_ID}"
    assert _normalize_file_ref(url) == _DOC_ID


def test_parse_result_payload_returns_plain_string() -> None:
    """Non-JSON tool results are returned unchanged."""
    assert _parse_result_payload({"result": "plain"}) == "plain"


def test_extract_created_ids_walks_lists_and_respects_depth() -> None:
    """Created IDs inside lists are collected; zero depth returns nothing."""
    payload = [{"documentId": "doc-1"}]
    assert _extract_created_ids(payload, frozenset({"documentId"}), 0) == set()
    assert _extract_created_ids(payload, frozenset({"documentId"}), 2) == {"doc-1"}


def test_append_updates_ignores_non_list_tracked_state() -> None:
    """Tracked state that is not a list is treated as empty."""
    updates = _append_updates({"new-id"}, tracked="not-a-list", state_key="key")
    assert updates == [{"key": "key", "action": "append", "value": "new-id"}]


def test_malformed_tool_call_abstains_for_all_google_policies() -> None:
    """Non-dict tool_call data abstains across Drive, Gmail, and Calendar."""
    bad_event = {"type": "tool_call", "data": "bad", "context": {}, "session_state": {}}
    for factory in (gdrive_policy, gmail_policy, gcalendar_policy):
        assert factory()(bad_event) is None


def test_tool_call_non_string_name_abstains() -> None:
    """Non-string tool names abstain during parse."""
    policy = gdrive_policy()
    event = {"type": "tool_call", "data": {"name": 42, "arguments": {}}, "context": {}, "session_state": {}}
    assert policy(event) is None


def test_gmail_non_tool_call_phase_abstains() -> None:
    """Gmail policy abstains on non-tool_call phases."""
    policy = gmail_policy()
    event = {"type": "request", "data": "hello", "context": {}, "session_state": {}}
    assert policy(event) is None


def test_gdrive_non_tool_phase_abstains() -> None:
    """Drive policy abstains on phases other than tool_call / tool_result."""
    policy = gdrive_policy()
    event = {"type": "response", "data": "done", "context": {}, "session_state": {}}
    assert policy(event) is None


def test_gcalendar_non_tool_call_phase_abstains() -> None:
    """Calendar policy only gates tool_call events."""
    policy = gcalendar_policy()
    event = {"type": "tool_result", "target": "mcp__google__calendar_list", "data": {}}
    assert policy(event) is None


def test_drive_tool_result_non_string_target_abstains() -> None:
    """Drive create tracking abstains when ``target`` is not a string."""
    policy = gdrive_policy()
    event = {
        "type": "tool_result",
        "target": 99,
        "data": {"result": json.dumps({"id": _DOC_ID})},
        "context": {},
        "session_state": {},
    }
    assert policy(event) is None


def test_gmail_tool_result_non_create_tool_abstains() -> None:
    """Draft tracking only runs for draft-create tools."""
    policy = gmail_policy()
    event = tr("mcp__google__gmail_message_get", json.dumps({"id": "draft-1"}))
    assert policy(event) is None


def test_drive_tool_result_non_create_tool_abstains() -> None:
    """File tracking only runs for Drive create tools."""
    policy = gdrive_policy()
    event = tr("mcp__google__drive_file_get", json.dumps({"id": _DOC_ID}))
    assert policy(event) is None


def test_drive_create_result_without_ids_abstains() -> None:
    """Create results with no discoverable IDs produce no state updates."""
    policy = gdrive_policy()
    event = tr("mcp__google__drive_file_create", json.dumps({"status": "ok"}))
    assert policy(event) is None


def test_gmail_draft_create_result_without_ids_abstains() -> None:
    """Draft-create results with no IDs produce no state updates."""
    policy = gmail_policy()
    event = tr("mcp__google__gmail_draft_create", json.dumps({"status": "ok"}))
    assert policy(event) is None


def test_gmail_draft_write_denied_when_drafts_disabled() -> None:
    """Draft edits are denied when ``allow_drafts`` is false."""
    policy = gmail_policy(allow_drafts=False)
    result = policy(tc("mcp__google__gmail_draft_update", {"draft_id": "draft-1"}))
    assert result is not None
    assert result["result"] == "DENY"


def test_gmail_tool_result_non_string_target_abstains() -> None:
    """Draft tracking abstains when ``target`` is not a string."""
    policy = gmail_policy()
    event = {
        "type": "tool_result",
        "target": None,
        "data": {"result": json.dumps({"id": "draft-1"})},
        "context": {},
        "session_state": {},
    }
    assert policy(event) is None


def test_drive_tool_result_skips_already_tracked_ids() -> None:
    """No state updates when every created ID is already tracked."""
    policy = gdrive_policy()
    event = tr(
        "mcp__google__drive_file_create",
        json.dumps({"id": _DOC_ID}),
        session_state={CREATED_FILES_STATE_KEY: [_DOC_ID]},
    )
    assert policy(event) is None


def test_gmail_tool_result_skips_already_tracked_drafts() -> None:
    """No state updates when draft IDs are already in session state."""
    policy = gmail_policy()
    event = tr(
        "mcp__google__gmail_draft_create",
        json.dumps({"id": "draft-1"}),
        session_state={CREATED_DRAFTS_STATE_KEY: ["draft-1"]},
    )
    assert policy(event) is None


def test_gmail_draft_edit_without_target_ids_denies() -> None:
    """Draft edits without identifiable draft IDs are denied."""
    policy = gmail_policy(allow_drafts=True)
    result = policy(tc("mcp__google__gmail_draft_update", {}))
    assert result is not None
    assert result["result"] == "DENY"