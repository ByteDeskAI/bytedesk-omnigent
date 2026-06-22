"""External work-item intake tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.tasks.router import create_tasks_router
from bytedesk_omnigent.tasks.store import SqlAlchemyTaskStore
from bytedesk_omnigent.work_item_intake import ingest_work_item, normalize_work_item


def _store(tmp_path) -> SqlAlchemyTaskStore:
    return SqlAlchemyTaskStore(f"sqlite:///{tmp_path / 'work_items.db'}")


def test_normalize_github_issue_payload() -> None:
    draft = normalize_work_item(
        {
            "action": "opened",
            "issue": {
                "id": 123,
                "number": 7,
                "title": "Ship Slack approvals",
                "html_url": "https://github.com/acme/app/issues/7",
                "body": "Need approval buttons for autonomous runs.",
                "labels": [{"name": "P1"}, {"name": "integration"}],
            },
        },
        source="github",
    )

    assert draft.provider == "github"
    assert draft.external_id == "123"
    assert draft.title == "Ship Slack approvals"
    assert draft.priority == 2
    assert draft.required_capability == "developer.work_item"
    assert draft.labels == ("P1", "integration")


def test_ingest_work_item_creates_idempotent_task(tmp_path) -> None:
    store = _store(tmp_path)
    payload = {
        "provider": "linear",
        "data": {
            "id": "lin_42",
            "identifier": "ENG-42",
            "title": "Build GitHub engineering copilot",
            "url": "https://linear.app/acme/issue/ENG-42",
            "priority": 1,
            "labels": [{"name": "autonomous-agents"}],
        },
    }

    first = ingest_work_item(payload=payload, store=store, now=100)
    replay = ingest_work_item(payload=payload, store=store, now=101)

    assert first.created is True
    assert replay.created is False
    assert replay.task.id == first.task.id
    tasks = store.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].source == "work_item:linear"
    assert tasks[0].required_capability == "project_management.work_item"
    assert tasks[0].priority == 1
    assert tasks[0].payload is not None
    assert tasks[0].payload["external_id"] == "lin_42"
    assert tasks[0].payload["kind"] == "external_work_item"


def test_tasks_intake_route_creates_then_returns_existing_task(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)

    import bytedesk_omnigent.tasks.store as task_store_module

    monkeypatch.setattr(task_store_module, "get_task_store", lambda: store)

    app = FastAPI()
    app.include_router(create_tasks_router(), prefix="/v1")
    client = TestClient(app)

    payload = {
        "issue": {
            "key": "PROJ-99",
            "fields": {
                "summary": "Wire Jira intake to Omnigent Tasks",
                "priority": {"name": "Highest"},
                "labels": ["workflow-harness"],
            },
            "self": "https://example.atlassian.net/rest/api/3/issue/PROJ-99",
        }
    }

    created = client.post("/v1/tasks/intake?source=jira", json=payload)
    assert created.status_code == 201
    created_payload = created.json()
    assert created_payload["status"] == "created"
    assert created_payload["provider"] == "jira"
    assert created_payload["external_id"] == "PROJ-99"
    assert created_payload["task"]["title"] == "Wire Jira intake to Omnigent Tasks"
    assert created_payload["task"]["priority"] == 1

    existing = client.post("/v1/tasks/intake?source=jira", json=payload)
    assert existing.status_code == 200
    assert existing.json()["status"] == "existing"
    assert existing.json()["task"]["id"] == created_payload["task"]["id"]


def test_tasks_intake_route_rejects_payload_without_identifier(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)

    import bytedesk_omnigent.tasks.store as task_store_module

    monkeypatch.setattr(task_store_module, "get_task_store", lambda: store)

    app = FastAPI()
    app.include_router(create_tasks_router(), prefix="/v1")
    client = TestClient(app)

    response = client.post("/v1/tasks/intake?source=generic", json={"labels": ["missing"]})

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_payload"
