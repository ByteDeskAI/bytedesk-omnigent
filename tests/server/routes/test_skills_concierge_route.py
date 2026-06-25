"""Tests for scoped Skills Concierge session bootstrap."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.routes.skills_concierge import create_skills_concierge_router


@dataclass
class _FakeAgent:
    id: str
    name: str


@dataclass
class _FakeAgentStore:
    lookups: list[str] = field(default_factory=list)

    def get_by_name(self, name: str) -> _FakeAgent | None:
        self.lookups.append(name)
        if name == "skills-concierge":
            return _FakeAgent(id="ag_concierge", name="skills-concierge")
        return None


@dataclass
class _FakeConversationStore:
    created: list[dict[str, Any]] = field(default_factory=list)
    labels: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    appended: list[tuple[str, list[Any]]] = field(default_factory=list)

    def create_conversation(self, **kwargs: Any) -> Any:
        self.created.append(kwargs)
        return type("Conv", (), {"id": "conv_skills"})()

    def set_labels(self, conv_id: str, labels: dict[str, str]) -> None:
        self.labels.append((conv_id, labels))

    def append(self, conv_id: str, items: list[Any]) -> None:
        self.appended.append((conv_id, items))


def test_start_skills_concierge_session_seeds_scope_prompt(monkeypatch) -> None:
    agent_store = _FakeAgentStore()
    conversation_store = _FakeConversationStore()
    monkeypatch.setattr("omnigent.runtime.get_agent_store", lambda: agent_store)
    monkeypatch.setattr("omnigent.runtime.get_conversation_store", lambda: conversation_store)

    app = FastAPI()
    app.include_router(create_skills_concierge_router(auth_provider=None), prefix="/v1")
    client = TestClient(app)

    resp = client.post(
        "/v1/skills/concierge/sessions",
        json={
            "target_kind": "department",
            "target_id": "Engineering",
            "target_label": "Engineering",
            "target_agent_ids": ["ag_dev"],
        },
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["session_id"] == "conv_skills"
    assert body["web_path"] == "/c/conv_skills"
    assert conversation_store.labels[0][1]["bytedesk.skills_concierge.scope"] == "department:Engineering"
    seeded = conversation_store.appended[0][1][0]
    assert "SKILLS CONCIERGE SESSION" in seeded.data.content[0]["text"]