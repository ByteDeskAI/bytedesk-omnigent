"""Unit tests for pure helper functions in ``sessions.py``.

These helpers are extracted at the module boundary for batch permission
resolution, SSE formatting, usage accounting, and attachment safety. Direct
unit tests lift coverage without standing up the full sessions router.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.requests import Request

from omnigent._wrapper_labels import CLAUDE_NATIVE_WRAPPER_VALUE
from omnigent.entities import Conversation, StoredFile
from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.entities.pagination import PagedList
from omnigent.entities.permission import SessionPermission
from omnigent.server.schemas import SessionEventInput
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.policies.types import Phase
from omnigent.server._elicitation_registry import _PreResolvedHarnessElicitation
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, LEVEL_READ, RESERVED_USER_PUBLIC
from omnigent.server.routes import sessions as sessions_mod
from omnigent.server.routes.sessions import (
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
    _build_actor,
    _build_evaluation_context,
    _build_skill_slash_command_policy_body,
    _descendant_sessions,
    _derive_terminal_launch_args_from_spec,
    _enforce_tenant_scope,
    _extract_assistant_text_from_event,
    _extract_user_text_from_event,
    _handle_external_session_todos,
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
    _merge_pending_file_blocks,
    _message_text,
    _multipart_missing_detail,
    _native_terminal_name_for_harness,
    _native_terminal_runtime,
    _parse_external_assistant_message,
    _parse_session_create_metadata,
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
    yield
    sessions_mod._session_status_cache.clear()


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