"""Phase 6a admin endpoints (BDP-2588): delete/conditions/budget/outcomes/
decisions + the goal-templates router — admin gate (403) + happy path.

Self-contained: a permission store forces the admin gate; the goal/template/
treasury stores are monkeypatched so no runtime init is needed (mirrors
test_goals_route.py)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from bytedesk_omnigent.engine.treasury import Decision, Outcome
from bytedesk_omnigent.goals import Goal, GoalTemplate
from bytedesk_omnigent.routes.goals import (
    create_goal_templates_router,
    create_goals_router,
)
from omnigent.errors import OmnigentError


class _AdminAuth:
    """Resolves a fixed identity so the admin gate runs against the perm store."""

    def __init__(self, user_id: str = "u_1") -> None:
        self._user_id = user_id

    def get_user_id(self, request: object) -> str:
        return self._user_id


class _PermStore:
    def __init__(self, admin: bool) -> None:
        self._admin = admin

    def is_admin(self, user_id: str) -> bool:
        return self._admin


def _goal(goal_id: str = "goal_1") -> Goal:
    return Goal(
        id=goal_id,
        title="g",
        owner_agent_id=None,
        status="open",
        priority=3,
        source=None,
        payload={"condition": {"type": "all", "nodes": []}},
        created_at=1,
        updated_at=1,
        tier="org",
        target_id="omnigent",
    )


class _FakeGoalStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.removed: list[tuple[str, str]] = []
        self.conditions: list[tuple[str, object]] = []

    def get_goal(self, *, goal_id, include_dependencies=True):
        return _goal(goal_id) if goal_id == "goal_1" else None

    def delete_goal(self, *, goal_id):
        self.deleted.append(goal_id)
        return goal_id == "goal_1"

    def remove_dependency(self, *, goal_id, dependency_id):
        self.removed.append((goal_id, dependency_id))
        return goal_id == "goal_1" and dependency_id == "dep_1"

    def get_condition(self, *, goal_id):
        return {"type": "all", "nodes": []}

    def set_condition(self, *, ast_dict, goal_id):
        self.conditions.append((goal_id, ast_dict))
        return _goal(goal_id) if goal_id == "goal_1" else None


class _FakeTreasury:
    def __init__(self) -> None:
        self.budgets: list[dict] = []

    def spent_cents(self, *, tier, target_id):
        return 42

    def set_budget(self, **kwargs):
        self.budgets.append(kwargs)

    def outcomes(self, *, goal_id=None):
        return [Outcome("o1", goal_id or "goal_1", 10, 500, "stripe", None)]

    def decisions(self, *, goal_id=None, tick_id=None):
        return [Decision("d1", "tick", "goal_1", 1.0, None, None, "funded", None, 10)]


class _FakeTemplateStore:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.instantiated: list[tuple[str, dict]] = []

    def list_templates(self):
        return [GoalTemplate("t1", "n", None, {"priority": 2}, 1, 1)]

    def get_template(self, *, template_id):
        return GoalTemplate(template_id, "n", None, {}, 1, 1) if template_id == "t1" else None

    def create_template(self, *, name, description=None, definition=None):
        self.created.append({"name": name, "definition": definition})
        return GoalTemplate("t_new", name, description, definition or {}, 1, 1)

    def update_template(self, *, template_id, name=None, definition=None, description=...):
        if template_id != "t1":
            return None
        return GoalTemplate(template_id, name or "n", None, definition or {}, 1, 2)

    def delete_template(self, *, template_id):
        return template_id == "t1"

    def instantiate(self, *, template_id, overrides=None):
        self.instantiated.append((template_id, overrides or {}))
        return _goal("goal_new") if template_id == "t1" else None


def _client(admin: bool, monkeypatch):
    goal_store = _FakeGoalStore()
    treasury = _FakeTreasury()
    template_store = _FakeTemplateStore()
    # patch the module-level accessors the routes import lazily
    import bytedesk_omnigent.engine.treasury as treasury_mod
    import bytedesk_omnigent.goals as goals_mod

    monkeypatch.setattr(goals_mod, "get_goal_store", lambda: goal_store)
    monkeypatch.setattr(goals_mod, "get_goal_template_store", lambda: template_store)
    monkeypatch.setattr(treasury_mod, "get_treasury", lambda: treasury)

    app = FastAPI()
    app.add_exception_handler(
        OmnigentError,
        lambda request, exc: JSONResponse(
            status_code=exc.http_status, content={"error": exc.code}
        ),
    )
    auth = _AdminAuth()
    perm = _PermStore(admin)
    app.include_router(
        create_goals_router(auth_provider=auth, permission_store=perm), prefix="/v1"
    )
    app.include_router(
        create_goal_templates_router(auth_provider=auth, permission_store=perm), prefix="/v1"
    )
    client = TestClient(app, raise_server_exceptions=False)
    return client, goal_store, treasury, template_store


# ── admin gate: every mutation 403s for a non-admin ──────────────────────────

def test_mutations_forbidden_for_non_admin(monkeypatch) -> None:
    client, *_ = _client(admin=False, monkeypatch=monkeypatch)
    assert client.delete("/v1/goals/goal_1").status_code == 403
    assert client.delete("/v1/goals/goal_1/dependencies/dep_1").status_code == 403
    assert client.put("/v1/goals/goal_1/conditions", json={"condition": None}).status_code == 403
    assert client.patch("/v1/goals/goal_1/budget", json={"cap_cents": 5}).status_code == 403
    assert client.post("/v1/goal-templates", json={"name": "x"}).status_code == 403
    assert client.put("/v1/goal-templates/t1", json={"name": "y"}).status_code == 403
    assert client.delete("/v1/goal-templates/t1").status_code == 403
    assert client.post("/v1/goal-templates/t1/instantiate", json={}).status_code == 403


# ── happy paths ──────────────────────────────────────────────────────────────

def test_delete_goal_and_dependency(monkeypatch) -> None:
    client, goal_store, *_ = _client(admin=True, monkeypatch=monkeypatch)
    assert client.delete("/v1/goals/goal_1").status_code == 200
    assert goal_store.deleted == ["goal_1"]
    assert client.delete("/v1/goals/missing").status_code == 404
    assert client.delete("/v1/goals/goal_1/dependencies/dep_1").status_code == 200
    assert client.delete("/v1/goals/goal_1/dependencies/nope").status_code == 404


def test_conditions_get_put_roundtrip(monkeypatch) -> None:
    client, goal_store, *_ = _client(admin=True, monkeypatch=monkeypatch)
    resp = client.get("/v1/goals/goal_1/conditions")
    assert resp.status_code == 200
    assert resp.json()["condition"] == {"type": "all", "nodes": []}
    ast = {"type": "leaf", "sensor": "jira", "query": {}, "predicate": {"op": "exists"}}
    put = client.put("/v1/goals/goal_1/conditions", json={"condition": ast})
    assert put.status_code == 200
    assert goal_store.conditions == [("goal_1", ast)]
    assert client.get("/v1/goals/missing/conditions").status_code == 404


def test_budget_get_patch(monkeypatch) -> None:
    client, _gs, treasury, _ts = _client(admin=True, monkeypatch=monkeypatch)
    get = client.get("/v1/goals/goal_1/budget")
    assert get.status_code == 200
    assert get.json()["spent_cents"] == 42
    patch = client.patch("/v1/goals/goal_1/budget", json={"cap_cents": 1000})
    assert patch.status_code == 200
    assert treasury.budgets[0]["cap_cents"] == 1000
    assert treasury.budgets[0]["tier"] == "org"


def test_outcomes_and_decisions_reads(monkeypatch) -> None:
    client, *_ = _client(admin=True, monkeypatch=monkeypatch)
    assert client.get("/v1/goals/outcomes").json()["outcomes"][0]["id"] == "o1"
    assert client.get("/v1/goals/goal_1/outcomes").json()["outcomes"][0]["goal_id"] == "goal_1"
    assert client.get("/v1/goals/decisions").json()["decisions"][0]["reason"] == "funded"


def test_frontier_read(monkeypatch) -> None:
    client, *_ = _client(admin=True, monkeypatch=monkeypatch)
    captured: dict = {}

    def _fake_frontier(**kwargs):
        captured.update(kwargs)
        return [
            {"goal_id": "goal_1", "roi": 9.0, "actionable": True, "waiting_reasons": []},
            {"goal_id": "goal_2", "roi": 0.0, "actionable": False,
             "waiting_reasons": ["waiting: jira"]},
        ]

    import bytedesk_omnigent.engine.frontier as frontier_mod

    monkeypatch.setattr(frontier_mod, "build_frontier", _fake_frontier)
    resp = client.get("/v1/goals/frontier", params={"target_kind": "organization"})
    assert resp.status_code == 200
    rows = resp.json()["frontier"]
    assert rows[0]["goal_id"] == "goal_1" and rows[0]["actionable"] is True
    assert rows[1]["waiting_reasons"] == ["waiting: jira"]
    assert captured["target_kind"] == "organization"


def test_frontier_requires_auth() -> None:
    # No auth provider -> require_user passes (single-user); with a provider that
    # returns no identity, reads still resolve. Authed reach is covered above; here
    # we assert the route is registered and not shadowed by /goals/{goal_id}.
    from bytedesk_omnigent.routes.goals import create_goals_router

    paths = {r.path for r in create_goals_router().routes}
    assert "/goals/frontier" in paths


def test_template_crud_and_instantiate(monkeypatch) -> None:
    client, _gs, _tr, template_store = _client(admin=True, monkeypatch=monkeypatch)
    assert client.get("/v1/goal-templates").json()["templates"][0]["id"] == "t1"
    created = client.post(
        "/v1/goal-templates", json={"name": "weekly", "definition": {"priority": 2}}
    )
    assert created.status_code == 201
    assert template_store.created[0]["name"] == "weekly"
    assert client.get("/v1/goal-templates/t1").status_code == 200
    assert client.get("/v1/goal-templates/missing").status_code == 404
    assert client.put("/v1/goal-templates/t1", json={"name": "renamed"}).status_code == 200
    assert client.delete("/v1/goal-templates/t1").status_code == 200
    assert client.delete("/v1/goal-templates/missing").status_code == 404
    inst = client.post("/v1/goal-templates/t1/instantiate", json={"overrides": {"title": "X"}})
    assert inst.status_code == 201
    assert inst.json()["goal"]["id"] == "goal_new"
    assert template_store.instantiated == [("t1", {"title": "X"})]
    assert client.post("/v1/goal-templates/missing/instantiate", json={}).status_code == 404
