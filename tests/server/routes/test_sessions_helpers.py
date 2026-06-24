"""Unit tests for pure helper functions in ``sessions.py``.

These helpers are extracted at the module boundary for batch permission
resolution, SSE formatting, usage accounting, and attachment safety. Direct
unit tests lift coverage without standing up the full sessions router.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE,
)
from omnigent.entities import Agent, CommentsFingerprint, Conversation, LoadedAgent, StoredFile
from omnigent.entities.conversation import (
    ConversationItem,
    ErrorData,
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
    NewConversationItem,
)
from omnigent.entities.pagination import PagedList
from omnigent.entities.permission import SessionPermission
from omnigent.server.schemas import ElicitationRequestParams, ElicitationResult, SessionEventInput
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.policies.types import Phase, PolicyAction, PolicyResult
from omnigent.server._elicitation_registry import (
    _ParkedHarnessElicitation,
    _PreResolvedHarnessElicitation,
)
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, LEVEL_READ, RESERVED_USER_PUBLIC
from omnigent.server.routes import sessions as sessions_mod
from omnigent.server.routes.sessions import (
    SessionLiveness,
    _accumulate_session_usage,
    _apply_liveness_to_items,
    _agent_display_names_for,
    _build_session_list_item,
    _build_session_response,
    _parse_external_conversation_item,
    _pending_elicitation_snapshot_for_session,
    _publish_and_persist_resource_event,
    _publish_compaction_completed,
    _publish_compaction_failed,
    _publish_compaction_in_progress,
    _publish_elicitation_request_to_ancestors,
    _publish_external_assistant_message,
    _publish_input_consumed,
    _resolve_llm_model,
    _resolve_output_schema,
    _structured_ask_user_question,
    _validated_harness_override,
    _CLAUDE_NATIVE_SUBAGENT_ID_LABEL_KEY,
    _CLAUDE_NATIVE_WRAPPER_LABEL_KEY,
    _CODEX_NATIVE_SUBAGENT_DISPLAY_FALLBACK,
    _CODEX_NATIVE_SUBAGENT_NICKNAME_LABEL_KEY,
    _CODEX_NATIVE_SUBAGENT_ROLE_LABEL_KEY,
    _CODEX_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE,
    _LAST_TASK_ERROR_CODE_LABEL_KEY,
    _LAST_TASK_ERROR_MESSAGE_LABEL_KEY,
    _SHARED_DISCOVERY_KEY,
    _add_model_usage_delta,
    _allow_all_edits_eligible,
    _ancestor_session_ids,
    _announce_session_added,
    _attachment_disposition,
    _agent_is_native,
    _agent_provider_family,
    _build_actor,
    _build_evaluation_context,
    _build_new_item,
    _build_skill_slash_command_policy_body,
    _descendant_sessions,
    _derive_terminal_launch_args_from_spec,
    _enforce_tenant_scope,
    _find_claude_native_subagent_child,
    _find_subagent_child_by_title,
    _publish_session_created,
    _extract_assistant_text_from_event,
    _extract_user_text_from_event,
    _handle_external_session_todos,
    _hold_native_ask_gate,
    _mcp_error_response,
    _mcp_ok_response,
    _native_ask_gate_lock,
    _parse_skill_slash_command,
    _resilient_stream_payload,
    _spec_config_flag_enabled,
    _spec_harness,
    _client_supplied_hook_elicitation_id,
    _codex_subagent_display_tool,
    _codex_subagent_labels_from_body,
    _coerce_cumulative_field,
    _consume_pre_resolved_harness_elicitation,
    _discovery_key,
    _format_sse,
    _is_codex_native_subagent,
    _is_native_terminal_session,
    _last_task_error_from_labels,
    _latest_message_preview,
    _merge_pending_file_blocks,
    _persist_session_status_error_labels,
    _presentation_labels_for_agent,
    _publish_changed_files_invalidated,
    _publish_error_event,
    _publish_interrupted,
    _publish_policy_deny,
    _publish_runner_recovered_status,
    _publish_runner_skills,
    _publish_sandbox_status,
    _registered_runner_id,
    _same_provider_family,
    _title_content_from_item,
    _message_text,
    _multipart_missing_detail,
    _native_terminal_name_for_harness,
    _native_terminal_runtime,
    _RunnerForwardResult,
    _drive_terminal_resolved_elicitation,
    _forward_approval_to_runner,
    _enrich_idle_status_with_subagent_output,
    _latest_assistant_text_from_store,
    _parse_external_assistant_message,
    _parse_session_create_metadata,
    _persist_external_model_change,
    _persist_external_session_usage,
    _persist_model_change_note,
    _persist_native_cumulative_usage,
    _poll_request_disconnect,
    _publish_and_wait_for_harness_elicitation,
    _publish_subtree_cost_to_ancestors,
    _publish_elicitation_resolved,
    _publish_elicitation_resolved_to_ancestors,
    _schedule_deferred_elicitation_clear,
    _signal_harness_elicitation_resolved_by_id,
    _signal_terminal_resolved_harness_elicitation,
    _spawn_native_approval_popup_forward,
    _publish_external_conversation_item,
    _publish_external_output_text_delta,
    _require_external_status_forward,
    _prune_pre_resolved_harness_elicitations,
    _reject_reserved_cost_control_label_seed,
    _targeted_elicitation_event,
    _validate_terminal_launch_args,
    _validated_cost_control_mode_override,
    _model_usage_bucket,
    _owner_from_grants,
    _parse_last_event_id,
    _permission_level_from_grants,
    _priced_cost_for_display,
    _record_daily_cost,
    _resolve_elicitation,
    _resolve_harness,
    _session_status_from_cache,
    _session_status_with_child_rollup,
    _stored_file_to_resource,
    _usage_by_model_for_display,
    _utc_day,
)


def _request(
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    query_string: bytes = b"",
) -> Request:
    """Build a minimal Starlette :class:`Request` for header/query tests."""
    return Request(
        {
            "type": "http",
            "headers": headers or [],
            "query_string": query_string,
        }
    )


@pytest.fixture(autouse=True)
def _clear_status_cache() -> None:
    """Isolate status-cache helper tests from other suites."""
    sessions_mod._session_status_cache.clear()
    sessions_mod._session_terminal_pending_cache.clear()
    sessions_mod._session_sandbox_status_cache.clear()
    yield
    sessions_mod._session_status_cache.clear()
    sessions_mod._session_terminal_pending_cache.clear()
    sessions_mod._session_sandbox_status_cache.clear()


# ── _allow_all_edits_eligible ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tool", "mode", "expected"),
    [
        ("Edit", "default", True),
        ("Write", "plan", True),
        ("MultiEdit", None, True),
        ("NotebookEdit", "default", True),
        ("ExitPlanMode", "default", True),
        ("Bash", "default", False),
        ("Edit", "acceptEdits", False),
        ("Edit", "bypassPermissions", False),
        ("ExitPlanMode", "acceptEdits", False),
    ],
)
def test_allow_all_edits_eligible(tool: str, mode: str | None, expected: bool) -> None:
    """Edit tools and ExitPlanMode qualify unless mode is already permissive."""
    assert _allow_all_edits_eligible(tool, mode) is expected


# ── _discovery_key ───────────────────────────────────────────────────────────


def test_discovery_key_returns_user_id_when_set() -> None:
    """Authenticated users subscribe on their own channel key."""
    assert _discovery_key("alice@example.com") == "alice@example.com"


def test_discovery_key_returns_shared_key_when_unauthenticated() -> None:
    """Single-user mode uses the shared discovery key."""
    assert _discovery_key(None) == _SHARED_DISCOVERY_KEY


# ── _attachment_disposition ──────────────────────────────────────────────────


def test_attachment_disposition_ascii_filename() -> None:
    """Simple ASCII names appear in both fallback and RFC 5987 parameters."""
    header = _attachment_disposition("report.pdf")
    assert 'filename="report.pdf"' in header
    assert "filename*=UTF-8''report.pdf" in header
    assert header.startswith("attachment; ")


def test_attachment_disposition_strips_quotes_and_non_ascii() -> None:
    """Dangerous and non-ASCII characters are stripped from the ASCII fallback."""
    header = _attachment_disposition('bad"name\n.pdf')
    assert 'filename="badname.pdf"' in header
    assert "filename*=UTF-8''" in header


def test_attachment_disposition_utf8_name() -> None:
    """UTF-8 names are percent-encoded in filename* while fallback is safe."""
    header = _attachment_disposition("résumé.pdf")
    assert 'filename="rsum.pdf"' in header or 'filename="download"' in header
    assert "%C3%A9" in header


def test_attachment_disposition_all_unsafe_chars_falls_back_to_download() -> None:
    """When every character is stripped, the ASCII fallback is ``download``."""
    header = _attachment_disposition('"\n\r')
    assert 'filename="download"' in header


# ── _format_sse ──────────────────────────────────────────────────────────────


def test_format_sse_without_event_id() -> None:
    """Synthetic frames omit the ``id:`` line."""
    out = _format_sse("session.heartbeat", {"ok": True})
    assert out == 'event: session.heartbeat\ndata: {"ok": true}\n\n'


def test_format_sse_with_event_id() -> None:
    """Resume-capable frames include a monotonic ``id:`` line."""
    out = _format_sse("response.output_text.delta", {"delta": "hi"}, event_id=42)
    assert out.startswith("id: 42\n")
    assert "event: response.output_text.delta\n" in out
    payload = json.loads(out.split("data: ", 1)[1].strip())
    assert payload == {"delta": "hi"}


# ── _parse_last_event_id ─────────────────────────────────────────────────────


def test_parse_last_event_id_from_header() -> None:
    """Browsers resend ``Last-Event-ID`` on reconnect."""
    req = _request(headers=[(b"last-event-id", b"17")])
    assert _parse_last_event_id(req) == 17


def test_parse_last_event_id_from_query_param() -> None:
    """Non-EventSource clients may pass ``last_event_id`` as a query param."""
    req = _request(query_string=b"last_event_id=99")
    assert _parse_last_event_id(req) == 99


def test_parse_last_event_id_prefers_header_over_query() -> None:
    """The header wins when both cursor sources are present."""
    req = _request(
        headers=[(b"last-event-id", b"5")],
        query_string=b"last_event_id=9",
    )
    assert _parse_last_event_id(req) == 5


def test_parse_last_event_id_returns_none_for_missing_or_invalid() -> None:
    """Fresh connects and malformed cursors read as no resume."""
    assert _parse_last_event_id(_request()) is None
    assert _parse_last_event_id(_request(query_string=b"last_event_id=abc")) is None


# ── permission grant helpers ─────────────────────────────────────────────────


def _grants(*pairs: tuple[str, int]) -> list[SessionPermission]:
    return [
        SessionPermission(user_id=uid, conversation_id="conv_x", level=level)
        for uid, level in pairs
    ]


def test_permission_level_from_grants_none_user() -> None:
    """Unauthenticated callers have no displayed level."""
    grants = _grants(("alice@test.com", LEVEL_OWNER))
    assert _permission_level_from_grants(None, grants, is_admin=False) is None


def test_permission_level_from_grants_admin_bypass() -> None:
    """Admins always report owner level."""
    grants = _grants((BOB := "bob@test.com", LEVEL_READ))  # noqa: F841
    assert _permission_level_from_grants("admin@test.com", grants, is_admin=True) == LEVEL_OWNER


def test_permission_level_from_grants_user_grant() -> None:
    """A direct user grant wins over public."""
    grants = _grants(
        ("alice@test.com", LEVEL_EDIT),
        (RESERVED_USER_PUBLIC, LEVEL_READ),
    )
    assert _permission_level_from_grants("alice@test.com", grants, is_admin=False) == LEVEL_EDIT


def test_permission_level_from_grants_public_fallback() -> None:
    """Public grant applies when the user has no direct grant."""
    grants = _grants((RESERVED_USER_PUBLIC, LEVEL_READ))
    assert _permission_level_from_grants("stranger@test.com", grants, is_admin=False) == LEVEL_READ


def test_permission_level_from_grants_no_match() -> None:
    """No grant and no public access yields ``None``."""
    grants = _grants(("alice@test.com", LEVEL_OWNER))
    assert _permission_level_from_grants("bob@test.com", grants, is_admin=False) is None


def test_owner_from_grants_returns_first_owner() -> None:
    """Owner is the first grant at or above owner level."""
    grants = _grants(
        ("alice@test.com", LEVEL_EDIT),
        ("bob@test.com", LEVEL_OWNER),
    )
    assert _owner_from_grants(grants) == "bob@test.com"


def test_owner_from_grants_none_when_missing() -> None:
    """No owner-level grant returns ``None``."""
    grants = _grants(("alice@test.com", LEVEL_READ))
    assert _owner_from_grants(grants) is None


# ── session status cache helpers ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("cached", "expected"),
    [
        ("running", "running"),
        ("waiting", "running"),
        ("failed", "failed"),
        ("idle", "idle"),
        (None, "idle"),
    ],
)
def test_session_status_from_cache(cached: str | None, expected: str) -> None:
    """Relay cache values collapse to list-item status."""
    conv_id = "conv_parent"
    if cached is not None:
        sessions_mod._session_status_cache[conv_id] = cached
    assert _session_status_from_cache(conv_id) == expected


def test_session_status_with_child_rollup_parent_running() -> None:
    """Parent running status short-circuits child inspection."""
    parent = "conv_parent"
    sessions_mod._session_status_cache[parent] = "running"
    assert _session_status_with_child_rollup(parent, ["conv_child"]) == "running"


def test_session_status_with_child_rollup_child_activity() -> None:
    """Idle parent reads running when a direct child is active."""
    parent, child = "conv_parent", "conv_child"
    sessions_mod._session_status_cache[child] = "waiting"
    assert _session_status_with_child_rollup(parent, [child]) == "running"


def test_session_status_with_child_rollup_inherits_parent_failed() -> None:
    """Failed parent stays failed when children are idle."""
    parent = "conv_parent"
    sessions_mod._session_status_cache[parent] = "failed"
    assert _session_status_with_child_rollup(parent, []) == "failed"


# ── usage / cost helpers ─────────────────────────────────────────────────────


def test_utc_day_formats_utc_calendar_date() -> None:
    """Epoch seconds map to the UTC ``YYYY-MM-DD`` bucket."""
    from datetime import datetime, timezone

    epoch = int(datetime(2026, 6, 5, tzinfo=timezone.utc).timestamp())
    assert _utc_day(epoch) == "2026-06-05"


def test_priced_cost_for_display_present_and_absent() -> None:
    """Absent key means unpriced; present key coerces to float."""
    assert _priced_cost_for_display({"input_tokens": 10}) is None
    assert _priced_cost_for_display({"total_cost_usd": 0.42}) == pytest.approx(0.42)
    assert _priced_cost_for_display({"total_cost_usd": "0.5"}) == pytest.approx(0.5)


def test_priced_cost_for_display_malformed_returns_none() -> None:
    """Bad persisted values must not break snapshots."""
    assert _priced_cost_for_display({"total_cost_usd": "not-a-number"}) is None


def test_model_usage_bucket_creates_nested_structure() -> None:
    """First touch creates ``by_model`` and the per-model bucket."""
    usage: dict[str, Any] = {}
    bucket = _model_usage_bucket(usage, "claude-sonnet-4-6")
    bucket["input_tokens"] = 100
    assert usage["by_model"]["claude-sonnet-4-6"]["input_tokens"] == 100
    assert _model_usage_bucket(usage, "claude-sonnet-4-6") is bucket


def test_add_model_usage_delta_tokens_and_cost() -> None:
    """Token deltas accumulate; cost is added only when priced."""
    bucket: dict[str, float] = {"input_tokens": 10}
    _add_model_usage_delta(bucket, {"input_tokens": 5, "output_tokens": 3}, cost_delta=0.1)
    assert bucket["input_tokens"] == 15
    assert bucket["output_tokens"] == 3
    assert bucket["total_cost_usd"] == pytest.approx(0.1)

    unpriced: dict[str, float] = {}
    _add_model_usage_delta(unpriced, {"input_tokens": 1}, cost_delta=None)
    assert "total_cost_usd" not in unpriced


def test_usage_by_model_for_display_projects_typed_models() -> None:
    """Nested buckets become :class:`ModelUsage` entries."""
    usage = {
        "by_model": {
            "m1": {"input_tokens": 10, "total_cost_usd": 0.2},
            "m2": {"output_tokens": "bad"},
        }
    }
    out = _usage_by_model_for_display(usage)
    assert out is not None
    assert out["m1"].input_tokens == 10
    assert out["m1"].total_cost_usd == pytest.approx(0.2)
    assert out["m2"].input_tokens is None


def test_usage_by_model_for_display_empty_returns_none() -> None:
    """Missing or empty ``by_model`` omits the API field entirely."""
    assert _usage_by_model_for_display({}) is None
    assert _usage_by_model_for_display({"by_model": {}}) is None
    assert _usage_by_model_for_display({"by_model": "nope"}) is None


class _ConvStore:
    """Minimal store stub for daily-cost attribution."""

    def __init__(self, owner: str | None) -> None:
        self.owner = owner
        self.daily: list[tuple[str, str, float]] = []

    def get_session_owner(self, conv_id: str) -> str | None:
        del conv_id
        return self.owner

    def add_daily_cost(self, owner: str, day: str, delta: float) -> None:
        self.daily.append((owner, day, delta))


def test_record_daily_cost_noops(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-positive deltas and missing owners are ignored."""
    store = _ConvStore(owner="alice@test.com")
    conv = Conversation(
        id="conv_x", created_at=0, updated_at=0, root_conversation_id="conv_x"
    )
    _record_daily_cost(None, 1.0, store)  # type: ignore[arg-type]
    _record_daily_cost(conv, 0.0, store)
    _record_daily_cost(conv, -1.0, store)
    assert store.daily == []

    no_owner = _ConvStore(owner=None)
    _record_daily_cost(conv, 1.0, no_owner)
    assert no_owner.daily == []


def test_record_daily_cost_attributes_to_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive deltas roll into the owner's UTC day bucket."""
    from datetime import datetime, timezone

    epoch = int(datetime(2026, 6, 5, tzinfo=timezone.utc).timestamp())
    monkeypatch.setattr("omnigent.db.utils.now_epoch", lambda: epoch)
    store = _ConvStore(owner="alice@test.com")
    conv = Conversation(
        id="conv_x", created_at=0, updated_at=0, root_conversation_id="conv_x"
    )
    _record_daily_cost(conv, 0.75, store)
    assert store.daily == [("alice@test.com", "2026-06-05", 0.75)]


# ── file resource + message text ─────────────────────────────────────────────


def test_stored_file_to_resource_shape() -> None:
    """Stored files project to the unified session.resource dict."""
    stored = StoredFile(
        id="file_abc",
        created_at=1_700_000_000,
        filename="notes.txt",
        bytes=12,
        content_type="text/plain",
        session_id="conv_x",
    )
    out = _stored_file_to_resource("conv_x", stored)
    assert out["object"] == "session.resource"
    assert out["type"] == "file"
    assert out["session_id"] == "conv_x"
    assert out["name"] == "notes.txt"
    assert out["metadata"]["bytes"] == 12


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ([{"type": "output_text", "text": "Hello"}], "Hello"),
        ([{"type": "input_text", "input_text": "Ping"}], "Ping"),
        ([{"text": "A"}, {"text": "B"}], "A\nB"),
        ([{"type": "image"}], None),
        ([], None),
    ],
)
def test_message_text_extracts_joined_text(
    content: list[dict[str, Any]], expected: str | None
) -> None:
    """Text blocks join with newlines; non-text blocks are skipped."""
    assert _message_text(content) == expected


# ── task error + codex sub-agent helpers ─────────────────────────────────────


def test_last_task_error_from_labels_projects_both_fields() -> None:
    """Runner failure labels become the public error shape."""
    labels = {
        _LAST_TASK_ERROR_CODE_LABEL_KEY: "runner_unavailable",
        _LAST_TASK_ERROR_MESSAGE_LABEL_KEY: "Host offline",
    }
    assert _last_task_error_from_labels(labels) == {
        "code": "runner_unavailable",
        "message": "Host offline",
    }


def test_last_task_error_from_labels_partial_returns_none() -> None:
    """Either missing label suppresses the projection."""
    assert _last_task_error_from_labels({_LAST_TASK_ERROR_CODE_LABEL_KEY: "x"}) is None
    assert _last_task_error_from_labels({_LAST_TASK_ERROR_MESSAGE_LABEL_KEY: "y"}) is None


def test_codex_subagent_display_tool_precedence() -> None:
    """Nickname beats role beats generic fallback."""
    assert _codex_subagent_display_tool(
        {_CODEX_NATIVE_SUBAGENT_NICKNAME_LABEL_KEY: "auth-auditor"}
    ) == "auth-auditor"
    assert _codex_subagent_display_tool(
        {_CODEX_NATIVE_SUBAGENT_ROLE_LABEL_KEY: "reviewer"}
    ) == "reviewer"
    assert _codex_subagent_display_tool({}) == _CODEX_NATIVE_SUBAGENT_DISPLAY_FALLBACK


def test_resolve_harness_returns_persisted_override() -> None:
    """Per-session harness_override wins without loading the agent spec."""
    conv = Conversation(
        id="conv_x",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_x",
        harness_override="pi",
    )
    assert _resolve_harness(conv) == "pi"
    assert _resolve_harness(None) is None


def test_client_supplied_hook_elicitation_id_validates_namespace() -> None:
    """Hook re-attach ids must match the claude-hook namespace."""
    valid = "elicit_claude_" + "a" * 32
    assert _client_supplied_hook_elicitation_id({}, "conv_a") is None
    assert (
        _client_supplied_hook_elicitation_id(
            {"_omnigent_elicitation_id": valid}, "conv_a"
        )
        == valid
    )

    with pytest.raises(OmnigentError) as bad:
        _client_supplied_hook_elicitation_id(
            {"_omnigent_elicitation_id": "elicit_codex_nope"}, "conv_a"
        )
    assert bad.value.code == ErrorCode.INVALID_INPUT


def test_client_supplied_hook_elicitation_id_rejects_cross_session_owner() -> None:
    """A parked id owned by another session is rejected."""
    elicitation_id = "elicit_claude_" + "b" * 32
    sessions_mod._harness_elicitation_owners[elicitation_id] = "conv_owner"
    try:
        with pytest.raises(OmnigentError) as exc:
            _client_supplied_hook_elicitation_id(
                {"_omnigent_elicitation_id": elicitation_id},
                "conv_other",
            )
        assert exc.value.code == ErrorCode.INVALID_INPUT
    finally:
        sessions_mod._harness_elicitation_owners.pop(elicitation_id, None)


def test_is_codex_native_subagent_requires_wrapper_label() -> None:
    """Only codex-native sub-agent rows match."""
    codex_child = Conversation(
        id="conv_child",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_root",
        kind="sub_agent",
        labels={
            _CLAUDE_NATIVE_WRAPPER_LABEL_KEY: _CODEX_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE,
        },
    )
    other = Conversation(
        id="conv_other",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_other",
        kind="default",
    )
    assert _is_codex_native_subagent(codex_child) is True
    assert _is_codex_native_subagent(other) is False


# ── elicitation tombstones + ancestor walk ───────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_pre_resolved_elicitations() -> None:
    """Isolate tombstone helper tests from other suites."""
    sessions_mod._harness_pre_resolved_elicitations.clear()
    yield
    sessions_mod._harness_pre_resolved_elicitations.clear()


def test_targeted_elicitation_event_annotates_params() -> None:
    """Mirrored elicitations carry the owning session as resolution target."""
    event = {"type": "response.elicitation_request", "params": {"tool": "Bash"}}
    mirrored = _targeted_elicitation_event(event, target_session_id="conv_child")
    assert mirrored["params"]["target_session_id"] == "conv_child"
    assert mirrored["params"]["tool"] == "Bash"

    bare = _targeted_elicitation_event(
        {"type": "response.elicitation_request"},
        target_session_id="conv_x",
    )
    assert bare["params"] == {"target_session_id": "conv_x"}


class _AncestorStore:
    def __init__(self, convs: dict[str, Conversation]) -> None:
        self._convs = convs

    def get_conversation(self, conv_id: str) -> Conversation | None:
        return self._convs.get(conv_id)


def test_ancestor_session_ids_walks_parent_chain() -> None:
    """Ancestors return nearest-parent-first order."""
    root = Conversation(
        id="conv_root", created_at=0, updated_at=0, root_conversation_id="conv_root"
    )
    parent = Conversation(
        id="conv_parent",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_root",
        parent_conversation_id="conv_root",
    )
    child = Conversation(
        id="conv_child",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_root",
        parent_conversation_id="conv_parent",
    )
    store = _AncestorStore({"conv_child": child, "conv_parent": parent, "conv_root": root})
    assert _ancestor_session_ids(store, "conv_child") == ["conv_parent", "conv_root"]  # type: ignore[arg-type]


def test_consume_pre_resolved_harness_elicitation_session_match() -> None:
    """Matching session consumes the tombstone; mismatched session restores it."""
    import time

    tombstone = _PreResolvedHarnessElicitation(session_id="conv_a", created_at=time.time())
    sessions_mod._harness_pre_resolved_elicitations["elicit_x"] = tombstone

    consumed = _consume_pre_resolved_harness_elicitation("conv_a", "elicit_x")
    assert consumed is tombstone
    assert "elicit_x" not in sessions_mod._harness_pre_resolved_elicitations

    sessions_mod._harness_pre_resolved_elicitations["elicit_y"] = _PreResolvedHarnessElicitation(
        session_id="conv_owner", created_at=time.time()
    )
    assert _consume_pre_resolved_harness_elicitation("conv_other", "elicit_y") is None
    assert "elicit_y" in sessions_mod._harness_pre_resolved_elicitations


def test_prune_pre_resolved_harness_elicitations_expires_and_caps() -> None:
    """Stale tombstones expire; overflow evicts oldest entries."""
    sessions_mod._harness_pre_resolved_elicitations["old"] = _PreResolvedHarnessElicitation(
        session_id="conv_a", created_at=0.0
    )
    sessions_mod._harness_pre_resolved_elicitations["fresh"] = _PreResolvedHarnessElicitation(
        session_id="conv_a", created_at=200.0
    )
    _prune_pre_resolved_harness_elicitations(now=400.0)
    assert "old" not in sessions_mod._harness_pre_resolved_elicitations
    assert "fresh" in sessions_mod._harness_pre_resolved_elicitations

    max_entries = sessions_mod._HARNESS_PRE_RESOLVED_ELICITATION_MAX_ENTRIES
    for i in range(max_entries + 3):
        sessions_mod._harness_pre_resolved_elicitations[f"elicit_{i}"] = (
            _PreResolvedHarnessElicitation(session_id="conv_a", created_at=float(i))
        )
    _prune_pre_resolved_harness_elicitations(now=10_000.0)
    assert len(sessions_mod._harness_pre_resolved_elicitations) <= max_entries


# ── usage coercion + file merge + external parse ─────────────────────────────


def test_coerce_cumulative_field_validates_numeric_and_int() -> None:
    """Token fields require int; cost fields accept float."""
    assert _coerce_cumulative_field({"cumulative_input_tokens": 10}, "cumulative_input_tokens", numeric=False) == 10
    assert _coerce_cumulative_field({"cumulative_cost_usd": 0.5}, "cumulative_cost_usd", numeric=True) == 0.5
    assert _coerce_cumulative_field({}, "missing", numeric=False) is None

    with pytest.raises(OmnigentError):
        _coerce_cumulative_field({"cumulative_input_tokens": -1}, "cumulative_input_tokens", numeric=False)
    with pytest.raises(OmnigentError):
        _coerce_cumulative_field({"cumulative_input_tokens": True}, "cumulative_input_tokens", numeric=False)


def test_merge_pending_file_blocks_prepends_images() -> None:
    """Pending file blocks fold into durable user messages."""
    item = NewConversationItem(
        type="message",
        response_id="resp_merge",
        data=MessageData(role="user", content=[{"type": "input_text", "text": "hi"}]),
    )
    pending = [
        {"type": "input_image", "file_id": "file_img"},
        {"type": "input_text", "text": "hi"},
    ]
    merged = _merge_pending_file_blocks(item, pending)
    assert merged.data.content[0]["type"] == "input_image"
    assert merged.data.content[1]["type"] == "input_text"


def test_merge_pending_file_blocks_noops_without_files() -> None:
    """Text-only pending content and duplicate file blocks are left unchanged."""
    item = NewConversationItem(
        type="message",
        response_id="resp_merge",
        data=MessageData(
            role="user",
            content=[{"type": "input_file", "file_id": "file_a"}],
        ),
    )
    assert _merge_pending_file_blocks(item, [{"type": "input_text", "text": "x"}]) is item


def test_parse_external_assistant_message_unpacks_fields() -> None:
    """External assistant events require agent + text; response_id may be minted."""
    from omnigent.server.schemas import SessionEventInput

    body = SessionEventInput(
        type="external_assistant_message",
        data={"agent": " claude-native ", "text": "hello", "response_id": "resp_1"},
    )
    agent, text, response_id = _parse_external_assistant_message(body)
    assert agent == "claude-native"
    assert text == "hello"
    assert response_id == "resp_1"

    with pytest.raises(OmnigentError):
        _parse_external_assistant_message(
            SessionEventInput(type="external_assistant_message", data={"agent": "", "text": "x"})
        )


def test_codex_subagent_labels_from_body_maps_optional_fields() -> None:
    """Codex child rows stamp wrapper + thread metadata from the event body."""
    from omnigent.server.schemas import SessionEventInput

    body = SessionEventInput(
        type="external_codex_subagent_start",
        data={
            "parent_thread_id": "thread_parent",
            "agent_nickname": "auth-auditor",
            "agent_role": "reviewer",
        },
    )
    labels = _codex_subagent_labels_from_body("thread_child", body)
    assert labels[_CLAUDE_NATIVE_WRAPPER_LABEL_KEY] == _CODEX_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE
    assert labels[_CODEX_NATIVE_SUBAGENT_NICKNAME_LABEL_KEY] == "auth-auditor"


# ── validation helpers + native terminal metadata ────────────────────────────


def test_build_actor_returns_run_as_or_none() -> None:
    assert _build_actor(None) is None
    assert _build_actor("alice@example.com") == {"run_as": "alice@example.com"}


def test_validated_cost_control_mode_override_accepts_on_off() -> None:
    assert _validated_cost_control_mode_override(None) is None
    assert _validated_cost_control_mode_override("on") == "on"
    with pytest.raises(OmnigentError):
        _validated_cost_control_mode_override("maybe")


def test_validate_terminal_launch_args_bounds() -> None:
    assert _validate_terminal_launch_args(None) is None
    assert _validate_terminal_launch_args(["--flag"]) == ["--flag"]
    with pytest.raises(ValueError):
        _validate_terminal_launch_args(["x" * 5000])


def test_parse_session_create_metadata_validates_json() -> None:
    parsed = _parse_session_create_metadata('{"title": "debug flow"}')
    assert parsed.title == "debug flow"
    with pytest.raises(OmnigentError):
        _parse_session_create_metadata("not-json")


def test_multipart_missing_detail_shape() -> None:
    detail = _multipart_missing_detail("bundle")
    assert detail["type"] == "missing"
    assert detail["loc"] == ["body", "bundle"]


def test_reject_reserved_cost_control_label_seed() -> None:
    with pytest.raises(OmnigentError):
        _reject_reserved_cost_control_label_seed({"cost_control.plan": "forged"})
    _reject_reserved_cost_control_label_seed({"team": "ml"})


def test_native_terminal_helpers_resolve_claude_wrapper() -> None:
    conv = Conversation(
        id="conv_claude",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_claude",
        labels={_CLAUDE_NATIVE_WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE},
    )
    assert _is_native_terminal_session(conv) is True
    display, model, harness = _native_terminal_runtime(conv)
    assert display == "Claude"
    assert harness == "claude-native"
    assert _native_terminal_name_for_harness("codex-native") == "codex"

    plain = Conversation(
        id="conv_plain", created_at=0, updated_at=0, root_conversation_id="conv_plain"
    )
    assert _is_native_terminal_session(plain) is False
    with pytest.raises(OmnigentError):
        _native_terminal_runtime(plain)


def test_build_evaluation_context_maps_tool_and_request_phases() -> None:
    actor = {"run_as": "alice@example.com"}
    tool_ctx = _build_evaluation_context(
        Phase.TOOL_CALL,
        {"name": "Bash", "arguments": {"command": "ls"}},
        {"context": {"model": "claude-sonnet", "harness": "claude-native"}},
        actor=actor,
    )
    assert tool_ctx.tool_name == "Bash"
    assert tool_ctx.model == "claude-sonnet"

    result_ctx = _build_evaluation_context(
        Phase.TOOL_RESULT,
        {"result": "ok"},
        {"request_data": {"name": "Bash"}},
        actor=actor,
    )
    assert result_ctx.tool_name == "Bash"
    assert result_ctx.content == {"result": "ok"}

    request_ctx = _build_evaluation_context(
        Phase.REQUEST,
        {"text": "hello"},
        {},
        actor=actor,
    )
    assert request_ctx.content == "hello"


# ── batch42: discovery, tenant scope, descendants, MCP, skills ───────────────


@pytest.fixture(autouse=True)
def _clear_todos_cache() -> None:
    sessions_mod._session_todos_cache.clear()
    yield
    sessions_mod._session_todos_cache.clear()


def test_announce_session_added_publishes_discovery_and_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_calls: list[tuple[str, dict[str, Any]]] = []
    hub_calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        sessions_mod.user_session_stream,
        "publish",
        lambda key, payload: stream_calls.append((key, payload)),
    )
    monkeypatch.setattr(
        sessions_mod.event_hub,
        "publish",
        lambda key, payload: hub_calls.append((key, payload)),
    )
    _announce_session_added("alice@example.com", "conv_new")
    assert stream_calls == [("alice@example.com", {"type": "session_added", "session_id": "conv_new"})]
    assert hub_calls == [("alice@example.com", {"type": "session.created", "session_id": "conv_new"})]


def test_native_ask_gate_lock_returns_shared_lock_per_key() -> None:
    lock_a1 = _native_ask_gate_lock("conv_1", "cost_guard")
    lock_a2 = _native_ask_gate_lock("conv_1", "cost_guard")
    lock_b = _native_ask_gate_lock("conv_1", "other_policy")
    assert lock_a1 is lock_a2
    assert lock_a1 is not lock_b


def test_resilient_stream_payload_validates_known_events() -> None:
    event = {"type": "session.usage", "conversation_id": "conv_1", "total_cost_usd": 1.5}
    normalized = _resilient_stream_payload(event, "conv_1")
    assert normalized["type"] == "session.usage"
    assert normalized["total_cost_usd"] == 1.5


def test_resilient_stream_payload_forwards_invalid_events() -> None:
    bad = {"type": "response.failed"}
    assert _resilient_stream_payload(bad, "conv_x") is bad


def test_enforce_tenant_scope_raises_not_found_on_mismatch() -> None:
    conv = Conversation(
        id="conv_a",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_a",
        tenant_id="tenant_b",
    )
    with pytest.raises(OmnigentError) as exc:
        _enforce_tenant_scope("tenant_a", conv)
    assert exc.value.code == ErrorCode.NOT_FOUND


def test_enforce_tenant_scope_allows_matching_or_legacy_rows() -> None:
    conv = Conversation(
        id="conv_a",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_a",
        tenant_id="tenant_a",
    )
    _enforce_tenant_scope("tenant_a", conv)
    _enforce_tenant_scope(None, conv)
    _enforce_tenant_scope("tenant_a", None)


class _DescendantStore:
    def __init__(self, children_by_parent: dict[str, list[Conversation]]) -> None:
        self._children = children_by_parent

    def list_conversations(
        self,
        *,
        kind: str | None = None,
        parent_conversation_id: str | None = None,
        limit: int = 100,
        after: str | None = None,
        **_: Any,
    ) -> PagedList[Conversation]:
        assert kind == "sub_agent"
        assert parent_conversation_id is not None
        kids = self._children.get(parent_conversation_id, [])
        if after is not None:
            ids = [c.id for c in kids]
            if after not in ids:
                return PagedList()
            kids = kids[ids.index(after) + 1 :]
        page = kids[:limit]
        return PagedList(
            data=page,
            first_id=page[0].id if page else None,
            last_id=page[-1].id if page else None,
            has_more=len(kids) > limit,
        )


def test_descendant_sessions_walks_sub_agent_tree() -> None:
    root = Conversation(
        id="conv_root", created_at=0, updated_at=0, root_conversation_id="conv_root"
    )
    child = Conversation(
        id="conv_child",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_root",
        parent_conversation_id="conv_root",
        kind="sub_agent",
    )
    grandchild = Conversation(
        id="conv_grand",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_root",
        parent_conversation_id="conv_child",
        kind="sub_agent",
    )
    store = _DescendantStore({"conv_root": [child], "conv_child": [grandchild]})
    descendants = _descendant_sessions(store, "conv_root")  # type: ignore[arg-type]
    assert [d.id for d in descendants] == ["conv_child", "conv_grand"]


def test_parse_skill_slash_command_unpacks_name_and_arguments() -> None:
    body = SessionEventInput(
        type="slash_command",
        data={"kind": "skill", "name": "grill-me", "arguments": "review plan"},
    )
    assert _parse_skill_slash_command(body) == ("grill-me", "review plan")


def test_parse_skill_slash_command_rejects_non_skill_kind() -> None:
    body = SessionEventInput(type="slash_command", data={"kind": "compact"})
    with pytest.raises(OmnigentError):
        _parse_skill_slash_command(body)


def test_build_skill_slash_command_policy_body_projects_user_text() -> None:
    body = SessionEventInput(
        type="slash_command",
        data={"name": "grill-me", "arguments": "review plan"},
    )
    projected = _build_skill_slash_command_policy_body(body)
    assert projected.type == "message"
    assert projected.data["role"] == "user"
    assert projected.data["content"][0]["text"] == "/grill-me review plan"


def test_extract_user_and_assistant_text_from_events() -> None:
    user = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
    )
    assert _extract_user_text_from_event(user) == "hello"
    assistant = SessionEventInput(
        type="message",
        data={"role": "assistant", "content": [{"type": "output_text", "text": "hi"}]},
    )
    assert _extract_assistant_text_from_event(assistant) == "hi"


def test_handle_external_session_todos_filters_and_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, Any]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )
    body = SessionEventInput(
        type="external_session_todos",
        data={
            "todos": [
                {
                    "content": "Fix bug",
                    "status": "in_progress",
                    "activeForm": "Fixing bug",
                },
                {"content": "bad", "status": "bogus", "activeForm": "x"},
            ],
        },
    )
    _handle_external_session_todos("conv_todos", body)
    assert sessions_mod._session_todos_cache["conv_todos"] == [
        {"content": "Fix bug", "status": "in_progress", "activeForm": "Fixing bug"},
    ]
    assert published[0]["type"] == "session.todos"
    assert published[0]["todos"][0]["content"] == "Fix bug"


def test_handle_external_session_todos_requires_list() -> None:
    body = SessionEventInput(type="external_session_todos", data={"todos": "nope"})
    with pytest.raises(OmnigentError):
        _handle_external_session_todos("conv_x", body)


def test_mcp_json_rpc_responses_wrap_payloads() -> None:
    ok = _mcp_ok_response(1, {"tools": []})
    assert json.loads(ok.body) == {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
    err = _mcp_error_response(2, -32601, "not found")
    assert json.loads(err.body)["error"]["code"] == -32601


def _minimal_spec(*, harness: str, config: dict[str, str] | None = None) -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="worker",
        executor=ExecutorSpec(type="omnigent", config={"harness": harness, **(config or {})}),
    )


def test_spec_harness_canonicalizes_executor_config() -> None:
    assert _spec_harness(_minimal_spec(harness="codex-native")) == "codex-native"


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"yolo": True}, True),
        ({"yolo": "true"}, True),
        ({"yolo": "True"}, True),
        ({"yolo": "false"}, False),
        ({}, False),
    ],
)
def test_spec_config_flag_enabled_coerces_yaml_strings(
    config: dict[str, object], expected: bool
) -> None:
    spec = AgentSpec(
        spec_version=1,
        name="worker",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native", **config}),
    )
    assert _spec_config_flag_enabled(spec, "yolo") is expected


def test_derive_terminal_launch_args_codex_yolo() -> None:
    spec = _minimal_spec(harness="codex-native", config={"yolo": "true"})
    args = _derive_terminal_launch_args_from_spec(spec)
    assert args == ["--dangerously-bypass-approvals-and-sandbox"]


def test_derive_terminal_launch_args_claude_permission_mode() -> None:
    spec = _minimal_spec(harness="claude-native", config={"permission_mode": "bypassPermissions"})
    args = _derive_terminal_launch_args_from_spec(spec)
    assert args == ["--permission-mode", "bypassPermissions"]


def test_derive_terminal_launch_args_non_native_returns_none() -> None:
    assert _derive_terminal_launch_args_from_spec(_minimal_spec(harness="claude-sdk")) is None


# ── batch-43: list projection + resolver helpers ─────────────────────────────


def test_structured_ask_user_question_builds_typed_payload() -> None:
    payload = _structured_ask_user_question(
        {
            "questions": [
                {
                    "header": "Scope",
                    "question": "Which area?",
                    "multiSelect": True,
                    "options": [
                        {"label": "Auth", "description": "Login flows", "preview": "auth.py"},
                        "Skip",
                        {"label": "", "description": "ignored"},
                    ],
                },
                {"question": "", "options": ["x"]},
            ],
        }
    )
    assert payload is not None
    assert len(payload["questions"]) == 1
    question = payload["questions"][0]
    assert question["question"] == "Which area?"
    assert question["header"] == "Scope"
    assert question["multiSelect"] is True
    assert question["options"][0]["label"] == "Auth"
    assert question["options"][0]["preview"] == "auth.py"
    assert question["options"][1] == {"label": "Skip"}


def test_structured_ask_user_question_returns_none_for_invalid_shapes() -> None:
    assert _structured_ask_user_question("not-a-dict") is None
    assert _structured_ask_user_question({"questions": []}) is None
    assert _structured_ask_user_question({"questions": [{"question": "x", "options": []}]}) is None


def test_build_session_list_item_projects_permissions_and_comments() -> None:
    from omnigent.server.schemas import SessionListItem

    conv = Conversation(
        id="conv_list",
        created_at=10,
        updated_at=20,
        root_conversation_id="conv_list",
        agent_id="ag_demo",
        title="Demo [closed]",
        labels={"tier": "cheap"},
        runner_id="run_1",
        host_id="host_1",
        reasoning_effort="high",
        workspace="/tmp/ws",
        git_branch="feature/x",
        archived=True,
    )
    grants = [
        SessionPermission(
            user_id="alice@example.com",
            conversation_id="conv_list",
            level=LEVEL_OWNER,
        ),
    ]
    fingerprint = CommentsFingerprint(count=3, last_updated_at=99_000_000)

    item = _build_session_list_item(
        conv,
        agent_names_by_id={"ag_demo": "demo-agent"},
        agent_display_names_by_id={"ag_demo": "Demo Person"},
        grants=grants,
        user_id="alice@example.com",
        user_is_admin=False,
        permissions_enabled=True,
        pending_count=2,
        child_session_ids=["conv_child"],
        comments_fingerprint=fingerprint,
    )

    assert isinstance(item, SessionListItem)
    assert item.id == "conv_list"
    assert item.agent_name == "demo-agent"
    assert item.agent_display_name == "Demo Person"
    assert item.permission_level == LEVEL_OWNER
    assert item.owner == "alice@example.com"
    assert item.pending_elicitations_count == 2
    assert item.comments_count == 3
    assert item.comments_updated_at == 99_000_000
    assert item.archived is True
    assert item.git_branch == "feature/x"


@pytest.mark.asyncio
async def test_apply_liveness_to_items_mutates_runner_and_host_fields() -> None:
    from omnigent.server.schemas import SessionListItem

    items = [
        SessionListItem(
            id="conv_a",
            agent_id="ag_a",
            status="idle",
            created_at=1,
            updated_at=1,
        ),
        SessionListItem(
            id="conv_b",
            agent_id="ag_b",
            status="running",
            created_at=2,
            updated_at=2,
        ),
    ]

    def _lookup(ids: list[str]) -> dict[str, SessionLiveness]:
        return {
            "conv_a": SessionLiveness(runner_online=True, host_online=False),
            "conv_b": SessionLiveness(runner_online=False, host_online=None),
        }

    await _apply_liveness_to_items(items, _lookup)
    assert items[0].runner_online is True
    assert items[0].host_online is False
    assert items[1].runner_online is False
    assert items[1].host_online is None


@pytest.mark.asyncio
async def test_apply_liveness_to_items_noop_when_lookup_missing() -> None:
    from omnigent.server.schemas import SessionListItem

    item = SessionListItem(
        id="conv_x",
        agent_id="ag_x",
        status="idle",
        created_at=0,
        updated_at=0,
        runner_online=None,
        host_online=None,
    )
    await _apply_liveness_to_items([item], None)
    assert item.runner_online is None
    assert item.host_online is None


def test_agent_display_names_for_resolves_params_display_name() -> None:
    agent = Agent(
        id="ag_maya",
        created_at=1,
        name="maya",
        bundle_location="ag_maya/abc",
    )
    spec = AgentSpec(
        spec_version=1,
        name="maya",
        params={"displayName": "Maya Chen"},
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )
    store = MagicMock()
    store.get.return_value = agent
    cache = MagicMock()
    cache.load.return_value = LoadedAgent(spec=spec, workdir=Path("/tmp/maya"))

    names = _agent_display_names_for(["ag_maya"], store, cache)
    assert names == {"ag_maya": "Maya Chen"}


def test_agent_display_names_for_returns_empty_when_cache_disabled() -> None:
    assert _agent_display_names_for(["ag_x"], MagicMock(), None) == {}


def test_resolve_llm_model_reads_bound_agent_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.spec.types import LLMConfig

    conv = Conversation(
        id="conv_model",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_model",
        agent_id="ag_test",
    )
    agent = Agent(id="ag_test", created_at=1, name="t", bundle_location="ag_test/x")
    spec = AgentSpec(
        spec_version=1,
        name="t",
        llm=LLMConfig(model="claude-sonnet-4-6"),
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )
    store = MagicMock()
    store.get.return_value = agent
    cache = MagicMock()
    cache.load.return_value = LoadedAgent(spec=spec, workdir=Path("/tmp/t"))

    monkeypatch.setattr("omnigent.runtime._globals._agent_store", store)
    monkeypatch.setattr("omnigent.runtime.get_agent_cache", lambda: cache)
    assert _resolve_llm_model(conv) == "claude-sonnet-4-6"
    assert _resolve_llm_model(None) is None


def test_resolve_output_schema_reads_bound_agent_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    conv = Conversation(
        id="conv_schema",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_schema",
        agent_id="ag_schema",
    )
    agent = Agent(id="ag_schema", created_at=1, name="s", bundle_location="ag_schema/x")
    spec = AgentSpec(
        spec_version=1,
        name="s",
        output_schema=schema,
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )
    store = MagicMock()
    store.get.return_value = agent
    cache = MagicMock()
    cache.load.return_value = LoadedAgent(spec=spec, workdir=Path("/tmp/s"))

    monkeypatch.setattr("omnigent.runtime._globals._agent_store", store)
    monkeypatch.setattr("omnigent.runtime.get_agent_cache", lambda: cache)
    assert _resolve_output_schema(conv) == schema


def test_validated_harness_override_canonicalizes_known_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(id="ag_h", created_at=1, name="h", bundle_location="ag_h/x")
    spec = AgentSpec(
        spec_version=1,
        name="h",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )
    cache = MagicMock()
    cache.load.return_value = LoadedAgent(spec=spec, workdir=Path("/tmp/h"))
    monkeypatch.setattr("omnigent.runtime.get_agent_cache", lambda: cache)
    assert _validated_harness_override("pi", agent) == "pi"
    assert _validated_harness_override(None, agent) is None


def test_validated_harness_override_rejects_unknown_harness() -> None:
    agent = Agent(id="ag_h", created_at=1, name="h", bundle_location="ag_h/x")
    with pytest.raises(OmnigentError) as exc:
        _validated_harness_override("not-a-real-harness", agent)
    assert exc.value.code == ErrorCode.INVALID_INPUT


def test_validated_harness_override_rejects_non_omnigent_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(id="ag_h", created_at=1, name="h", bundle_location="ag_h/x")
    spec = AgentSpec(
        spec_version=1,
        name="h",
        executor=ExecutorSpec(type="workflow", config={}),
    )
    cache = MagicMock()
    cache.load.return_value = LoadedAgent(spec=spec, workdir=Path("/tmp/h"))
    monkeypatch.setattr("omnigent.runtime.get_agent_cache", lambda: cache)
    with pytest.raises(OmnigentError) as exc:
        _validated_harness_override("claude-sdk", agent)
    assert exc.value.code == ErrorCode.INVALID_INPUT


# ── batch-44: publish, parse, usage, and snapshot helpers ────────────────────


def _conversation_item(
    *,
    item_id: str = "msg_1",
    role: str = "user",
    text: str = "hello",
    is_meta: bool = False,
    agent: str | None = None,
) -> ConversationItem:
    data_kwargs: dict[str, object] = {
        "role": role,
        "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
        "is_meta": is_meta,
    }
    if agent is not None:
        data_kwargs["agent"] = agent
    return ConversationItem(
        id=item_id,
        type="message",
        status="completed",
        response_id="resp_1",
        created_at=1,
        data=MessageData(**data_kwargs),  # type: ignore[arg-type]
        created_by="alice@example.com",
    )


def test_publish_input_consumed_skips_meta_and_emits_user_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda sid, payload: published.append((sid, payload)),
    )

    _publish_input_consumed("conv_x", _conversation_item(is_meta=True))
    assert published == []

    _publish_input_consumed(
        "conv_x",
        _conversation_item(),
        cleared_pending_id="pending_abc",
    )
    assert len(published) == 1
    assert published[0][0] == "conv_x"
    assert published[0][1]["type"] == "session.input.consumed"
    data = published[0][1]["data"]
    assert data["item_id"] == "msg_1"
    assert data["cleared_pending_id"] == "pending_abc"


def test_publish_compaction_events_use_standard_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )

    _publish_compaction_in_progress("conv_cmp")
    _publish_compaction_completed("conv_cmp", total_tokens=8421)
    _publish_compaction_failed("conv_cmp")

    assert published[0] == {"type": "response.compaction.in_progress"}
    assert published[1]["type"] == "response.compaction.completed"
    assert published[1]["total_tokens"] == 8421
    assert published[2] == {"type": "response.compaction.failed"}


def test_publish_external_assistant_message_broadcasts_output_item_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )

    item = _conversation_item(
        item_id="msg_asst",
        role="assistant",
        text="mirrored reply",
        agent="claude-native",
    )
    _publish_external_assistant_message(
        "conv_ext",
        item,
        response_id="resp_ext",
        agent_name="claude-native",
    )

    assert len(published) == 1
    assert published[0]["type"] == "response.output_item.done"
    assert published[0]["item"]["id"] == "msg_asst"
    assert published[0]["item"]["role"] == "assistant"


def test_parse_external_assistant_message_mints_response_id_when_absent() -> None:
    from omnigent.server.schemas import SessionEventInput

    body = SessionEventInput(
        type="external_assistant_message",
        data={"agent": "codex-native", "text": "done"},
    )
    agent, text, response_id = _parse_external_assistant_message(body)
    assert agent == "codex-native"
    assert text == "done"
    assert response_id.startswith("resp_")


def test_parse_external_conversation_item_builds_typed_new_item() -> None:
    from omnigent.server.schemas import SessionEventInput

    body = SessionEventInput(
        type="external_conversation_item",
        data={
            "item_type": "message",
            "item_data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "external ping"}],
            },
            "response_id": "resp_item",
        },
    )
    parsed = _parse_external_conversation_item(body)
    assert parsed.type == "message"
    assert parsed.response_id == "resp_item"
    assert parsed.data.role == "user"  # type: ignore[attr-defined]

    with pytest.raises(OmnigentError):
        _parse_external_conversation_item(
            SessionEventInput(
                type="external_conversation_item",
                data={"item_type": "unknown", "item_data": {}},
            )
        )


def test_structured_ask_user_question_accepts_string_options() -> None:
    payload = _structured_ask_user_question(
        {
            "questions": [
                {
                    "question": "Pick one",
                    "options": ["Alpha", {"label": ""}],
                }
            ],
        }
    )
    assert payload is not None
    assert payload["questions"][0]["options"] == [{"label": "Alpha"}]


def test_structured_ask_user_question_skips_malformed_question_entries() -> None:
    """Non-dict questions and non-list options are ignored without crashing."""
    payload = _structured_ask_user_question(
        {
            "questions": [
                "not-a-dict",
                {"question": "Valid?", "options": "not-a-list"},
                {"question": "Pick", "options": [{"label": "ok"}]},
            ],
        }
    )
    assert payload is not None
    assert len(payload["questions"]) == 1
    assert payload["questions"][0]["question"] == "Pick"


def test_publish_and_persist_resource_event_swallows_append_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persist failures are logged but do not prevent the live SSE publish."""
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )
    store = MagicMock()
    store.append.side_effect = RuntimeError("db down")
    _publish_and_persist_resource_event(
        "conv_res",
        "session.resource.deleted",
        "term_1",
        "terminal",
        store,
    )
    assert published == [
        {
            "type": "session.resource.deleted",
            "resource_id": "term_1",
            "resource_type": "terminal",
            "session_id": "conv_res",
        }
    ]


class _UsageConversationStore:
    def __init__(self, conv: Conversation) -> None:
        self._conv = conv
        self.written: dict[str, dict[str, object]] = {}

    def get_conversation(self, session_id: str) -> Conversation | None:
        if session_id == self._conv.id:
            return self._conv
        return None

    def set_session_usage(self, session_id: str, usage: dict[str, object]) -> None:
        self.written[session_id] = usage

    def get_session_owner(self, session_id: str) -> str | None:
        del session_id
        return None


def test_accumulate_session_usage_noops_without_usage_dict() -> None:
    conv = Conversation(
        id="conv_usage",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_usage",
        agent_id="ag_u",
    )
    store = _UsageConversationStore(conv)
    assert _accumulate_session_usage({"status": "completed"}, "conv_usage", store) is None  # type: ignore[arg-type]
    assert store.written == {}


def test_accumulate_session_usage_persists_token_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv = Conversation(
        id="conv_usage",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_usage",
        agent_id="ag_u",
        session_usage={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
    )
    store = _UsageConversationStore(conv)
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda _model: None,
    )

    total = _accumulate_session_usage(
        {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 40,
                "total_tokens": 140,
                "model": "claude-sonnet-4-6",
            }
        },
        "conv_usage",
        store,  # type: ignore[arg-type]
    )

    assert total is None
    assert store.written["conv_usage"]["input_tokens"] == 105
    assert store.written["conv_usage"]["output_tokens"] == 42
    assert "total_cost_usd" not in store.written["conv_usage"]


def test_accumulate_session_usage_adds_priced_cost_when_catalog_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv = Conversation(
        id="conv_priced",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_priced",
        agent_id="ag_p",
    )
    store = _UsageConversationStore(conv)
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda _model: object(),
    )
    monkeypatch.setattr(
        "omnigent.llms.context_window.compute_llm_cost",
        lambda _usage, _pricing: 0.42,
    )

    total = _accumulate_session_usage(
        {
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "model": "claude-sonnet-4-6",
            }
        },
        "conv_priced",
        store,  # type: ignore[arg-type]
    )

    assert total == 0.42
    assert store.written["conv_priced"]["total_cost_usd"] == 0.42


def test_build_session_response_projects_usage_and_rejects_unbound_agent() -> None:
    conv = Conversation(
        id="conv_resp",
        created_at=1,
        updated_at=2,
        root_conversation_id="conv_resp",
        agent_id="ag_resp",
        session_usage={
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "total_cost_usd": 0.01,
        },
    )
    response = _build_session_response(
        conv,
        [],
        "running",
        permission_level=LEVEL_EDIT,
        llm_model="claude-sonnet-4-6",
        last_total_tokens=15,
    )
    assert response.id == "conv_resp"
    assert response.status == "running"
    assert response.llm_model == "claude-sonnet-4-6"
    assert response.total_cost_usd == 0.01
    assert response.permission_level == LEVEL_EDIT

    unbound = Conversation(
        id="conv_bad",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_bad",
        agent_id=None,
    )
    with pytest.raises(OmnigentError) as exc:
        _build_session_response(unbound, [], "idle")
    assert exc.value.code == ErrorCode.INTERNAL_ERROR


def test_pending_elicitation_snapshot_includes_child_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = Conversation(
        id="conv_parent",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_parent",
        agent_id="ag_p",
    )
    child = Conversation(
        id="conv_child",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_parent",
        parent_conversation_id="conv_parent",
        kind="sub_agent",
        agent_id="ag_c",
    )
    store = _DescendantStore({"conv_parent": [child]})

    def _snapshot(conv_id: str) -> list[dict[str, object]]:
        if conv_id == "conv_parent":
            return [{"elicitation_id": "elicit_parent", "params": {}}]
        return [{"elicitation_id": "elicit_child", "params": {}}]

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.pending_elicitations.snapshot_for",
        _snapshot,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.pending_elicitations.pending_session_ids",
        lambda: ["conv_parent", "conv_child"],
    )

    events = _pending_elicitation_snapshot_for_session(store, parent)  # type: ignore[arg-type]
    assert len(events) == 2
    assert events[0]["elicitation_id"] == "elicit_parent"
    assert events[1]["params"]["target_session_id"] == "conv_child"


def test_publish_elicitation_request_to_ancestors_mirrors_target_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Conversation(
        id="conv_root", created_at=0, updated_at=0, root_conversation_id="conv_root"
    )
    parent = Conversation(
        id="conv_parent",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_root",
        parent_conversation_id="conv_root",
    )
    child = Conversation(
        id="conv_child",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_root",
        parent_conversation_id="conv_parent",
    )
    store = _AncestorStore(
        {"conv_child": child, "conv_parent": parent, "conv_root": root}
    )
    published: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda sid, payload: published.append((sid, payload)),
    )

    _publish_elicitation_request_to_ancestors(
        store,  # type: ignore[arg-type]
        "conv_child",
        {"type": "response.elicitation_request", "params": {"tool": "Bash"}},
    )

    assert [sid for sid, _ in published] == ["conv_parent", "conv_root"]
    assert all(
        payload["params"]["target_session_id"] == "conv_child" for _, payload in published
    )


def test_publish_and_persist_resource_event_created_and_deleted_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )

    store = MagicMock()
    resource = {"id": "term_1", "name": "bash"}
    _publish_and_persist_resource_event(
        "conv_res",
        "session.resource.created",
        "term_1",
        "terminal",
        store,
        resource=resource,
    )
    _publish_and_persist_resource_event(
        "conv_res",
        "session.resource.deleted",
        "term_1",
        "terminal",
        store,
    )

    assert published[0]["type"] == "session.resource.created"
    assert published[0]["resource"] == resource
    assert published[1]["type"] == "session.resource.deleted"
    assert published[1]["resource_id"] == "term_1"
    assert store.append.call_count == 2


def test_resolve_llm_model_returns_none_when_store_or_agent_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv = Conversation(
        id="conv_missing",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_missing",
        agent_id="ag_missing",
    )
    cache = MagicMock()
    monkeypatch.setattr("omnigent.runtime.get_agent_cache", lambda: cache)

    monkeypatch.setattr("omnigent.runtime._globals._agent_store", None)
    assert _resolve_llm_model(conv) is None

    store = MagicMock()
    store.get.return_value = None
    monkeypatch.setattr("omnigent.runtime._globals._agent_store", store)
    assert _resolve_llm_model(conv) is None
    cache.load.assert_not_called()


# ── batch 45: external publish + status forward helpers ─────────────────────


def test_publish_elicitation_resolved_emits_resolved_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )
    _publish_elicitation_resolved("conv_elicit", "elicit_abc")
    assert published == [
        {
            "type": "response.elicitation_resolved",
            "elicitation_id": "elicit_abc",
        }
    ]


def test_publish_elicitation_resolved_to_ancestors_fans_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Conversation(
        id="conv_root", created_at=0, updated_at=0, root_conversation_id="conv_root"
    )
    parent = Conversation(
        id="conv_parent",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_root",
        parent_conversation_id="conv_root",
    )
    child = Conversation(
        id="conv_child",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_root",
        parent_conversation_id="conv_parent",
    )
    store = _AncestorStore({"conv_child": child, "conv_parent": parent, "conv_root": root})
    published: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda sid, payload: published.append((sid, payload)),
    )

    _publish_elicitation_resolved_to_ancestors(store, "conv_child", "elicit_xyz")  # type: ignore[arg-type]

    assert [sid for sid, _ in published] == ["conv_parent", "conv_root"]
    assert all(payload["elicitation_id"] == "elicit_xyz" for _, payload in published)


def test_publish_external_conversation_item_skips_meta_and_routes_user_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )

    meta_item = ConversationItem(
        id="msg_meta",
        type="message",
        status="completed",
        response_id="resp_meta",
        created_at=0,
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": "hidden"}],
            is_meta=True,
        ),
    )
    _publish_external_conversation_item("conv_ext", meta_item)
    assert published == []

    user_item = ConversationItem(
        id="msg_user",
        type="message",
        status="completed",
        response_id="resp_user",
        created_at=0,
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": "hello"}],
        ),
    )
    _publish_external_conversation_item("conv_ext", user_item, cleared_pending_id="pend_1")
    assert published[0]["type"] == "session.input.consumed"
    assert published[0]["data"]["cleared_pending_id"] == "pend_1"

    assistant_item = ConversationItem(
        id="msg_asst",
        type="message",
        status="completed",
        response_id="resp_asst",
        created_at=0,
        data=MessageData(
            role="assistant",
            agent="claude-native",
            content=[{"type": "output_text", "text": "done"}],
        ),
    )
    _publish_external_conversation_item("conv_ext", assistant_item)
    assert published[-1]["type"] == "response.output_item.done"
    assert published[-1]["item"]["id"] == "msg_asst"


def test_publish_external_output_text_delta_validates_and_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )

    body = SessionEventInput(
        type="external_output_text_delta",
        data={"delta": "partial", "message_id": "msg_1", "index": 2, "final": False},
    )
    _publish_external_output_text_delta("conv_delta", body)
    assert published[0]["type"] == "response.output_text.delta"
    assert published[0]["delta"] == "partial"
    assert published[0]["message_id"] == "msg_1"
    assert published[0]["index"] == 2
    assert published[0]["final"] is False

    with pytest.raises(OmnigentError):
        _publish_external_output_text_delta(
            "conv_delta",
            SessionEventInput(type="external_output_text_delta", data={"delta": 1}),
        )
    with pytest.raises(OmnigentError):
        _publish_external_output_text_delta(
            "conv_delta",
            SessionEventInput(
                type="external_output_text_delta",
                data={"delta": "x", "message_id": 42},
            ),
        )
    with pytest.raises(OmnigentError):
        _publish_external_output_text_delta(
            "conv_delta",
            SessionEventInput(
                type="external_output_text_delta",
                data={"delta": "x", "index": True},
            ),
        )
    with pytest.raises(OmnigentError):
        _publish_external_output_text_delta(
            "conv_delta",
            SessionEventInput(
                type="external_output_text_delta",
                data={"delta": "x", "final": "yes"},
            ),
        )


def test_parse_external_conversation_item_rejects_malformed_payloads() -> None:
    with pytest.raises(OmnigentError):
        _parse_external_conversation_item(
            SessionEventInput(
                type="external_conversation_item",
                data={"item_type": "message", "item_data": "not-a-dict"},
            )
        )
    with pytest.raises(OmnigentError):
        _parse_external_conversation_item(
            SessionEventInput(
                type="external_conversation_item",
                data={
                    "item_type": "message",
                    "item_data": {"role": "user", "content": []},
                    "response_id": "   ",
                },
            )
        )
    with pytest.raises(OmnigentError):
        _parse_external_conversation_item(
            SessionEventInput(
                type="external_conversation_item",
                data={
                    "item_type": "message",
                    "item_data": {"role": "assistant"},
                },
            )
        )


def test_parse_external_assistant_message_rejects_blank_response_id() -> None:
    with pytest.raises(OmnigentError):
        _parse_external_assistant_message(
            SessionEventInput(
                type="external_assistant_message",
                data={"agent": "codex", "text": "hi", "response_id": "  "},
            )
        )


def test_resolve_harness_reads_bound_agent_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv = Conversation(
        id="conv_harness",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_harness",
        agent_id="ag_harness",
    )
    agent = Agent(
        id="ag_harness", created_at=1, name="h", bundle_location="ag_harness/x"
    )
    spec = AgentSpec(
        spec_version=1,
        name="h",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )
    store = MagicMock()
    store.get.return_value = agent
    cache = MagicMock()
    cache.load.return_value = LoadedAgent(spec=spec, workdir=Path("/tmp/h"))

    monkeypatch.setattr("omnigent.runtime._globals._agent_store", store)
    monkeypatch.setattr("omnigent.runtime.get_agent_cache", lambda: cache)
    assert _resolve_harness(conv) == "claude-sdk"


def test_resolve_harness_returns_none_when_store_or_agent_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv = Conversation(
        id="conv_no_h",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_no_h",
        agent_id="ag_no_h",
    )
    monkeypatch.setattr("omnigent.runtime._globals._agent_store", None)
    assert _resolve_harness(conv) is None

    store = MagicMock()
    store.get.return_value = None
    monkeypatch.setattr("omnigent.runtime._globals._agent_store", store)
    assert _resolve_harness(conv) is None


def test_resolve_output_schema_returns_none_when_store_or_agent_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv = Conversation(
        id="conv_no_schema",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_no_schema",
        agent_id="ag_no_schema",
    )
    monkeypatch.setattr("omnigent.runtime._globals._agent_store", None)
    assert _resolve_output_schema(conv) is None

    store = MagicMock()
    store.get.return_value = None
    monkeypatch.setattr("omnigent.runtime._globals._agent_store", store)
    assert _resolve_output_schema(conv) is None


def test_usage_by_model_for_display_skips_malformed_buckets() -> None:
    projected = _usage_by_model_for_display(
        {
            "by_model": {
                "good": {"input_tokens": 10, "total_cost_usd": 0.1},
                "bad": "not-a-dict",
            }
        }
    )
    assert projected is not None
    assert set(projected) == {"good"}
    assert projected["good"].input_tokens == 10


def test_message_text_ignores_non_dict_blocks() -> None:
    assert _message_text(["not-a-block", {"type": "output_text", "text": "ok"}]) == "ok"
    assert _message_text(["bad"]) is None


class _AssistantTextStore:
    def list_items(
        self,
        session_id: str,
        *,
        limit: int,
        order: str,
        type: str,
    ) -> PagedList[ConversationItem]:
        del session_id, limit, order, type
        return PagedList(
            data=[
                ConversationItem(
                    id="msg_meta",
                    type="message",
                    status="completed",
                    response_id="resp_1",
                    created_at=0,
                    data=MessageData(
                        role="assistant",
                        agent="native",
                        content=[{"type": "output_text", "text": "hidden"}],
                        is_meta=True,
                    ),
                ),
                ConversationItem(
                    id="msg_asst",
                    type="message",
                    status="completed",
                    response_id="resp_1",
                    created_at=1,
                    data=MessageData(
                        role="assistant",
                        agent="native",
                        content=[{"type": "output_text", "text": "final answer"}],
                    ),
                ),
            ]
        )


def test_latest_assistant_text_from_store_skips_meta_and_returns_newest() -> None:
    store = _AssistantTextStore()
    assert _latest_assistant_text_from_store(store, "conv_scan") == "final answer"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_enrich_idle_status_with_subagent_output_attaches_text() -> None:
    store = _AssistantTextStore()
    enriched = await _enrich_idle_status_with_subagent_output(
        {"status": "idle"},
        "idle",
        "conv_child",
        store,  # type: ignore[arg-type]
    )
    assert enriched["output"] == "final answer"

    unchanged = await _enrich_idle_status_with_subagent_output(
        {"status": "running"},
        "running",
        "conv_child",
        store,  # type: ignore[arg-type]
    )
    assert "output" not in unchanged


def test_require_external_status_forward_raises_when_runner_missing_or_rejects() -> None:
    with pytest.raises(OmnigentError) as missing:
        _require_external_status_forward("conv_child", "idle", None)
    assert missing.value.code == ErrorCode.RUNNER_UNAVAILABLE

    with pytest.raises(OmnigentError) as rejected:
        _require_external_status_forward(
            "conv_child",
            "idle",
            _RunnerForwardResult(status_code=500, body="boom"),
        )
    assert rejected.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert "500" in str(rejected.value)


def test_drive_terminal_resolved_elicitation_records_calls_and_resolves_prompts() -> None:
    sessions_mod._recent_mirrored_tool_calls.clear()
    try:
        call_item = ConversationItem(
            id="fc_1",
            type="function_call",
            status="completed",
            response_id="resp_fc",
            created_at=0,
            data=FunctionCallData(
                agent="native",
                name="Bash",
                arguments='{"command": "ls"}',
                call_id="call_1",
            ),
        )
        _drive_terminal_resolved_elicitation("conv_term", call_item)
        mirrored = sessions_mod._recent_mirrored_tool_calls["call_1"]
        assert mirrored.tool_name == "Bash"
        assert mirrored.tool_input == {"command": "ls"}

        resolved = asyncio.Event()
        sessions_mod._harness_parked_elicitations["elicit_term"] = _ParkedHarnessElicitation(
            session_id="conv_term",
            tool_name="Bash",
            tool_input={"command": "ls"},
            resolved_elsewhere=resolved,
        )
        output_item = ConversationItem(
            id="fco_1",
            type="function_call_output",
            status="completed",
            response_id="resp_fc",
            created_at=1,
            data=FunctionCallOutputData(call_id="call_1", output="ok"),
        )
        _drive_terminal_resolved_elicitation("conv_term", output_item)
        assert resolved.is_set()
    finally:
        sessions_mod._recent_mirrored_tool_calls.clear()
        sessions_mod._harness_parked_elicitations.clear()


class _SubagentLookupStore(_DescendantStore):
    """Descendant store that also supports title-based lookup tests."""

    def __init__(self, children_by_parent: dict[str, list[Conversation]]) -> None:
        super().__init__(children_by_parent)


def test_find_claude_native_subagent_child_matches_label_across_pages() -> None:
    target = Conversation(
        id="conv_match",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_parent",
        parent_conversation_id="conv_parent",
        kind="sub_agent",
        labels={_CLAUDE_NATIVE_SUBAGENT_ID_LABEL_KEY: "sub_abc"},
    )
    filler = Conversation(
        id="conv_other",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_parent",
        parent_conversation_id="conv_parent",
        kind="sub_agent",
        labels={_CLAUDE_NATIVE_SUBAGENT_ID_LABEL_KEY: "sub_other"},
    )
    store = _SubagentLookupStore({"conv_parent": [filler, target]})
    found = _find_claude_native_subagent_child(store, "conv_parent", "sub_abc")  # type: ignore[arg-type]
    assert found is not None
    assert found.id == "conv_match"
    assert _find_claude_native_subagent_child(store, "conv_parent", "missing") is None  # type: ignore[arg-type]


def test_find_subagent_child_by_title_returns_exact_match() -> None:
    child = Conversation(
        id="conv_title",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_parent",
        parent_conversation_id="conv_parent",
        kind="sub_agent",
        title="Explore:sub_123",
    )
    store = _SubagentLookupStore({"conv_parent": [child]})
    assert (
        _find_subagent_child_by_title(store, "conv_parent", "Explore:sub_123").id  # type: ignore[arg-type,union-attr]
        == "conv_title"
    )
    assert _find_subagent_child_by_title(store, "conv_parent", "Explore:other") is None  # type: ignore[arg-type]


def test_publish_session_created_emits_parent_stream_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda sid, payload: published.append((sid, payload)),
    )
    _publish_session_created("conv_parent", "conv_child", "ag_parent")
    assert len(published) == 1
    sid, payload = published[0]
    assert sid == "conv_parent"
    assert payload["type"] == "session.created"
    assert payload["conversation_id"] == "conv_parent"
    assert payload["child_session_id"] == "conv_child"
    assert payload["agent_id"] == "ag_parent"
    assert payload["parent_session_id"] == "conv_parent"


# ── batch 47: publish helpers + agent classification ────────────────────────


def test_publish_runner_recovered_status_clears_sticky_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )
    sessions_mod._session_status_cache["conv_recover"] = "failed"

    _publish_runner_recovered_status("conv_recover")
    assert sessions_mod._session_status_cache["conv_recover"] == "idle"
    assert published[-1]["status"] == "idle"

    _publish_runner_recovered_status("conv_other")
    assert "conv_other" not in sessions_mod._session_status_cache


def test_publish_terminal_pending_updates_cache_and_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server.routes.sessions import _publish_terminal_pending

    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )

    _publish_terminal_pending("conv_term", True)
    assert sessions_mod._session_terminal_pending_cache["conv_term"] is True
    assert published[-1]["pending"] is True

    _publish_terminal_pending("conv_term", False)
    assert "conv_term" not in sessions_mod._session_terminal_pending_cache
    assert published[-1]["pending"] is False


def test_publish_sandbox_status_tracks_stage_and_clears_on_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )

    _publish_sandbox_status("conv_sbx", "provisioning")
    assert sessions_mod._session_sandbox_status_cache["conv_sbx"].stage == "provisioning"

    _publish_sandbox_status("conv_sbx", "failed", error="quota exceeded")
    assert sessions_mod._session_sandbox_status_cache["conv_sbx"].error == "quota exceeded"

    _publish_sandbox_status("conv_sbx", "ready")
    assert "conv_sbx" not in sessions_mod._session_sandbox_status_cache
    assert published[-1]["stage"] == "ready"


def test_publish_runner_skills_and_changed_files_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )

    _publish_runner_skills("conv_skills")
    assert published[-1]["type"] == "session.skills"

    _publish_changed_files_invalidated("conv_files", environment_id="env_a")
    assert published[-1]["type"] == "session.changed_files.invalidated"
    assert published[-1]["environment_id"] == "env_a"


def test_publish_interrupted_omits_response_id_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )

    _publish_interrupted("conv_int")
    assert published[-1]["type"] == "session.interrupted"
    data = published[-1]["data"]
    assert isinstance(data, dict)
    assert "response_id" not in data

    _publish_interrupted("conv_int", response_id="resp_codex")
    data_with_id = published[-1]["data"]
    assert isinstance(data_with_id, dict)
    assert data_with_id["response_id"] == "resp_codex"


def test_publish_error_event_and_policy_deny_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )

    _publish_error_event(
        "conv_err",
        ErrorData(source="execution", code="native_terminal_start_failed", message="boom"),
    )
    assert published[-1]["type"] == "response.error"
    assert published[-1]["error"]["code"] == "native_terminal_start_failed"

    _publish_policy_deny("conv_deny", "blocked by guardrail")
    assert published[-1]["type"] == "response.output_text.delta"
    assert "[Denied by policy: blocked by guardrail]" in str(published[-1]["delta"])
    assert str(published[-1]["message_id"]).startswith("deny_")


@pytest.mark.asyncio
async def test_persist_session_status_error_labels_upserts_and_clears() -> None:
    from omnigent.server.schemas import ErrorDetail

    store = MagicMock()
    error = ErrorDetail(code="runner_error", message="turn setup failed")

    await _persist_session_status_error_labels("conv_labels", error, store)  # type: ignore[arg-type]
    store.set_labels.assert_called_with(
        "conv_labels",
        {
            _LAST_TASK_ERROR_CODE_LABEL_KEY: "runner_error",
            _LAST_TASK_ERROR_MESSAGE_LABEL_KEY: "turn setup failed",
        },
    )

    store.reset_mock()
    await _persist_session_status_error_labels("conv_labels", None, store)  # type: ignore[arg-type]
    store.set_labels.assert_called_with(
        "conv_labels",
        {
            _LAST_TASK_ERROR_CODE_LABEL_KEY: "",
            _LAST_TASK_ERROR_MESSAGE_LABEL_KEY: "",
        },
    )


def _agent_with_harness(agent_id: str, harness: str) -> Agent:
    return Agent(id=agent_id, created_at=1, name=agent_id, bundle_location=f"{agent_id}/x")


def _patch_agent_cache(
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    *,
    harness_by_agent_id: dict[str, str] | None = None,
) -> None:
    def _loaded(agent_id: str, harness_value: str) -> LoadedAgent:
        spec = AgentSpec(
            spec_version=1,
            name="t",
            executor=ExecutorSpec(type="omnigent", config={"harness": harness_value}),
        )
        return LoadedAgent(spec=spec, workdir=Path("/tmp/t"))

    cache = MagicMock()
    if harness_by_agent_id is None:
        cache.load.return_value = _loaded("default", harness)
    else:
        cache.load.side_effect = lambda agent_id, _loc, **_: _loaded(
            agent_id,
            harness_by_agent_id[agent_id],
        )
    monkeypatch.setattr(sessions_mod, "get_agent_cache", lambda: cache)


def test_agent_provider_family_and_native_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_agent_cache(monkeypatch, "claude-native")
    claude_agent = _agent_with_harness("ag_claude", "claude-native")
    assert _agent_provider_family(claude_agent) == "anthropic"
    assert _agent_is_native(claude_agent) is True
    labels = _presentation_labels_for_agent(claude_agent)
    assert labels[_CLAUDE_NATIVE_WRAPPER_LABEL_KEY] == CLAUDE_NATIVE_WRAPPER_VALUE

    _patch_agent_cache(monkeypatch, "codex-native")
    codex_agent = _agent_with_harness("ag_codex", "codex-native")
    assert _agent_provider_family(codex_agent) == "openai"

    _patch_agent_cache(
        monkeypatch,
        "unused",
        harness_by_agent_id={
            "ag_claude": "claude-native",
            "ag_codex": "codex-native",
        },
    )
    assert _same_provider_family(claude_agent, codex_agent) is False

    _patch_agent_cache(monkeypatch, "claude-sdk")
    sdk_agent = _agent_with_harness("ag_sdk", "claude-sdk")
    assert _agent_is_native(sdk_agent) is False
    assert _presentation_labels_for_agent(sdk_agent) == {}


def test_same_provider_family_requires_both_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = _agent_with_harness("ag_a", "claude-sdk")
    b = _agent_with_harness("ag_b", "claude-sdk")
    _patch_agent_cache(monkeypatch, "claude-sdk")
    assert _same_provider_family(a, b) is True

    cache = MagicMock()
    cache.load.side_effect = OSError("missing bundle")
    monkeypatch.setattr(sessions_mod, "get_agent_cache", lambda: cache)
    assert _same_provider_family(a, b) is False


def test_registered_runner_id_validates_registry_and_ownership() -> None:
    router = MagicMock()
    router.runner_is_online.return_value = True
    router.runner_owner.return_value = "alice@example.com"

    assert _registered_runner_id(router, " runner_1 ", user_id="alice@example.com") == "runner_1"

    with pytest.raises(OmnigentError) as forbidden:
        _registered_runner_id(router, "runner_1", user_id="bob@example.com")
    assert forbidden.value.code == ErrorCode.FORBIDDEN

    with pytest.raises(OmnigentError):
        _registered_runner_id(None, "runner_1")


def test_latest_message_preview_skips_meta_and_truncates() -> None:
    items = [
        ConversationItem(
            id="msg_meta",
            type="message",
            status="completed",
            response_id="resp_1",
            created_at=0,
            data=MessageData(
                role="assistant",
                agent="native",
                content=[{"type": "output_text", "text": "hidden meta"}],
                is_meta=True,
            ),
        ),
        ConversationItem(
            id="msg_long",
            type="message",
            status="completed",
            response_id="resp_1",
            created_at=1,
            data=MessageData(
                role="assistant",
                agent="native",
                content=[{"type": "output_text", "text": "word " * 40}],
            ),
        ),
    ]
    preview = _latest_message_preview(items, limit_chars=20)
    assert preview is not None
    assert preview.endswith("…")
    assert len(preview) == 20


def test_title_content_from_item_only_returns_user_message_blocks() -> None:
    user_item = NewConversationItem(
        type="message",
        response_id="resp_title",
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": "Plan the refactor"}],
        ),
    )
    assert _title_content_from_item(user_item) == user_item.data.content

    assistant_item = NewConversationItem(
        type="message",
        response_id="resp_title",
        data=MessageData(
            role="assistant",
            agent="claude",
            content=[{"type": "output_text", "text": "Done"}],
        ),
    )
    assert _title_content_from_item(assistant_item) == []


def test_build_new_item_wraps_validated_event_data() -> None:
    body = SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        },
    )
    item = _build_new_item(body, "resp_new", created_by="alice@example.com")
    assert item.type == "message"
    assert item.response_id == "resp_new"
    assert item.created_by == "alice@example.com"
    assert item.data.role == "user"  # type: ignore[attr-defined]


# ── batch 48: terminal elicitation signal + deferred clear ───────────────────


def test_signal_terminal_resolved_noop_without_parked_candidates() -> None:
    sessions_mod._harness_parked_elicitations.clear()
    _signal_terminal_resolved_harness_elicitation("conv_none", "Bash", {"command": "ls"})
    assert sessions_mod._harness_parked_elicitations == {}


def test_signal_terminal_resolved_prefers_exact_tool_input_match() -> None:
    exact = asyncio.Event()
    other = asyncio.Event()
    sessions_mod._harness_parked_elicitations.clear()
    try:
        sessions_mod._harness_parked_elicitations["elicit_a"] = _ParkedHarnessElicitation(
            session_id="conv_sig",
            tool_name="Bash",
            tool_input={"command": "ls"},
            resolved_elsewhere=exact,
        )
        sessions_mod._harness_parked_elicitations["elicit_b"] = _ParkedHarnessElicitation(
            session_id="conv_sig",
            tool_name="Bash",
            tool_input={"command": "pwd"},
            resolved_elsewhere=other,
        )
        _signal_terminal_resolved_harness_elicitation(
            "conv_sig",
            "Bash",
            {"command": "ls"},
        )
        assert exact.is_set()
        assert not other.is_set()
    finally:
        sessions_mod._harness_parked_elicitations.clear()


def test_signal_terminal_resolved_single_candidate_without_exact_input() -> None:
    resolved = asyncio.Event()
    sessions_mod._harness_parked_elicitations.clear()
    try:
        sessions_mod._harness_parked_elicitations["elicit_only"] = _ParkedHarnessElicitation(
            session_id="conv_single",
            tool_name="Edit",
            tool_input={"path": "a.py"},
            resolved_elsewhere=resolved,
        )
        _signal_terminal_resolved_harness_elicitation(
            "conv_single",
            "Edit",
            {"path": "b.py"},
        )
        assert resolved.is_set()
    finally:
        sessions_mod._harness_parked_elicitations.clear()


def test_signal_terminal_resolved_stays_conservative_with_ambiguous_candidates() -> None:
    first = asyncio.Event()
    second = asyncio.Event()
    sessions_mod._harness_parked_elicitations.clear()
    try:
        sessions_mod._harness_parked_elicitations["elicit_1"] = _ParkedHarnessElicitation(
            session_id="conv_amb",
            tool_name="Bash",
            tool_input={"command": "ls"},
            resolved_elsewhere=first,
        )
        sessions_mod._harness_parked_elicitations["elicit_2"] = _ParkedHarnessElicitation(
            session_id="conv_amb",
            tool_name="Bash",
            tool_input={"command": "pwd"},
            resolved_elsewhere=second,
        )
        _signal_terminal_resolved_harness_elicitation(
            "conv_amb",
            "Bash",
            {"command": "whoami"},
        )
        assert not first.is_set()
        assert not second.is_set()
    finally:
        sessions_mod._harness_parked_elicitations.clear()


@pytest.mark.asyncio
async def test_schedule_deferred_elicitation_clear_publishes_after_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )

    async def _instant_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(sessions_mod.asyncio, "sleep", _instant_sleep)
    sessions_mod._harness_elicitation_registry.clear()
    _schedule_deferred_elicitation_clear("conv_defer", "elicit_defer", None)
    await asyncio.sleep(0)
    pending = list(sessions_mod._deferred_elicitation_clear_tasks)
    if pending:
        await asyncio.gather(*pending)

    assert published == [
        {
            "type": "response.elicitation_resolved",
            "elicitation_id": "elicit_defer",
        }
    ]


@pytest.mark.asyncio
async def test_schedule_deferred_elicitation_clear_skips_when_reparked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )

    async def _instant_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(sessions_mod.asyncio, "sleep", _instant_sleep)
    sessions_mod._harness_elicitation_registry["elicit_reparked"] = asyncio.get_running_loop().create_future()
    _schedule_deferred_elicitation_clear("conv_defer", "elicit_reparked", None)
    await asyncio.sleep(0)
    pending = list(sessions_mod._deferred_elicitation_clear_tasks)
    if pending:
        await asyncio.gather(*pending)

    assert published == []
    sessions_mod._harness_elicitation_registry.clear()


# ── batch 49: harness elicitation resolved-by-id ────────────────────────────


def test_signal_harness_elicitation_resolved_by_id_requires_id() -> None:
    with pytest.raises(OmnigentError) as exc:
        _signal_harness_elicitation_resolved_by_id("conv_a", "")
    assert exc.value.code == ErrorCode.INVALID_INPUT


def test_signal_harness_elicitation_resolved_by_id_rejects_cross_session_owner() -> None:
    elicitation_id = "elicit_codex_abc123def4567890abcdef12345678"
    sessions_mod._harness_elicitation_owners[elicitation_id] = "conv_other"
    try:
        with pytest.raises(OmnigentError) as exc:
            _signal_harness_elicitation_resolved_by_id("conv_a", elicitation_id)
        assert exc.value.code == ErrorCode.INVALID_INPUT
    finally:
        sessions_mod._harness_elicitation_owners.pop(elicitation_id, None)


def test_signal_harness_elicitation_resolved_by_id_pre_resolves_when_not_parked() -> None:
    elicitation_id = "elicit_codex_deadbeefdeadbeefdeadbeefdeadbeef"
    sessions_mod._harness_pre_resolved_elicitations.clear()
    sessions_mod._harness_parked_elicitations.clear()
    try:
        _signal_harness_elicitation_resolved_by_id("conv_pre", elicitation_id)
        tombstone = sessions_mod._harness_pre_resolved_elicitations[elicitation_id]
        assert tombstone.session_id == "conv_pre"
    finally:
        sessions_mod._harness_pre_resolved_elicitations.clear()


def test_signal_harness_elicitation_resolved_by_id_sets_parked_event() -> None:
    elicitation_id = "elicit_codex_cafebabecafebabecafebabecafebabe"
    resolved = asyncio.Event()
    sessions_mod._harness_parked_elicitations.clear()
    try:
        sessions_mod._harness_parked_elicitations[elicitation_id] = _ParkedHarnessElicitation(
            session_id="conv_parked",
            tool_name="Bash",
            tool_input={"command": "ls"},
            resolved_elsewhere=resolved,
        )
        _signal_harness_elicitation_resolved_by_id("conv_parked", elicitation_id)
        assert resolved.is_set()
        assert elicitation_id not in sessions_mod._harness_pre_resolved_elicitations
    finally:
        sessions_mod._harness_parked_elicitations.clear()


# ── batch 50: harness elicitation long-poll + disconnect poller ─────────────


def _clear_harness_elicitation_state() -> None:
    sessions_mod._harness_elicitation_registry.clear()
    sessions_mod._harness_elicitation_owners.clear()
    sessions_mod._harness_parked_elicitations.clear()
    sessions_mod._harness_pre_resolved_elicitations.clear()
    sessions_mod._deferred_elicitation_clear_tasks.clear()


@pytest.mark.asyncio
async def test_poll_request_disconnect_exits_on_http_disconnect() -> None:
    """``_poll_request_disconnect`` returns when the ASGI stack signals disconnect."""

    async def _receive() -> dict[str, str]:
        return {"type": "http.disconnect"}

    request = MagicMock()
    request.receive = _receive
    await _poll_request_disconnect(request)


@pytest.mark.asyncio
async def test_publish_and_wait_consumes_pre_resolved_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tombstone from a gap re-park returns the verdict without re-publishing."""
    published: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda sid, payload: published.append((sid, payload)),
    )
    elicitation_id = "elicit_batch50_tombstone1234567890abcdef12"
    verdict = ElicitationResult(action="decline")
    _clear_harness_elicitation_state()
    sessions_mod._harness_pre_resolved_elicitations[elicitation_id] = (
        _PreResolvedHarnessElicitation(
            session_id="conv_tomb",
            created_at=time.time(),
            result=verdict,
        )
    )
    try:
        result = await _publish_and_wait_for_harness_elicitation(
            MagicMock(),
            session_id="conv_tomb",
            params=ElicitationRequestParams(message="Allow rm?"),
            timeout_s=1.0,
            elicitation_id=elicitation_id,
        )
        assert result == verdict
        assert published == []
    finally:
        _clear_harness_elicitation_state()


@pytest.mark.asyncio
async def test_publish_and_wait_returns_web_verdict_and_publishes_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Web ``approval`` verdict completes the parked future and clears the card."""
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )

    async def _block_disconnect(_request: object) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(sessions_mod, "_poll_request_disconnect", _block_disconnect)

    elicitation_id = "elicit_batch50_webverdict1234567890abcdef"
    verdict = ElicitationResult(action="accept")
    _clear_harness_elicitation_state()

    async def _deliver_verdict() -> None:
        for _ in range(50):
            await asyncio.sleep(0.01)
            future = sessions_mod._harness_elicitation_registry.get(elicitation_id)
            if future is not None and not future.done():
                future.set_result(verdict)
                return
        raise AssertionError("elicitation future never registered")

    deliver_task = asyncio.create_task(_deliver_verdict())
    try:
        result = await _publish_and_wait_for_harness_elicitation(
            MagicMock(),
            session_id="conv_web",
            params=ElicitationRequestParams(message="Run tests?"),
            timeout_s=5.0,
            elicitation_id=elicitation_id,
            tool_name="Bash",
            tool_input={"command": "pytest"},
        )
        await deliver_task
        assert result == verdict
        event_types = [p["type"] for p in published]
        assert event_types == [
            "response.elicitation_request",
            "response.elicitation_resolved",
        ]
        assert published[0]["elicitation_id"] == elicitation_id
    finally:
        deliver_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await deliver_task
        _clear_harness_elicitation_state()


@pytest.mark.asyncio
async def test_publish_and_wait_returns_none_when_terminal_resolves_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal-side resolution ends the wait with ``None`` but still clears the card."""
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )

    async def _block_disconnect(_request: object) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(sessions_mod, "_poll_request_disconnect", _block_disconnect)

    elicitation_id = "elicit_batch50_terminal1234567890abcdef12"
    _clear_harness_elicitation_state()

    async def _resolve_in_terminal() -> None:
        for _ in range(50):
            await asyncio.sleep(0.01)
            parked = sessions_mod._harness_parked_elicitations.get(elicitation_id)
            if parked is not None:
                parked.resolved_elsewhere.set()
                return
        raise AssertionError("parked elicitation never registered")

    resolve_task = asyncio.create_task(_resolve_in_terminal())
    try:
        result = await _publish_and_wait_for_harness_elicitation(
            MagicMock(),
            session_id="conv_term",
            params=ElicitationRequestParams(message="Edit file?"),
            timeout_s=5.0,
            elicitation_id=elicitation_id,
            tool_name="Edit",
            tool_input={"path": "a.py"},
        )
        await resolve_task
        assert result is None
        assert [p["type"] for p in published] == [
            "response.elicitation_request",
            "response.elicitation_resolved",
        ]
    finally:
        resolve_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await resolve_task
        _clear_harness_elicitation_state()


@pytest.mark.asyncio
async def test_publish_and_wait_schedules_deferred_clear_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A severed wait without an answer defers the resolved event until the grace elapses."""
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )

    async def _block_disconnect(_request: object) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(sessions_mod, "_poll_request_disconnect", _block_disconnect)

    async def _instant_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(sessions_mod.asyncio, "sleep", _instant_sleep)
    monkeypatch.setattr(sessions_mod, "_HARNESS_ELICITATION_REPARK_GRACE_S", 0.0)

    elicitation_id = "elicit_batch50_timeout1234567890abcdef12"
    _clear_harness_elicitation_state()
    try:
        result = await _publish_and_wait_for_harness_elicitation(
            MagicMock(),
            session_id="conv_timeout",
            params=ElicitationRequestParams(message="Timed out?"),
            timeout_s=0.01,
            elicitation_id=elicitation_id,
        )
        assert result is None
        assert len(published) == 1
        assert published[0]["type"] == "response.elicitation_request"
        assert published[0]["elicitation_id"] == elicitation_id
        pending = list(sessions_mod._deferred_elicitation_clear_tasks)
        if pending:
            await asyncio.gather(*pending)
        assert published[-1] == {
            "type": "response.elicitation_resolved",
            "elicitation_id": elicitation_id,
        }
    finally:
        _clear_harness_elicitation_state()


@pytest.mark.asyncio
async def test_publish_and_wait_mirrors_request_to_ancestors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a store is supplied, ancestor streams receive the elicitation request."""
    mirrored: list[tuple[str, dict[str, object]]] = []

    def _mirror(
        store: object,
        session_id: str,
        payload: dict[str, object],
    ) -> None:
        mirrored.append((session_id, payload))

    monkeypatch.setattr(
        sessions_mod,
        "_publish_elicitation_request_to_ancestors",
        _mirror,
    )

    async def _block_disconnect(_request: object) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(sessions_mod, "_poll_request_disconnect", _block_disconnect)

    elicitation_id = "elicit_batch50_ancestor1234567890abcdef1"
    verdict = ElicitationResult(action="accept")
    store = MagicMock()
    _clear_harness_elicitation_state()

    async def _deliver_verdict() -> None:
        for _ in range(50):
            await asyncio.sleep(0.01)
            future = sessions_mod._harness_elicitation_registry.get(elicitation_id)
            if future is not None and not future.done():
                future.set_result(verdict)
                return
        raise AssertionError("elicitation future never registered")

    deliver_task = asyncio.create_task(_deliver_verdict())
    try:
        await _publish_and_wait_for_harness_elicitation(
            MagicMock(),
            session_id="conv_child",
            params=ElicitationRequestParams(message="Child prompt"),
            timeout_s=5.0,
            conversation_store=store,  # type: ignore[arg-type]
            elicitation_id=elicitation_id,
        )
        await deliver_task
        assert len(mirrored) == 1
        assert mirrored[0][0] == "conv_child"
        assert mirrored[0][1]["type"] == "response.elicitation_request"
    finally:
        deliver_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await deliver_task
        _clear_harness_elicitation_state()


# ── batch 52: native cumulative usage + subtree/ descendant edges ─────────────


def test_persist_native_cumulative_usage_noops_without_cumulative_fields() -> None:
    """No cumulative keys → no write and ``None`` return."""
    conv = Conversation(
        id="conv_native",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_native",
        agent_id="ag_n",
    )
    store = _UsageConversationStore(conv)
    assert _persist_native_cumulative_usage("conv_native", {}, store) is None  # type: ignore[arg-type]
    assert store.written == {}


def test_persist_native_cumulative_usage_persists_policy_cost_only() -> None:
    """``policy_cost_usd`` alone updates enforcement without repricing display."""
    conv = Conversation(
        id="conv_policy",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_policy",
        agent_id="ag_p",
        session_usage={"total_cost_usd": 0.10},
    )
    store = _UsageConversationStore(conv)
    result = _persist_native_cumulative_usage(
        "conv_policy",
        {"policy_cost_usd": 0.55},
        store,  # type: ignore[arg-type]
    )
    assert result == 0.10
    written = store.written["conv_policy"]
    assert written["policy_cost_usd"] == 0.55
    assert written["total_cost_usd"] == 0.10


def test_persist_native_cumulative_usage_splits_codex_cached_input_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cached input is peeled out of the input total before pricing."""
    conv = Conversation(
        id="conv_codex",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_codex",
        agent_id="ag_c",
    )
    store = _UsageConversationStore(conv)
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda _model: object(),
    )
    monkeypatch.setattr(
        "omnigent.llms.context_window.compute_llm_cost",
        lambda usage, _pricing: 1.23,
    )

    result = _persist_native_cumulative_usage(
        "conv_codex",
        {
            "cumulative_input_tokens": 100,
            "cumulative_output_tokens": 40,
            "cumulative_cache_read_input_tokens": 30,
            "model": "claude-sonnet-4-6",
        },
        store,  # type: ignore[arg-type]
    )

    assert result == 1.23
    written = store.written["conv_codex"]
    assert written["cache_read_input_tokens"] == 30
    assert written["input_tokens"] == 70
    assert written["output_tokens"] == 40
    assert written["total_tokens"] == 140
    assert written["total_cost_usd"] == 1.23
    assert written["by_model"]["claude-sonnet-4-6"]["input_tokens"] == 70


class _SubtreeCostStore:
    """Minimal store for ``load_session_usage`` + ancestor walks."""

    def __init__(self, convs: dict[str, Conversation]) -> None:
        self._convs = convs

    def get_conversation(self, conv_id: str) -> Conversation | None:
        return self._convs.get(conv_id)

    def list_conversations(
        self,
        *,
        root_conversation_id: str | None = None,
        limit: int = 100,
        after: str | None = None,
        kind: str | None = "default",
        **_: Any,
    ) -> PagedList[Conversation]:
        convs = [
            c
            for c in self._convs.values()
            if c.root_conversation_id == root_conversation_id
        ]
        if after is not None:
            ids = [c.id for c in convs]
            if after in ids:
                convs = convs[ids.index(after) + 1 :]
        page = convs[:limit]
        return PagedList(
            data=page,
            first_id=page[0].id if page else None,
            last_id=page[-1].id if page else None,
            has_more=len(convs) > limit,
        )


def test_publish_subtree_cost_to_ancestors_publishes_child_cost_to_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Child spend rolls into the ancestor subtree total and is broadcast."""
    published: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda sid, payload: published.append((sid, payload)),
    )
    parent = Conversation(
        id="conv_parent",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_parent",
        parent_conversation_id=None,
        session_usage={"input_tokens": 50},
    )
    child = Conversation(
        id="conv_child",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_parent",
        parent_conversation_id="conv_parent",
        session_usage={"total_cost_usd": 2.0, "input_tokens": 10},
    )
    store = _SubtreeCostStore({"conv_parent": parent, "conv_child": child})
    _publish_subtree_cost_to_ancestors(store, "conv_child")  # type: ignore[arg-type]
    assert len(published) == 1
    assert published[0][0] == "conv_parent"
    assert published[0][1]["type"] == "session.usage"
    assert published[0][1]["total_cost_usd"] == 2.0


def test_publish_subtree_cost_to_ancestors_skips_unpriced_subtree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ancestors whose subtree has no priced cost or per-model usage are skipped."""
    published: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda sid, payload: published.append((sid, payload)),
    )
    parent = Conversation(
        id="conv_parent",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_parent",
        parent_conversation_id=None,
        session_usage={"input_tokens": 50},
    )
    child = Conversation(
        id="conv_child",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_parent",
        parent_conversation_id="conv_parent",
        session_usage={"input_tokens": 10},
    )
    store = _SubtreeCostStore({"conv_parent": parent, "conv_child": child})
    _publish_subtree_cost_to_ancestors(store, "conv_child")  # type: ignore[arg-type]
    assert published == []


@pytest.mark.asyncio
async def test_publish_and_wait_mints_elicitation_id_when_unspecified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``elicitation_id=None`` registers a freshly minted correlation id."""
    published: list[str] = []

    async def _block_disconnect(_request: object) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(sessions_mod, "_poll_request_disconnect", _block_disconnect)
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload["elicitation_id"]),
    )

    verdict = ElicitationResult(action="accept")
    _clear_harness_elicitation_state()
    minted_holder: list[str] = []

    async def _resolve_first_registered() -> None:
        for _ in range(50):
            await asyncio.sleep(0.01)
            if sessions_mod._harness_elicitation_registry:
                elicitation_id = next(iter(sessions_mod._harness_elicitation_registry))
                minted_holder.append(elicitation_id)
                sessions_mod._harness_elicitation_registry[elicitation_id].set_result(verdict)
                return
        raise AssertionError("no elicitation registered")

    resolve_task = asyncio.create_task(_resolve_first_registered())
    try:
        result = await _publish_and_wait_for_harness_elicitation(
            MagicMock(),
            session_id="conv_mint",
            params=ElicitationRequestParams(message="Approve?"),
            timeout_s=5.0,
            elicitation_id=None,
        )
        await resolve_task
        assert result == verdict
        assert len(minted_holder) == 1
        assert minted_holder[0].startswith("elicit_")
        assert published[0] == minted_holder[0]
    finally:
        resolve_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await resolve_task
        _clear_harness_elicitation_state()


@pytest.mark.asyncio
async def test_schedule_deferred_elicitation_clear_mirrors_resolved_to_ancestors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deferred clear fans out ``elicitation_resolved`` to ancestor streams."""
    resolved: list[tuple[str, str]] = []

    def _mirror_resolved(
        store: object,
        session_id: str,
        elicitation_id: str,
    ) -> None:
        resolved.append((session_id, elicitation_id))

    monkeypatch.setattr(
        sessions_mod,
        "_publish_elicitation_resolved_to_ancestors",
        _mirror_resolved,
    )
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, _payload: None,
    )

    async def _instant_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(sessions_mod.asyncio, "sleep", _instant_sleep)
    store = MagicMock()
    _clear_harness_elicitation_state()
    _schedule_deferred_elicitation_clear("conv_child", "elicit_anc", store)
    pending = list(sessions_mod._deferred_elicitation_clear_tasks)
    if pending:
        await asyncio.gather(*pending)
    assert resolved == [("conv_child", "elicit_anc")]


class _PaginatedDescendantStore:
    def __init__(self, children_by_parent: dict[str, list[Conversation]]) -> None:
        self._children = children_by_parent

    def list_conversations(
        self,
        *,
        kind: str | None = None,
        parent_conversation_id: str | None = None,
        limit: int = 100,
        after: str | None = None,
        **_: Any,
    ) -> PagedList[Conversation]:
        assert kind == "sub_agent"
        assert parent_conversation_id is not None
        kids = list(self._children.get(parent_conversation_id, []))
        if after is not None:
            ids = [c.id for c in kids]
            if after in ids:
                kids = kids[ids.index(after) + 1 :]
        page = kids[:limit]
        return PagedList(
            data=page,
            first_id=page[0].id if page else None,
            last_id=page[-1].id if page else None,
            has_more=len(kids) > limit,
        )


def test_descendant_sessions_paginates_and_deduplicates_seen_ids() -> None:
    """Pagination continues after ``has_more`` and skips already-seen child ids."""
    child_a = Conversation(
        id="conv_a",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_root",
        parent_conversation_id="conv_root",
        kind="sub_agent",
    )
    child_b = Conversation(
        id="conv_b",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_root",
        parent_conversation_id="conv_root",
        kind="sub_agent",
    )
    child_c = Conversation(
        id="conv_c",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_root",
        parent_conversation_id="conv_root",
        kind="sub_agent",
    )
    store = _PaginatedDescendantStore(
        {"conv_root": [child_a, child_a, child_b, child_c]},
    )
    descendants = _descendant_sessions(store, "conv_root")  # type: ignore[arg-type]
    assert [d.id for d in descendants] == ["conv_a", "conv_b", "conv_c"]


def test_pending_elicitation_snapshot_skips_duplicate_child_elicitation_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Child prompts already mirrored on the parent are not duplicated."""
    parent = Conversation(
        id="conv_parent",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_parent",
        agent_id="ag_p",
    )
    child = Conversation(
        id="conv_child",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_parent",
        parent_conversation_id="conv_parent",
        kind="sub_agent",
        agent_id="ag_c",
    )
    store = _DescendantStore({"conv_parent": [child]})

    def _snapshot(conv_id: str) -> list[dict[str, object]]:
        del conv_id
        return [{"elicitation_id": "elicit_shared", "params": {"tool": "Bash"}}]

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.pending_elicitations.snapshot_for",
        _snapshot,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.pending_elicitations.pending_session_ids",
        lambda: ["conv_parent", "conv_child"],
    )

    events = _pending_elicitation_snapshot_for_session(store, parent)  # type: ignore[arg-type]
    assert len(events) == 1
    assert events[0]["elicitation_id"] == "elicit_shared"


# ── batch 53: subtree usage_by_model publish + snapshot fast-path ─────────────


def test_publish_subtree_cost_to_ancestors_publishes_usage_by_model_without_flat_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-model token usage alone triggers a broadcast when flat cost is absent."""
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )
    parent = Conversation(
        id="conv_parent",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_parent",
        parent_conversation_id=None,
    )
    child = Conversation(
        id="conv_child",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_parent",
        parent_conversation_id="conv_parent",
        session_usage={
            "by_model": {"claude-sonnet-4-6": {"input_tokens": 120, "output_tokens": 30}},
        },
    )
    store = _SubtreeCostStore({"conv_parent": parent, "conv_child": child})
    _publish_subtree_cost_to_ancestors(store, "conv_child")  # type: ignore[arg-type]
    assert len(published) == 1
    assert published[0]["type"] == "session.usage"
    assert "total_cost_usd" not in published[0]
    assert "usage_by_model" in published[0]


def test_pending_elicitation_snapshot_skips_descendant_walk_when_only_self_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No other pending sessions → return parent snapshot without listing children."""
    parent = Conversation(
        id="conv_parent",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_parent",
        agent_id="ag_p",
    )
    store = MagicMock()

    def _must_not_walk_descendants(*_args: object, **_kwargs: object) -> list[Conversation]:
        raise AssertionError("descendant walk should be skipped")

    monkeypatch.setattr(sessions_mod, "_descendant_sessions", _must_not_walk_descendants)
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.pending_elicitations.snapshot_for",
        lambda conv_id: [{"elicitation_id": f"elicit_{conv_id}", "params": {}}],
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.pending_elicitations.pending_session_ids",
        lambda: ["conv_parent"],
    )

    events = _pending_elicitation_snapshot_for_session(store, parent)  # type: ignore[arg-type]
    assert len(events) == 1
    assert events[0]["elicitation_id"] == "elicit_conv_parent"


# ── batch 54: accumulate zero-token noop, pagination, external persist ────────


def test_accumulate_session_usage_noops_when_all_token_counts_zero() -> None:
    """Zero input/output/total tokens → no store write."""
    conv = Conversation(
        id="conv_zero",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_zero",
        agent_id="ag_z",
    )
    store = _UsageConversationStore(conv)
    assert _accumulate_session_usage(
        {"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}},
        "conv_zero",
        store,  # type: ignore[arg-type]
    ) is None
    assert store.written == {}


def test_descendant_sessions_continues_when_page_has_more_and_last_id() -> None:
    """``after = page.last_id`` advances pagination across multiple pages."""
    child_a = Conversation(
        id="conv_a",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_root",
        parent_conversation_id="conv_root",
        kind="sub_agent",
    )
    child_b = Conversation(
        id="conv_b",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_root",
        parent_conversation_id="conv_root",
        kind="sub_agent",
    )

    class _OnePerPageStore:
        def list_conversations(
            self,
            *,
            kind: str | None = None,
            parent_conversation_id: str | None = None,
            limit: int = 100,
            after: str | None = None,
            **_: Any,
        ) -> PagedList[Conversation]:
            assert kind == "sub_agent"
            if parent_conversation_id != "conv_root":
                return PagedList(data=[], first_id=None, last_id=None, has_more=False)
            kids = [child_a, child_b]
            if after is not None:
                ids = [c.id for c in kids]
                kids = kids[ids.index(after) + 1 :]
            page = kids[:1]
            return PagedList(
                data=page,
                first_id=page[0].id if page else None,
                last_id=page[-1].id if page else None,
                has_more=len(kids) > 1,
            )

    descendants = _descendant_sessions(_OnePerPageStore(), "conv_root")  # type: ignore[arg-type]
    assert [d.id for d in descendants] == ["conv_a", "conv_b"]


class _ExternalUsageStore:
    """Store for external session-usage / model-change helpers."""

    def __init__(self, conv: Conversation) -> None:
        self._conv = conv
        self._convs = {conv.id: conv}
        self.usage_written: dict[str, dict[str, object]] = {}
        self.labels_written: dict[str, dict[str, str]] = {}
        self.updated: list[tuple[str, dict[str, object]]] = []
        self.appended: list[tuple[str, list[ConversationItem]]] = []

    def get_conversation(self, session_id: str) -> Conversation | None:
        return self._convs.get(session_id)

    def set_session_usage(self, session_id: str, usage: dict[str, object]) -> None:
        self.usage_written[session_id] = usage
        conv = self._convs.get(session_id)
        if conv is not None:
            conv.session_usage = usage  # type: ignore[assignment]

    def set_labels(self, session_id: str, labels: dict[str, str]) -> None:
        self.labels_written[session_id] = labels

    def list_conversations(
        self,
        *,
        root_conversation_id: str | None = None,
        **_: Any,
    ) -> PagedList[Conversation]:
        convs = [
            c for c in self._convs.values() if c.root_conversation_id == root_conversation_id
        ]
        return PagedList(
            data=convs,
            first_id=convs[0].id if convs else None,
            last_id=convs[-1].id if convs else None,
            has_more=False,
        )

    def update_conversation(self, session_id: str, **kwargs: object) -> None:
        self.updated.append((session_id, kwargs))
        conv = self._convs[session_id]
        if "model_override" in kwargs:
            conv.model_override = str(kwargs["model_override"])

    def append(
        self,
        session_id: str,
        items: list[NewConversationItem],
    ) -> list[ConversationItem]:
        persisted = [
            ConversationItem(
                id=f"item_{len(self.appended)}",
                created_at=1,
                type=item.type,
                status="completed",
                response_id=item.response_id,
                data=item.data,
            )
            for item in items
        ]
        self.appended.append((session_id, persisted))
        return persisted

    def get_session_owner(self, session_id: str) -> str | None:
        del session_id
        return None


@pytest.mark.asyncio
async def test_persist_external_session_usage_rejects_invalid_context_tokens() -> None:
    conv = Conversation(
        id="conv_ext",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_ext",
        agent_id="ag_e",
    )
    store = _ExternalUsageStore(conv)
    body = SessionEventInput(type="external_session_usage", data={"context_tokens": -1})
    with pytest.raises(OmnigentError) as exc:
        await _persist_external_session_usage("conv_ext", body, store)  # type: ignore[arg-type]
    assert exc.value.code == ErrorCode.INVALID_INPUT


@pytest.mark.asyncio
async def test_persist_external_session_usage_rejects_invalid_context_window() -> None:
    conv = Conversation(
        id="conv_ext",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_ext",
        agent_id="ag_e",
    )
    store = _ExternalUsageStore(conv)
    body = SessionEventInput(type="external_session_usage", data={"context_window": 0})
    with pytest.raises(OmnigentError) as exc:
        await _persist_external_session_usage("conv_ext", body, store)  # type: ignore[arg-type]
    assert exc.value.code == ErrorCode.INVALID_INPUT


@pytest.mark.asyncio
async def test_persist_external_session_usage_requires_at_least_one_field() -> None:
    conv = Conversation(
        id="conv_ext",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_ext",
        agent_id="ag_e",
    )
    store = _ExternalUsageStore(conv)
    body = SessionEventInput(type="external_session_usage", data={})
    with pytest.raises(OmnigentError) as exc:
        await _persist_external_session_usage("conv_ext", body, store)  # type: ignore[arg-type]
    assert "requires at least one" in str(exc.value)


@pytest.mark.asyncio
async def test_persist_external_session_usage_publishes_context_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Context-only update sets labels and broadcasts tokens without zeroing cost."""
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )
    conv = Conversation(
        id="conv_ext",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_ext",
        agent_id="ag_e",
        session_usage={"input_tokens": 10, "total_cost_usd": 0.5},
    )
    store = _ExternalUsageStore(conv)
    body = SessionEventInput(
        type="external_session_usage",
        data={"context_tokens": 42000, "context_window": 200000},
    )
    result = await _persist_external_session_usage("conv_ext", body, store)  # type: ignore[arg-type]
    assert result == 42000
    assert store.labels_written["conv_ext"]["omnigent.last_context_tokens"] == "42000"
    assert store.labels_written["conv_ext"]["omnigent.last_context_window"] == "200000"
    assert len(published) == 1
    assert published[0]["type"] == "session.usage"
    assert published[0]["context_tokens"] == 42000
    assert published[0]["context_window"] == 200000
    assert published[0]["total_cost_usd"] == 0.5


@pytest.mark.asyncio
async def test_persist_external_session_usage_accepts_policy_cost_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``policy_cost_usd`` alone satisfies the cumulative-field requirement."""
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )
    conv = Conversation(
        id="conv_policy_ext",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_policy_ext",
        agent_id="ag_pe",
    )
    store = _ExternalUsageStore(conv)
    body = SessionEventInput(
        type="external_session_usage",
        data={"policy_cost_usd": 1.25},
    )
    result = await _persist_external_session_usage("conv_policy_ext", body, store)  # type: ignore[arg-type]
    assert result is None
    assert store.usage_written["conv_policy_ext"]["policy_cost_usd"] == 1.25
    assert published[-1]["type"] == "session.usage"


@pytest.mark.asyncio
async def test_persist_external_model_change_rejects_empty_model() -> None:
    conv = Conversation(
        id="conv_model",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_model",
        agent_id="ag_m",
    )
    store = _ExternalUsageStore(conv)
    body = SessionEventInput(type="external_model_change", data={"model": "   "})
    with pytest.raises(OmnigentError) as exc:
        await _persist_external_model_change("conv_model", conv, body, store)  # type: ignore[arg-type]
    assert exc.value.code == ErrorCode.INVALID_INPUT


@pytest.mark.asyncio
async def test_persist_external_model_change_noops_when_already_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )
    conv = Conversation(
        id="conv_model",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_model",
        agent_id="ag_m",
        model_override="claude-sonnet-4-6",
    )
    store = _ExternalUsageStore(conv)
    body = SessionEventInput(
        type="external_model_change",
        data={"model": "claude-sonnet-4-6"},
    )
    await _persist_external_model_change("conv_model", conv, body, store)  # type: ignore[arg-type]
    assert store.updated == []
    assert published == []


@pytest.mark.asyncio
async def test_persist_external_model_change_updates_and_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )
    conv = Conversation(
        id="conv_model",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_model",
        agent_id="ag_m",
    )
    store = _ExternalUsageStore(conv)
    body = SessionEventInput(
        type="external_model_change",
        data={"model": "  databricks-gpt-5-4  "},
    )
    await _persist_external_model_change("conv_model", conv, body, store)  # type: ignore[arg-type]
    assert store.updated == [("conv_model", {"model_override": "databricks-gpt-5-4"})]
    assert len(published) == 1
    assert published[0]["type"] == "session.model"
    assert published[0]["conversation_id"] == "conv_model"
    assert published[0]["model"] == "databricks-gpt-5-4"


@pytest.mark.asyncio
async def test_persist_model_change_note_appends_system_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )
    conv = Conversation(
        id="conv_note",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_note",
        agent_id="ag_n",
    )
    store = _ExternalUsageStore(conv)
    await _persist_model_change_note("conv_note", "claude-opus-4-6", store)  # type: ignore[arg-type]
    assert len(store.appended) == 1
    item = store.appended[0][1][0]
    assert item.type == "message"
    assert isinstance(item.data, MessageData)
    assert item.data.role == "user"
    text = item.data.content[0]["text"]
    assert "model changed to claude-opus-4-6" in text
    assert published[-1]["type"] == "session.input.consumed"


@pytest.mark.asyncio
async def test_persist_model_change_note_records_reset_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )
    conv = Conversation(
        id="conv_reset",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_reset",
        agent_id="ag_r",
    )
    store = _ExternalUsageStore(conv)
    await _persist_model_change_note("conv_reset", None, store)  # type: ignore[arg-type]
    item = store.appended[0][1][0]
    assert isinstance(item.data, MessageData)
    assert "model reset to the agent default" in item.data.content[0]["text"]


# ── batch 55: approval forward + resolve_elicitation + usage_by_model ─────────


class _CaptureRunnerClient:
    """Records runner POSTs for approval-forward tests."""

    def __init__(self, captured: dict[str, object], *, fail: bool = False) -> None:
        self._captured = captured
        self._fail = fail

    async def post(self, path: str, *, json: dict[str, object], **_: Any) -> object:
        if self._fail:
            import httpx

            raise httpx.HTTPError("runner down")
        self._captured["path"] = path
        self._captured["body"] = json

        class _Resp:
            status_code = 202
            headers: dict[str, str] = {}
            text = ""

        return _Resp()


@pytest.mark.asyncio
async def test_forward_approval_to_runner_noops_without_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _no_client(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _no_client)
    await _forward_approval_to_runner(
        "conv_fwd",
        {"elicitation_id": "elicit_x", "action": "accept"},
        MagicMock(),
    )


@pytest.mark.asyncio
async def test_forward_approval_to_runner_posts_canonical_approval_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _client(*_args: object, **_kwargs: object) -> _CaptureRunnerClient:
        return _CaptureRunnerClient(captured)

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _client)
    payload = {"elicitation_id": "elicit_fwd", "action": "accept", "content": {"ok": True}}
    await _forward_approval_to_runner("conv_fwd", payload, MagicMock())
    assert captured["path"] == "/v1/sessions/conv_fwd/events"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["type"] == "approval"
    assert body["data"] == payload


@pytest.mark.asyncio
async def test_forward_approval_to_runner_swallows_http_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _failing_client(*_args: object, **_kwargs: object) -> _CaptureRunnerClient:
        return _CaptureRunnerClient(captured, fail=True)

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _failing_client)
    await _forward_approval_to_runner(
        "conv_fwd",
        {"elicitation_id": "elicit_fail", "action": "decline"},
        MagicMock(),
    )
    assert captured == {}


@pytest.mark.asyncio
async def test_resolve_elicitation_sets_owned_harness_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )
    async def _no_runner(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _no_runner)
    _clear_harness_elicitation_state()
    elicitation_id = "elicit_resolve_owned1234567890abcdef12"
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    sessions_mod._harness_elicitation_registry[elicitation_id] = future
    sessions_mod._harness_elicitation_owners[elicitation_id] = "conv_resolve"
    try:
        await _resolve_elicitation(
            "conv_resolve",
            {"elicitation_id": elicitation_id, "action": "accept"},
            None,
        )
        assert future.done()
        assert future.result().action == "accept"
        assert published[-1]["type"] == "response.elicitation_resolved"
        assert published[-1]["elicitation_id"] == elicitation_id
    finally:
        _clear_harness_elicitation_state()


@pytest.mark.asyncio
async def test_resolve_elicitation_skips_future_on_owner_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )
    async def _no_runner(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _no_runner)
    _clear_harness_elicitation_state()
    elicitation_id = "elicit_resolve_mismatch1234567890abcdef"
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    sessions_mod._harness_elicitation_registry[elicitation_id] = future
    sessions_mod._harness_elicitation_owners[elicitation_id] = "conv_owner"
    try:
        await _resolve_elicitation(
            "conv_intruder",
            {"elicitation_id": elicitation_id, "action": "accept"},
            None,
        )
        assert not future.done()
        assert published[-1]["elicitation_id"] == elicitation_id
    finally:
        _clear_harness_elicitation_state()


@pytest.mark.asyncio
async def test_resolve_elicitation_tombstones_unparked_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )
    async def _no_runner(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _no_runner)
    _clear_harness_elicitation_state()
    elicitation_id = "elicit_resolve_tomb1234567890abcdef1234"
    try:
        await _resolve_elicitation(
            "conv_tomb",
            {"elicitation_id": elicitation_id, "action": "decline"},
            None,
        )
        tombstone = sessions_mod._harness_pre_resolved_elicitations[elicitation_id]
        assert tombstone.session_id == "conv_tomb"
        assert tombstone.result.action == "decline"
    finally:
        _clear_harness_elicitation_state()


@pytest.mark.asyncio
async def test_resolve_elicitation_ignores_invalid_payload_for_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _no_runner(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _no_runner)
    _clear_harness_elicitation_state()
    elicitation_id = "elicit_resolve_bad1234567890abcdef12345"
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    sessions_mod._harness_elicitation_registry[elicitation_id] = future
    sessions_mod._harness_elicitation_owners[elicitation_id] = "conv_bad"
    try:
        await _resolve_elicitation(
            "conv_bad",
            {"elicitation_id": elicitation_id, "action": "not_a_real_action"},
            None,
        )
        assert not future.done()
        assert elicitation_id not in sessions_mod._harness_pre_resolved_elicitations
    finally:
        _clear_harness_elicitation_state()


@pytest.mark.asyncio
async def test_resolve_elicitation_mirrors_resolved_to_ancestors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mirrored: list[tuple[str, str]] = []

    def _mirror(
        store: object,
        session_id: str,
        elicitation_id: str,
    ) -> None:
        mirrored.append((session_id, elicitation_id))

    monkeypatch.setattr(
        sessions_mod,
        "_publish_elicitation_resolved_to_ancestors",
        _mirror,
    )
    async def _no_runner(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _no_runner)
    store = MagicMock()
    await _resolve_elicitation(
        "conv_child",
        {"elicitation_id": "elicit_anc", "action": "accept"},
        None,
        conversation_store=store,
    )
    assert mirrored == [("conv_child", "elicit_anc")]


@pytest.mark.asyncio
async def test_resolve_elicitation_empty_id_skips_resolved_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )
    async def _no_runner(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _no_runner)
    await _resolve_elicitation("conv_empty", {"action": "accept"}, None)
    assert published == []


@pytest.mark.asyncio
async def test_persist_external_session_usage_includes_usage_by_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subtree per-model tokens appear on the broadcast when flat cost is absent."""
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )
    conv = Conversation(
        id="conv_by_model",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_by_model",
        agent_id="ag_bm",
        session_usage={
            "by_model": {
                "claude-sonnet-4-6": {"input_tokens": 200, "output_tokens": 50},
            },
        },
    )
    store = _ExternalUsageStore(conv)
    body = SessionEventInput(
        type="external_session_usage",
        data={"policy_cost_usd": 0.75},
    )
    await _persist_external_session_usage("conv_by_model", body, store)  # type: ignore[arg-type]
    assert len(published) == 1
    assert published[0]["type"] == "session.usage"
    assert "total_cost_usd" not in published[0]
    assert "usage_by_model" in published[0]
    by_model = published[0]["usage_by_model"]
    assert isinstance(by_model, dict)
    assert "claude-sonnet-4-6" in by_model


# ── batch 56: native popup forward + hold_native_ask_gate ─────────────────────


@pytest.mark.asyncio
async def test_spawn_native_approval_popup_forward_posts_cost_approval_popup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fire-and-forget task forwards ``cost_approval_popup`` to the runner."""
    forwarded: list[dict[str, object]] = []

    async def _capture_forward(
        session_id: str,
        _router: object,
        event: dict[str, object],
    ) -> None:
        forwarded.append({"session_id": session_id, "event": event})

    monkeypatch.setattr(
        sessions_mod,
        "_forward_session_change_to_runner",
        _capture_forward,
    )
    _spawn_native_approval_popup_forward(
        "conv_popup",
        "elicit_pop",
        "Approve deletion?",
        "cost_budget",
    )
    pending = list(sessions_mod._native_popup_forward_tasks)
    if pending:
        await asyncio.gather(*pending)
    assert len(forwarded) == 1
    assert forwarded[0]["session_id"] == "conv_popup"
    event = forwarded[0]["event"]
    assert isinstance(event, dict)
    assert event["type"] == "cost_approval_popup"
    assert event["elicitation_id"] == "elicit_pop"
    assert event["message"] == "Approve deletion?"
    assert event["policy_name"] == "cost_budget"


@pytest.mark.asyncio
async def test_hold_native_ask_gate_returns_true_and_applies_side_effects_on_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approved ASK applies withheld labels and state updates."""
    labels_applied: list[dict[str, str]] = []
    states_applied: list[object] = []
    popup_calls: list[tuple[str, str, str, str | None]] = []

    def _record_popup(
        session_id: str,
        elicitation_id: str,
        message: str,
        policy_name: str | None = None,
    ) -> None:
        popup_calls.append((session_id, elicitation_id, message, policy_name))

    async def _accept_verdict(*_args: object, **_kwargs: object) -> ElicitationResult:
        return ElicitationResult(action="accept")

    monkeypatch.setattr(sessions_mod, "_spawn_native_approval_popup_forward", _record_popup)
    monkeypatch.setattr(
        sessions_mod,
        "_publish_and_wait_for_harness_elicitation",
        _accept_verdict,
    )
    monkeypatch.setattr(sessions_mod, "resolve_ask_timeout", lambda _e, _r: 45)

    engine = MagicMock()
    engine.apply_label_writes = lambda labels: labels_applied.append(labels)
    engine.apply_state_updates = lambda updates: states_applied.append(updates)

    ask_result = PolicyResult(
        action=PolicyAction.ASK,
        reason="Deleting files requires approval",
        deciding_policy="delete_guard",
        set_labels={"integrity": "0"},
        state_updates=[{"key": "calls", "action": "increment", "value": 1}],  # type: ignore[list-item]
    )

    approved = await _hold_native_ask_gate(
        MagicMock(),
        session_id="conv_gate",
        phase=Phase.TOOL_CALL,
        data={"name": "Bash", "arguments": {"command": "rm -rf /"}},
        engine=engine,
        result=ask_result,
        conversation_store=MagicMock(),
    )

    assert approved is True
    assert labels_applied == [{"integrity": "0"}]
    assert len(states_applied) == 1
    assert len(popup_calls) == 1
    assert popup_calls[0][0] == "conv_gate"
    assert popup_calls[0][2] == "Deleting files requires approval"
    assert popup_calls[0][3] == "delete_guard"


@pytest.mark.asyncio
async def test_hold_native_ask_gate_returns_false_on_decline_without_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declined ASK fails closed and does not apply withheld writes."""
    labels_applied: list[dict[str, str]] = []

    async def _decline_verdict(*_args: object, **_kwargs: object) -> ElicitationResult:
        return ElicitationResult(action="decline")

    monkeypatch.setattr(sessions_mod, "_spawn_native_approval_popup_forward", lambda *_a, **_k: None)
    monkeypatch.setattr(
        sessions_mod,
        "_publish_and_wait_for_harness_elicitation",
        _decline_verdict,
    )
    monkeypatch.setattr(sessions_mod, "resolve_ask_timeout", lambda _e, _r: 30)

    engine = MagicMock()
    engine.apply_label_writes = lambda labels: labels_applied.append(labels)

    ask_result = PolicyResult(
        action=PolicyAction.ASK,
        reason="blocked",
        deciding_policy="guard",
        set_labels={"blocked": "1"},
    )

    approved = await _hold_native_ask_gate(
        MagicMock(),
        session_id="conv_deny",
        phase=Phase.REQUEST,
        data={"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        engine=engine,
        result=ask_result,
        conversation_store=MagicMock(),
    )

    assert approved is False
    assert labels_applied == []


@pytest.mark.asyncio
async def test_hold_native_ask_gate_returns_false_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeout / disconnect (``None`` verdict) fails closed."""
    monkeypatch.setattr(sessions_mod, "_spawn_native_approval_popup_forward", lambda *_a, **_k: None)

    async def _no_verdict(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        sessions_mod,
        "_publish_and_wait_for_harness_elicitation",
        _no_verdict,
    )
    monkeypatch.setattr(sessions_mod, "resolve_ask_timeout", lambda _e, _r: 5)

    approved = await _hold_native_ask_gate(
        MagicMock(),
        session_id="conv_timeout",
        phase=Phase.TOOL_CALL,
        data={"name": "Bash", "arguments": {}},
        engine=MagicMock(),
        result=PolicyResult(action=PolicyAction.ASK, reason="wait", deciding_policy="p"),
        conversation_store=MagicMock(),
    )
    assert approved is False


@pytest.mark.asyncio
async def test_resolve_elicitation_invalid_tombstone_payload_is_not_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unparked resolve with malformed verdict does not create a tombstone."""
    async def _no_runner(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _no_runner)
    _clear_harness_elicitation_state()
    elicitation_id = "elicit_invalid_tomb1234567890abcdef"
    try:
        await _resolve_elicitation(
            "conv_bad_tomb",
            {"elicitation_id": elicitation_id, "action": "not_valid"},
            None,
        )
        assert elicitation_id not in sessions_mod._harness_pre_resolved_elicitations
    finally:
        _clear_harness_elicitation_state()