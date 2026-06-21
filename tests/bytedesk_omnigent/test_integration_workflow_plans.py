"""Tests for deterministic connected-app workflow plan compilation."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.integration_workflow_plans import (
    compile_integration_workflow_plan,
    normalize_provider,
)
from bytedesk_omnigent.routes.integration_workflow_plans import (
    create_integration_workflow_plans_router,
)


def test_compile_github_plan_is_deterministic_and_agent_step_is_bounded() -> None:
    plan1 = compile_integration_workflow_plan(
        provider="GitHub",
        goal="triage failing CI",
        object_ref="ByteDeskAI/bytedesk-omnigent#123",
        requester="ryan",
        context_refs=["check-run:build"],
    )
    plan2 = compile_integration_workflow_plan(
        provider="github",
        goal="triage failing CI",
        object_ref="ByteDeskAI/bytedesk-omnigent#123",
        requester="ryan",
        context_refs=["check-run:build"],
    )

    assert plan1.idempotency_key == plan2.idempotency_key
    assert plan1.provider == "github"
    assert plan1.capability == "developer.work_item"
    assert plan1.approval_required is False
    assert plan1.task_template["required_capability"] == "developer.work_item"
    assert plan1.task_template["source"] == "integration:github"

    agent_steps = [step for step in plan1.steps if step.kind == "agent"]
    assert len(agent_steps) == 1
    assert agent_steps[0].name == "agent.run_capability"
    assert agent_steps[0].deterministic is False
    assert all(step.idempotency_key.startswith(plan1.idempotency_key) for step in plan1.steps)


def test_sensitive_systems_default_to_approval_before_writeback() -> None:
    plan = compile_integration_workflow_plan(
        provider="stripe",
        goal="resolve disputed invoice",
        object_ref="acct_123/in_456",
        requester="finance@example.com",
    )

    assert plan.approval_required is True
    names = [step.name for step in plan.steps]
    assert "approval.request" in names
    assert names.index("approval.request") < names.index("agent.run_capability")
    assert "providers.stripe.writeback" in names


def test_writeback_can_be_disabled_for_read_only_previews() -> None:
    plan = compile_integration_workflow_plan(
        provider="notion",
        goal="summarize launch notes",
        object_ref="page_123",
        writeback=False,
    )

    assert plan.writeback_enabled is False
    assert all("writeback" not in step.name for step in plan.steps)
    assert plan.steps[-1].name == "outcome_record"


def test_provider_aliases_and_unknown_provider_validation() -> None:
    assert normalize_provider("MS Teams") == "microsoft_teams"

    try:
        normalize_provider("unknown app")
    except ValueError as exc:
        assert "unsupported integration provider" in str(exc)
        assert "github" in str(exc)
    else:  # pragma: no cover - defensive assertion clarity
        raise AssertionError("expected unknown provider to fail")


def test_compile_route_returns_plan() -> None:
    app = FastAPI()
    app.include_router(create_integration_workflow_plans_router(), prefix="/v1")
    client = TestClient(app)

    response = client.post(
        "/v1/integration-workflow-plans/compile",
        json={
            "provider": "linear",
            "goal": "scope implementation for ticket",
            "object_ref": "LIN-42",
            "requester": "pm@example.com",
            "idempotency_key": "external-key-42",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "linear"
    assert body["idempotency_key"] == "external-key-42"
    assert body["task_template"]["source"] == "integration:linear"
    assert [step["key"] for step in body["steps"]][:3] == [
        "01_normalize_event",
        "02_fetch_context",
        "03_resolve_assignee",
    ]
