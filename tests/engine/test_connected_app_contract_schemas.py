"""Contract checks for the connected-app v1 schema bundle."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "connected-app" / "v1"


def _load_json(name: str) -> dict[str, Any]:
    return json.loads((CONTRACT_DIR / name).read_text())


def _validator(name: str, ref: str | None = None) -> Draft202012Validator:
    schema = _load_json(name)
    Draft202012Validator.check_schema(schema)
    if ref is None:
        return Draft202012Validator(schema)
    return Draft202012Validator({**schema, "$ref": ref})


def test_connected_app_json_schemas_are_valid_draft_2020_12() -> None:
    for path in sorted(CONTRACT_DIR.glob("*.schema.json")):
        Draft202012Validator.check_schema(json.loads(path.read_text()))


def test_provider_manifest_accepts_office_reference_provider_shape() -> None:
    manifest = {
        "name": "office",
        "baseUrl": "https://office.internal",
        "contractVersion": "connected-app.v1",
        "schemaId": (
            "https://omnigent.ai/contracts/connected-app/v1/"
            "provider-manifest.schema.json"
        ),
        "sensors": ["sales.opportunity.closed"],
        "actuators": [{"name": "sales.create_task", "riskTier": "medium"}],
        "outcomes": ["outcome.booked"],
        "webhookSources": ["sales"],
        "auth": {"header": "X-Omnigent-Provider-Secret", "secret": "redacted"},
    }

    _validator("provider-manifest.schema.json").validate(manifest)


def test_goal_request_matches_implemented_create_goal_route_shape() -> None:
    request = {
        "title": "Increase qualified pipeline",
        "priority": 2,
        "source": "office",
        "payload": {"officeGoalId": "bd-goal-123"},
        "targetKind": "department",
        "targetId": "sales",
        "targetLabel": "Sales",
        "departmentSlug": "sales",
        "outcomeKind": "financial",
        "readinessKind": "dependent",
        "dependencies": [
            {
                "kind": "jira_issue",
                "ref": "BDP-2608",
                "label": "Office provider route is live",
                "status": "pending",
                "metadata": {"team": "office"},
            }
        ],
    }

    _validator("goal-request.schema.json", "#/$defs/request").validate(request)


def test_sensor_and_actuator_role_payloads_validate() -> None:
    sensor_request = {
        "query": {"kind": "opportunity", "id": "opp-123"},
        "now": 1800000000,
        "context": {
            "goalId": "goal_1",
            "targetKind": "department",
            "targetId": "sales",
            "departmentSlug": "sales",
            "outcomeKind": "financial",
        },
    }
    sensor_response = {
        "satisfied": True,
        "value": {"amountCents": 250000},
        "observedAt": 1800000001,
        "staleAfterS": 300,
        "detail": None,
    }
    actuator_request = {
        "action": {"name": "createTask", "subjectRef": "opp-123"},
        "context": {"goalId": "goal_1", "targetKind": "department"},
    }
    actuator_response = {
        "ok": True,
        "output": {"taskId": "task_123"},
        "detail": None,
    }

    _validator("sensor-evaluate.schema.json", "#/$defs/request").validate(sensor_request)
    _validator("sensor-evaluate.schema.json", "#/$defs/response").validate(sensor_response)
    _validator("actuator-execute.schema.json", "#/$defs/request").validate(actuator_request)
    _validator("actuator-execute.schema.json", "#/$defs/response").validate(actuator_response)


def test_inbound_approval_and_tool_payloads_validate() -> None:
    inbound_event = {
        "source": "office",
        "type": "outcome.booked",
        "idempotencyKey": "office:opp-123:closed",
        "occurredAt": 1800000000,
        "tenantId": "tenant_1",
        "eventId": "evt_123",
        "subjectRef": "opp-123",
        "normalized": {
            "goalId": "goal_1",
            "realizedValueCents": 250000,
            "evidence": {"opportunityId": "opp-123"},
        },
        "rawPayload": {"id": "opp-123"},
    }
    approval_decision = {
        "approvalRef": "approval_123",
        "decision": "approved",
        "decidedBy": "ryan",
        "decidedAt": 1800000001,
        "reason": "Within campaign budget",
        "metadata": {"source": "office"},
    }
    tool_event = {
        "eventId": "tool_evt_123",
        "type": "tool.completed",
        "occurredAt": 1800000002,
        "source": "office",
        "tenantId": "tenant_1",
        "goalId": "goal_1",
        "targetKind": "department",
        "targetId": "sales",
        "toolName": "bytedesk_generate_image",
        "toolCallId": "call_123",
        "arguments": {"prompt": "campaign hero"},
        "output": {"assetId": "asset_123"},
        "failureClass": None,
        "retryable": None,
        "detail": None,
        "traceId": "trace_123",
        "correlationId": "corr_123",
    }

    _validator("inbound-event.schema.json").validate(inbound_event)
    _validator("approval-decision.schema.json", "#/$defs/decision").validate(
        approval_decision
    )
    _validator("tool-event.schema.json", "#/$defs/event").validate(tool_event)


def test_asyncapi_catalog_covers_lifecycle_approval_and_tool_events() -> None:
    catalog = yaml.safe_load((CONTRACT_DIR / "events.asyncapi.yaml").read_text())

    messages = {
        message["name"]
        for message in catalog["components"]["messages"].values()
        if "name" in message
    }
    assert {
        "goal.progress",
        "goal.completed",
        "goal.failed",
        "goal.canceled",
        "goal.retry_scheduled",
        "approval.requested",
        "approval.decision",
        "budget_risk.requested",
        "tool.started",
        "tool.completed",
        "tool.failed",
    } <= messages

    channels = catalog["channels"]
    assert channels["goalLifecycle"]["address"] == "omnigent.connected-app.v1.goal.lifecycle"
    assert channels["goalApproval"]["address"] == "omnigent.connected-app.v1.goal.approval"
    assert channels["goalTools"]["address"] == "omnigent.connected-app.v1.goal.tools"
