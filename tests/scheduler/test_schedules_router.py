from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.scheduler import SqlAlchemyCronScheduler
from bytedesk_omnigent.scheduler.router import create_schedules_router
from bytedesk_omnigent.tasks.store import SqlAlchemyTaskStore
from omnigent.entities import Agent


class _AgentStore:
    def __init__(self, agents: list[Agent]) -> None:
        self._agents = {agent.id: agent for agent in agents}

    def get(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    def get_by_name(self, name: str) -> Agent | None:
        for agent in self._agents.values():
            if agent.name == name and agent.session_id is None:
                return agent
        return None


def test_cadence_draft_derives_common_natural_language() -> None:
    app = FastAPI()
    app.include_router(create_schedules_router())
    client = TestClient(app)

    response = client.post(
        "/schedules/assistant/draft",
        json={"natural_language": "weekdays at 8:30am"},
    )

    assert response.status_code == 200
    assert response.json() == {"schedule_kind": "cron", "schedule_expr": "30 8 * * 1-5"}


def test_create_schedule_and_project_occurrences(tmp_path, monkeypatch) -> None:
    scheduler = SqlAlchemyCronScheduler(f"sqlite:///{tmp_path / 'cron.db'}")
    task_store = SqlAlchemyTaskStore(f"sqlite:///{tmp_path / 'tasks.db'}")
    task = task_store.create_task(
        title="Run the weekly business review",
        payload={"prompt": "Run the weekly business review."},
    )

    import bytedesk_omnigent.runtime as runtime
    import bytedesk_omnigent.tasks.store as tasks_store

    monkeypatch.setattr(runtime, "get_cron_scheduler", lambda: scheduler)
    monkeypatch.setattr(tasks_store, "get_task_store", lambda: task_store)

    app = FastAPI()
    app.include_router(create_schedules_router())
    client = TestClient(app)
    start_at = datetime(2026, 6, 25, 12, tzinfo=UTC)

    created = client.post(
        "/schedules",
        json={
            "agent_id": "ag_owner",
            "title": "Weekly review",
            "task_id": task.id,
            "natural_language": "weekly on Thursday at 12pm",
            "start_at": start_at.isoformat(),
        },
    )

    assert created.status_code == 201
    body = created.json()["schedule"]
    assert body["agent_id"] == "ag_owner"
    assert body["task_id"] == task.id
    assert body["schedule_kind"] == "cron"
    assert body["schedule_expr"] == "0 12 * * 4"

    occurrences = client.get(
        "/schedules/occurrences",
        params={
            "agent_id": "ag_owner",
            "start": "2026-06-25T00:00:00+00:00",
            "end": "2026-07-10T00:00:00+00:00",
        },
    )

    assert occurrences.status_code == 200
    fires = [item["fire_at"] for item in occurrences.json()["occurrences"]]
    assert int(start_at.timestamp()) in fires


def test_create_schedule_accepts_agent_name(tmp_path, monkeypatch) -> None:
    scheduler = SqlAlchemyCronScheduler(f"sqlite:///{tmp_path / 'cron.db'}")
    task_store = SqlAlchemyTaskStore(f"sqlite:///{tmp_path / 'tasks.db'}")
    agent_store = _AgentStore(
        [
            Agent(
                id="ag_owner",
                created_at=1,
                name="chief-of-staff",
                bundle_location="ag_owner/hash",
            )
        ]
    )

    import bytedesk_omnigent.runtime as runtime
    import bytedesk_omnigent.tasks.store as tasks_store
    import omnigent.runtime as omnigent_runtime

    monkeypatch.setattr(runtime, "get_cron_scheduler", lambda: scheduler)
    monkeypatch.setattr(tasks_store, "get_task_store", lambda: task_store)
    monkeypatch.setattr(omnigent_runtime, "get_agent_store", lambda: agent_store)

    app = FastAPI()
    app.include_router(create_schedules_router())
    client = TestClient(app)

    created = client.post(
        "/schedules",
        json={
            "agent_id": "chief-of-staff",
            "title": "Daily check-in",
            "natural_language": "daily at 9am",
        },
    )

    assert created.status_code == 201, created.text
    assert created.json()["schedule"]["agent_id"] == "ag_owner"

    listed = client.get("/schedules", params={"agent_id": "chief-of-staff"})
    assert listed.status_code == 200, listed.text
    assert [row["agent_id"] for row in listed.json()["schedules"]] == ["ag_owner"]


def test_disable_schedule(tmp_path, monkeypatch) -> None:
    scheduler = SqlAlchemyCronScheduler(f"sqlite:///{tmp_path / 'cron.db'}")
    trigger = scheduler.register_trigger(
        agent_id="ag_owner",
        key="schedule:test",
        schedule_kind="interval",
        schedule_expr="3600",
        next_fire_at=1000,
        payload={"title": "Hourly test"},
    )

    import bytedesk_omnigent.runtime as runtime

    monkeypatch.setattr(runtime, "get_cron_scheduler", lambda: scheduler)
    app = FastAPI()
    app.include_router(create_schedules_router())
    client = TestClient(app)

    response = client.patch(f"/schedules/{trigger.id}", json={"enabled": False})

    assert response.status_code == 200
    assert response.json()["schedule"]["enabled"] is False
