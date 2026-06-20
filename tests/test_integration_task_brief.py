"""Tests for deterministic third-party integration task briefs."""

from __future__ import annotations

import pytest

from omnigent.integration_task_brief import (
    IntegrationEvent,
    compile_task_brief,
    compile_task_brief_markdown,
)


def test_compile_task_brief_normalizes_event_into_agent_ready_plan() -> None:
    """A noisy SaaS event becomes a deterministic task brief for an agent."""
    event = IntegrationEvent(
        provider="GitHub",
        event_type="pull_request.opened",
        resource_id="ByteDeskAI/bytedesk-omnigent#130",
        title="Add integration rollback plan compiler",
        actor="octocat",
        url="https://github.com/ByteDeskAI/bytedesk-omnigent/pull/130",
        summary="New PR opened with rollback compiler changes",
        payload={
            "labels": ["enhancement", "loop"],
            "repository": {"full_name": "ByteDeskAI/bytedesk-omnigent"},
            "pull_request": {"number": 130},
            "ignored": {"nested": {"bulk": "data"}},
        },
    )

    brief = compile_task_brief(event, objective="Review for platform integration readiness")

    assert brief["version"] == 1
    assert brief["source"] == {
        "provider": "github",
        "event_type": "pull_request.opened",
        "resource_id": "ByteDeskAI/bytedesk-omnigent#130",
        "url": "https://github.com/ByteDeskAI/bytedesk-omnigent/pull/130",
    }
    assert brief["task"]["title"] == "GitHub: Add integration rollback plan compiler"
    assert brief["task"]["objective"] == "Review for platform integration readiness"
    assert brief["task"]["context"] == "New PR opened with rollback compiler changes"
    assert brief["task"]["requested_by"] == "octocat"
    assert brief["task"]["routing_labels"] == (
        "integration",
        "provider:github",
        "event:pull_request.opened",
    )
    assert brief["task"]["payload_facts"] == {
        "labels": ["enhancement", "loop"],
        "repository.full_name": "ByteDeskAI/bytedesk-omnigent",
        "pull_request.number": 130,
    }
    assert brief["handoff"]["recommended_agent_capabilities"] == (
        "third-party-integration",
        "github",
        "pull-request-triage",
    )
    assert brief["handoff"]["next_steps"] == (
        "Inspect the source event and payload facts.",
        "Decide whether the event requires agent action or can be acknowledged.",
        "Execute the objective, then post a concise outcome back to the integration surface.",
    )


def test_compile_task_brief_markdown_is_stable_and_human_readable() -> None:
    """The Markdown form is stable enough to paste into spawned agent prompts."""
    event = IntegrationEvent(
        provider="Slack",
        event_type="app_mention",
        resource_id="C123:1700000000.0001",
        title="Can an agent summarize the release blockers?",
        actor="U123",
        summary="User mentioned the app in #release asking for blocker analysis.",
    )

    markdown = compile_task_brief_markdown(event, objective="Triage the request")

    assert markdown == "\n".join(
        [
            "# Slack integration task brief",
            "",
            "- Event: app_mention",
            "- Resource: C123:1700000000.0001",
            "- Requested by: U123",
            "- Objective: Triage the request",
            "- Context: User mentioned the app in #release asking for blocker analysis.",
            "- Routing labels: integration, provider:slack, event:app_mention",
            "- Recommended capabilities: third-party-integration, slack, collaborative-triage",
            "",
            "## Next steps",
            "1. Inspect the source event and payload facts.",
            "2. Decide whether the event requires agent action or can be acknowledged.",
            "3. Execute the objective, then post a concise outcome back to the "
            "integration surface.",
        ]
    )


@pytest.mark.parametrize("field", ["provider", "event_type", "resource_id", "title"])
def test_compile_task_brief_requires_core_event_fields(field: str) -> None:
    """Blank core fields fail loud instead of producing unroutable work."""
    values = {
        "provider": "notion",
        "event_type": "page.updated",
        "resource_id": "page-123",
        "title": "Requirements changed",
    }
    values[field] = "  "

    with pytest.raises(ValueError, match=field):
        compile_task_brief(IntegrationEvent(**values))


def test_compile_task_brief_flattens_only_safe_scalar_payload_facts() -> None:
    """Payload facts expose useful scalars without dumping entire webhook bodies."""
    event = IntegrationEvent(
        provider="Notion",
        event_type="page.updated",
        resource_id="page-123",
        title="Roadmap updated",
        payload={
            "page_id": "page-123",
            "archived": False,
            "properties": {"status": "In Review", "owner": {"id": "hidden"}},
            "children": [{"type": "paragraph"}],
            "empty": None,
        },
    )

    brief = compile_task_brief(event)

    assert brief["task"]["payload_facts"] == {
        "page_id": "page-123",
        "archived": False,
        "properties.status": "In Review",
    }
    assert brief["task"]["objective"] == "Handle Notion page.updated for Roadmap updated"
