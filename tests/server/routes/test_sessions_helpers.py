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

from omnigent.entities import Conversation, StoredFile
from omnigent.entities.permission import SessionPermission
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
    _attachment_disposition,
    _codex_subagent_display_tool,
    _discovery_key,
    _format_sse,
    _is_codex_native_subagent,
    _last_task_error_from_labels,
    _message_text,
    _model_usage_bucket,
    _owner_from_grants,
    _parse_last_event_id,
    _permission_level_from_grants,
    _priced_cost_for_display,
    _record_daily_cost,
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