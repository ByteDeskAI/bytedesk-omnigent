"""Deterministic third-party integration backfill plans (iteration 56)."""

from __future__ import annotations

import pytest

from bytedesk_omnigent.integration_backfill import (
    IntegrationBackfillRequest,
    compile_backfill_plan,
)


def test_compile_backfill_plan_builds_bounded_cursor_resume_contract() -> None:
    """A Slack history import becomes deterministic, resumable work for agents.

    The plan must tell a runner which checkpoint to persist, how to deduplicate
    imported records, and which completion event can resume downstream workflows.
    """
    plan = compile_backfill_plan(
        IntegrationBackfillRequest(
            source="Slack",
            resource="channel.messages",
            workspace_id="acme",
            start_cursor="2026-01-01T00:00:00Z",
            end_cursor="2026-01-31T23:59:59Z",
            page_size=250,
            max_pages=3,
            required_scopes=("channels:history", "users:read"),
        )
    )

    assert plan.source == "slack"
    assert plan.resource == "channel.messages"
    assert plan.checkpoint_key == "integration-backfill:acme:slack:channel.messages"
    assert plan.idempotency_scope == "integration-backfill/slack/channel.messages"
    assert plan.completion_match_key == "channel.messages.backfill.completed"
    assert plan.required_scopes == ("channels:history", "users:read")
    assert plan.safety_notes == (
        "read-only connector scope; never mutate third-party records during backfill",
        "persist the checkpoint after each committed page before fetching the next page",
        "deduplicate every imported record through the idempotency scope before task creation",
    )
    assert [step.name for step in plan.steps] == [
        "load_checkpoint",
        "fetch_page_1",
        "commit_page_1",
        "fetch_page_2",
        "commit_page_2",
        "fetch_page_3",
        "commit_page_3",
        "emit_completion_event",
    ]
    assert plan.steps[1].cursor == "2026-01-01T00:00:00Z"
    assert plan.steps[2].checkpoint_after == "page:1"
    assert plan.steps[-1].signal_payload == {
        "source": "slack",
        "resource": "channel.messages",
        "workspace_id": "acme",
        "checkpoint_key": "integration-backfill:acme:slack:channel.messages",
        "pages_planned": 3,
    }


def test_compile_backfill_plan_rejects_unbounded_or_write_scoped_backfills() -> None:
    """Backfills are intentionally bounded and read-only before agents run them."""
    with pytest.raises(ValueError, match="max_pages must be between 1 and 100"):
        compile_backfill_plan(
            IntegrationBackfillRequest(
                source="github",
                resource="issues",
                workspace_id="helms",
                start_cursor="1",
                max_pages=0,
            )
        )

    with pytest.raises(ValueError, match="read-only scopes"):
        compile_backfill_plan(
            IntegrationBackfillRequest(
                source="notion",
                resource="pages",
                workspace_id="helms",
                start_cursor="cursor-0",
                required_scopes=("read:content", "write:content"),
            )
        )
