from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.sessions import set_session_initiator
from bytedesk_omnigent.tasks.router import create_tasks_router
from bytedesk_omnigent.tasks.store import SqlAlchemyTaskStore
from omnigent.entities import Agent


@dataclass
class _Initiator:
    calls: list[dict] = field(default_factory=list)

    def initiate(self, *, agent_id, prompt, source, metadata=None, external_key=None) -> str:
        self.calls.append(
            {
                "agent_id": agent_id,
                "prompt": prompt,
                "source": source,
                "metadata": metadata,
                "external_key": external_key,
            }
        )
        return "conv_1"


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


def test_create_fetch_and_run_task_route(tmp_path, monkeypatch) -> None:
    store = SqlAlchemyTaskStore(f"sqlite:///{tmp_path / 'tasks.db'}")
    agent_store = _AgentStore(
        [
            Agent(
                id="ag_planner",
                created_at=1,
                name="planner-agent",
                bundle_location="ag_planner/hash",
            ),
            Agent(
                id="ag_runner",
                created_at=1,
                name="runner-agent",
                bundle_location="ag_runner/hash",
            ),
        ]
    )

    import bytedesk_omnigent.tasks.store as tasks_store
    import omnigent.runtime as omnigent_runtime

    monkeypatch.setattr(tasks_store, "get_task_store", lambda: store)
    monkeypatch.setattr(omnigent_runtime, "get_agent_store", lambda: agent_store)
    app = FastAPI()
    app.include_router(create_tasks_router())
    client = TestClient(app)

    created = client.post(
        "/tasks",
        json={
            "title": "Draft launch plan",
            "prompt": "Draft the launch plan.",
            "owner_agent_id": "planner-agent",
        },
    )

    assert created.status_code == 201
    task = created.json()["task"]
    assert task["title"] == "Draft launch plan"
    assert task["owner_agent_id"] == "ag_planner"

    fetched = client.get(f"/tasks/{task['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["task"]["payload"]["prompt"] == "Draft the launch plan."

    initiator = _Initiator()
    try:
        set_session_initiator(initiator)
        run = client.post(
            f"/tasks/{task['id']}/run",
            json={"run_as_agent_id": "runner-agent", "external_key": "manual:test"},
        )
    finally:
        set_session_initiator(None)

    assert run.status_code == 200
    assert run.json()["dispatch"]["agent_id"] == "ag_runner"
    assert initiator.calls == [
        {
            "agent_id": "ag_runner",
            "prompt": "Draft the launch plan.",
            "source": f"task:{task['id']}",
            "metadata": {"task_id": task["id"], "agent_id": "ag_runner"},
            "external_key": "manual:test",
        }
    ]
