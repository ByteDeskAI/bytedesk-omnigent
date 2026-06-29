"""Tests for the goals-backlog read route: auth gate + serialization (BDP-2290).

The 401 path fires in ``require_user`` before the store, so it's self-contained;
the data path monkeypatches the store accessor so no runtime/conversation-store
init is needed (mirrors the governance route test's self-contained style).
"""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from bytedesk_omnigent.goals import Goal
from bytedesk_omnigent.routes.goals import create_goals_router
from omnigent.errors import OmnigentError


class _NoIdentityAuth:
    """A multi-user auth provider that never resolves an identity → forces 401."""

    def get_user_id(self, request: object) -> None:
        return None


class _FakeStore:
    def __init__(self, goals: list[Goal]) -> None:
        self._goals = goals
        self.calls: list[dict] = []
        self.created: list[dict] = []

    def list_goals(self, *, status=None, owner_agent_id=None, **kwargs) -> list[Goal]:
        self.calls.append({"status": status, "owner": owner_agent_id, **kwargs})
        return self._goals

    def create_goal(self, **kwargs) -> Goal:
        self.created.append(kwargs)
        return Goal(
            id="goal_committed",
            title=kwargs["title"],
            owner_agent_id=None,
            status="open",
            priority=kwargs.get("priority", 3),
            source=kwargs.get("source"),
            payload=kwargs.get("payload"),
            created_at=1000,
            updated_at=1000,
            target_kind=kwargs.get("target_kind") or "organization",
            target_id=kwargs.get("target_id") or "omnigent",
            target_label=kwargs.get("target_label"),
            readiness_kind=kwargs.get("readiness_kind") or "immediate",
            activation_state="ready",
        )


class _FakeAgentStore:
    def __init__(self) -> None:
        self.lookups: list[str] = []

    def get_by_name(self, name: str):
        self.lookups.append(name)
        if name == "chief-of-staff":
            return SimpleNamespace(id="ag_maya", name="chief-of-staff")
        return None


class _FakeConversationStore:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.labels: list[tuple[str, dict[str, str]]] = []
        self.appended: list[tuple[str, list]] = []

    def create_conversation(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(id="conv_plan", title=kwargs.get("title"))

    def set_labels(self, conversation_id: str, updates: dict[str, str]) -> None:
        self.labels.append((conversation_id, updates))

    def append(self, conversation_id: str, items: list) -> list:
        self.appended.append((conversation_id, items))
        return []


def _app(auth_provider: object) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(
        OmnigentError,
        lambda request, exc: JSONResponse(
            status_code=exc.http_status, content={"error": exc.code}
        ),
    )
    app.include_router(create_goals_router(auth_provider=auth_provider), prefix="/v1")
    return app


def test_list_requires_auth_in_multi_user_mode() -> None:
    client = TestClient(_app(_NoIdentityAuth()), raise_server_exceptions=False)
    assert client.get("/v1/goals").status_code == 401


def test_list_returns_backlog_in_single_user_mode(monkeypatch) -> None:
    goal = Goal(
        id="goal_1",
        title="Ship X",
        owner_agent_id=None,
        status="open",
        priority=2,
        source="triage",
        payload={"score": {"total": 9}},
        created_at=1000,
        updated_at=1000,
    )
    store = _FakeStore([goal])
    monkeypatch.setattr("bytedesk_omnigent.goals.get_goal_store", lambda: store)
    client = TestClient(_app(None))  # single-user mode → open
    resp = client.get(
        "/v1/goals?status=open&target_kind=department&target_id=Operations&include_dependencies=true"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["goals"][0]["id"] == "goal_1"
    assert body["goals"][0]["status"] == "open"
    assert body["goals"][0]["payload"] == {"score": {"total": 9}}
    # The query params thread through to the store filter.
    assert store.calls == [
        {
            "status": "open",
            "owner": None,
            "target_kind": "department",
                "target_id": "Operations",
                "readiness_kind": None,
                "activation_state": None,
                "department_slug": None,
                "outcome_kind": None,
                "ready_only": False,
                "include_dependencies": True,
            }
    ]


def test_planner_sources_report_atlassian_and_google_availability(monkeypatch) -> None:
    monkeypatch.delenv("GOOGLE_WORKSPACE_MCP_URL", raising=False)
    monkeypatch.delenv("BYTEDESK_GOOGLE_WORKSPACE_MCP_URL", raising=False)
    client = TestClient(_app(None))

    resp = client.get("/v1/goals/planner/sources")

    assert resp.status_code == 200
    sources = {source["id"]: source for source in resp.json()["sources"]}
    assert sources["jira"]["available"] is True
    assert sources["confluence"]["tools"] == ["bytedesk_confluence"]
    assert sources["google_workspace"]["available"] is False
    assert sources["google_workspace"]["reason"] == "not_configured"


def test_start_planning_session_uses_planner_agent_and_seeds_prompt(monkeypatch) -> None:
    agent_store = _FakeAgentStore()
    conversation_store = _FakeConversationStore()
    monkeypatch.setattr("omnigent.runtime.get_agent_store", lambda: agent_store)
    monkeypatch.setattr("omnigent.runtime.get_conversation_store", lambda: conversation_store)
    monkeypatch.setattr(
        "bytedesk_omnigent.routes.goals._publish_planning_event",
        lambda *args, **kwargs: None,
    )
    client = TestClient(_app(None))

    resp = client.post(
        "/v1/goals/planner/sessions",
        json={
            "target_kind": "department",
            "target_id": "Operations",
            "target_label": "Operations",
            "source_ids": ["jira", "confluence", "google_workspace"],
        },
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["session_id"] == "conv_plan"
    assert body["agent_name"] == "chief-of-staff"
    assert body["web_path"] == "/c/conv_plan"
    assert agent_store.lookups == ["goal-planner", "chief-of-staff"]
    assert conversation_store.created == [
        {"agent_id": "ag_maya", "title": "Plan goal: Operations", "kind": "default"}
    ]
    assert conversation_store.labels[0][1]["bytedesk.goal_planner.target_kind"] == "department"
    assert conversation_store.labels[0][1]["bytedesk.goal_planner.sources"] == "jira,confluence"
    seeded_item = conversation_store.appended[0][1][0]
    assert "GOAL PLANNING INTERVIEW" in seeded_item.data.content[0]["text"]
    assert "AskUserQuestion" in seeded_item.data.content[0]["text"]


def test_posture_readback_requires_auth_in_multi_user_mode() -> None:
    client = TestClient(_app(_NoIdentityAuth()), raise_server_exceptions=False)
    assert client.get("/v1/goals/posture").status_code == 401


def test_posture_readback_gated_is_not_armed(monkeypatch) -> None:
    from bytedesk_omnigent.engine.config import GoalEngineConfig

    async def _fake_load(target_id, **kwargs):
        assert target_id is None
        return GoalEngineConfig()  # default gated

    monkeypatch.setattr("bytedesk_omnigent.routes.goals.load_goal_engine_config", _fake_load)
    monkeypatch.setattr("bytedesk_omnigent.routes.goals._arming_enabled", lambda: True)
    client = TestClient(_app(None))

    resp = client.get("/v1/goals/posture")

    assert resp.status_code == 200
    assert resp.json() == {"posture": "gated", "armed": False, "arming_enabled": True}


def test_posture_readback_full_auto_with_arming_is_armed(monkeypatch) -> None:
    from bytedesk_omnigent.engine.config import GoalEngineConfig

    async def _fake_load(target_id, **kwargs):
        assert target_id == "acme"
        return GoalEngineConfig(autonomy_posture="full_auto")

    monkeypatch.setattr("bytedesk_omnigent.routes.goals.load_goal_engine_config", _fake_load)
    monkeypatch.setattr("bytedesk_omnigent.routes.goals._arming_enabled", lambda: True)
    client = TestClient(_app(None))

    resp = client.get("/v1/goals/posture?target_id=acme")

    assert resp.status_code == 200
    assert resp.json() == {"posture": "full_auto", "armed": True, "arming_enabled": True}


def test_posture_readback_full_auto_without_arming_is_not_armed(monkeypatch) -> None:
    from bytedesk_omnigent.engine.config import GoalEngineConfig

    async def _fake_load(target_id, **kwargs):
        return GoalEngineConfig(autonomy_posture="full_auto")

    monkeypatch.setattr("bytedesk_omnigent.routes.goals.load_goal_engine_config", _fake_load)
    monkeypatch.setattr("bytedesk_omnigent.routes.goals._arming_enabled", lambda: False)
    client = TestClient(_app(None))

    resp = client.get("/v1/goals/posture")

    assert resp.json() == {"posture": "full_auto", "armed": False, "arming_enabled": False}


def test_commit_planning_session_creates_goal_with_planning_payload(monkeypatch) -> None:
    store = _FakeStore([])
    monkeypatch.setattr("bytedesk_omnigent.goals.get_goal_store", lambda: store)
    monkeypatch.setattr(
        "bytedesk_omnigent.routes.goals._publish_planning_event",
        lambda *args, **kwargs: None,
    )
    client = TestClient(_app(None))

    resp = client.post(
        "/v1/goals/planner/sessions/conv_plan/commit",
        json={
            "source_ids": ["jira"],
            "draft": {
                "title": "Close onboarding loop",
                "priority": 2,
                "target_kind": "organization",
                "target_id": "omnigent",
                "target_label": "Organization",
                "readiness_kind": "dependent",
                "dependencies": [{"kind": "system_state", "label": "Roster synced"}],
                "outcome": "Every agent has a clear goal.",
                "acceptance_criteria": ["Goal appears in the backlog"],
                "assumptions": ["Maya owns coordination"],
                "source_refs": [{"type": "jira", "key": "BDP-1"}],
            },
        },
    )

    assert resp.status_code == 201
    assert resp.json()["goal"]["id"] == "goal_committed"
    assert store.created[0]["source"] == "goal-planner"
    assert store.created[0]["payload"]["goal_planning"]["session_id"] == "conv_plan"
    assert store.created[0]["payload"]["goal_planning"]["source_ids"] == ["jira"]
