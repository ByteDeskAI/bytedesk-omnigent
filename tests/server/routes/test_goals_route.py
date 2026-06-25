"""Tests for the goals-backlog read route: auth gate + serialization (BDP-2290).

The 401 path fires in ``require_user`` before the store, so it's self-contained;
the data path monkeypatches the store accessor so no runtime/conversation-store
init is needed (mirrors the governance route test's self-contained style).
"""
from __future__ import annotations

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

    def list_goals(self, *, status=None, owner_agent_id=None, **kwargs) -> list[Goal]:
        self.calls.append({"status": status, "owner": owner_agent_id, **kwargs})
        return self._goals


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
            "ready_only": False,
            "include_dependencies": True,
        }
    ]
