from __future__ import annotations

import json
from types import SimpleNamespace

from bytedesk_omnigent.connectors.store import SqlAlchemyConnectorStore
from bytedesk_omnigent.org_tool_intercept import execute_org_tool, is_org_tool
from bytedesk_omnigent.workforce import SqlAlchemyWorkforceStore
from omnigent.entities import Agent, HarnessAgent, SystemAgent, Workflow


class _AgentStore:
    def __init__(self, agents):
        self._agents = {agent.id: agent for agent in agents}
        self._capabilities: dict[str, tuple[str, ...]] = {}

    def get(self, agent_id: str):
        return self._agents.get(agent_id)

    def list(self, limit=1000, after=None, before=None, order="asc", category=None):
        del limit, after, before, order
        data = list(self._agents.values())
        if category is not None:
            data = [agent for agent in data if agent.category == category]
        return SimpleNamespace(data=data)

    def get_capabilities(self, agent_id: str) -> tuple[str, ...]:
        return self._capabilities.get(agent_id, ())

    def set_capabilities(self, agent_id: str, caps: tuple[str, ...]) -> None:
        self._capabilities[agent_id] = caps


class _AgentCache:
    def __init__(self, params: dict[str, dict]):
        self._params = params

    def load(self, agent_id: str, bundle_location: str, expand_env: bool = False):
        del bundle_location, expand_env
        return SimpleNamespace(spec=SimpleNamespace(params=self._params.get(agent_id, {})))


def _employee(agent_id: str, name: str) -> Agent:
    return Agent(id=agent_id, created_at=1, name=name, bundle_location=f"{agent_id}/bundle")


def test_org_tool_predicate() -> None:
    assert is_org_tool("org__get_chart")
    assert is_org_tool("org__find_agent")
    assert is_org_tool("org__get_effective_access")
    assert not is_org_tool("org__delete_everything")
    assert not is_org_tool("memory__get")


def test_get_chart_returns_employee_org_chart(monkeypatch) -> None:
    agent_store = _AgentStore(
        [
            _employee("ag_maya", "chief-of-staff"),
            _employee("ag_samir", "engineering-runbook"),
            HarnessAgent(
                id="ag_codex",
                created_at=1,
                name="codex-native-ui",
                bundle_location="ag_codex/bundle",
            ),
            SystemAgent(
                id="ag_goal",
                created_at=1,
                name="goal-commander",
                bundle_location="ag_goal/bundle",
            ),
            Workflow(
                id="ag_flow",
                created_at=1,
                name="weekly-flow",
                bundle_location="ag_flow/bundle",
            ),
        ]
    )
    agent_store.set_capabilities("ag_samir", ("docs", "engineering"))
    agent_cache = _AgentCache(
        {
            "ag_maya": {
                "displayName": "Maya Chen",
                "department": "Operations",
                "title": "Chief of Staff",
            },
            "ag_samir": {
                "displayName": "Samir Patel",
                "department": "Engineering",
                "title": "Runbook Engineer",
                "managers": [{"id": "ag_maya", "displayName": "Maya Chen"}],
            },
        }
    )
    monkeypatch.setattr("omnigent.runtime.get_agent_store", lambda: agent_store)
    monkeypatch.setattr("omnigent.runtime.get_agent_cache", lambda: agent_cache)

    out = execute_org_tool("org__get_chart", {}, caller_agent_id="ag_maya")
    payload = json.loads(out)

    assert payload["counts"] == {
        "employees": 2,
        "system": 0,
        "harness": 0,
        "workflow": 0,
    }
    assert [row["department"] for row in payload["departments"]] == [
        "Engineering",
        "Operations",
    ]
    assert payload["departments"][0]["agents"][0]["displayName"] == "Samir Patel"
    assert payload["departments"][0]["agents"][0]["capabilities"] == [
        "docs",
        "engineering",
    ]


def test_get_chart_can_include_non_employee_tiers(monkeypatch) -> None:
    agent_store = _AgentStore(
        [
            _employee("ag_maya", "chief-of-staff"),
            HarnessAgent(
                id="ag_codex",
                created_at=1,
                name="codex-native-ui",
                bundle_location="ag_codex/bundle",
            ),
        ]
    )
    agent_cache = _AgentCache(
        {
            "ag_maya": {"displayName": "Maya Chen", "department": "Operations"},
            "ag_codex": {"displayName": "Codex Harness"},
        }
    )
    monkeypatch.setattr("omnigent.runtime.get_agent_store", lambda: agent_store)
    monkeypatch.setattr("omnigent.runtime.get_agent_cache", lambda: agent_cache)

    out = execute_org_tool(
        "org__get_chart",
        {"include_harness": True},
        caller_agent_id="ag_maya",
    )
    payload = json.loads(out)

    assert payload["counts"]["employees"] == 1
    assert payload["counts"]["harness"] == 1
    assert any(agent["category"] == "harness" for agent in payload["agents"])


def test_find_agent_searches_current_roster(monkeypatch) -> None:
    agent_store = _AgentStore(
        [
            _employee("ag_maya", "chief-of-staff"),
            _employee("ag_samir", "engineering-runbook"),
        ]
    )
    agent_cache = _AgentCache(
        {
            "ag_maya": {
                "displayName": "Maya Chen",
                "department": "Operations",
                "title": "Chief of Staff",
            },
            "ag_samir": {
                "displayName": "Samir Patel",
                "department": "Engineering",
                "title": "Runbook Engineer",
            },
        }
    )
    monkeypatch.setattr("omnigent.runtime.get_agent_store", lambda: agent_store)
    monkeypatch.setattr("omnigent.runtime.get_agent_cache", lambda: agent_cache)

    out = execute_org_tool(
        "org__find_agent",
        {"query": "runbook", "department": "engineering"},
        caller_agent_id="ag_maya",
    )
    payload = json.loads(out)

    assert [row["agentId"] for row in payload["matches"]] == ["ag_samir"]


def test_get_effective_access_reports_workforce_and_direct_grants(
    monkeypatch,
    db_uri: str,
) -> None:
    workforce_store = SqlAlchemyWorkforceStore(db_uri)
    connector_store = SqlAlchemyConnectorStore(db_uri)
    connection = connector_store.upsert_connection(
        provider="google_workspace",
        display_name="Workspace",
        auth_type="google_domain_wide_delegation",
        scopes=[],
    )
    connector_store.upsert_agent_grant(
        connection_id=connection.id,
        agent_id="ag_maya",
        service_key="drive",
        tool_key="search",
        enabled=True,
    )
    workforce_store.set_instruction(
        scope_kind="organization",
        scope_id=None,
        body="Follow org rules.",
    )
    agent_store = _AgentStore([_employee("ag_maya", "chief-of-staff")])
    agent_cache = _AgentCache(
        {"ag_maya": {"displayName": "Maya Chen", "department": "Operations"}}
    )
    monkeypatch.setattr("omnigent.runtime.get_agent_store", lambda: agent_store)
    monkeypatch.setattr("omnigent.runtime.get_agent_cache", lambda: agent_cache)
    monkeypatch.setattr(
        "bytedesk_omnigent.org_tool_intercept.get_workforce_store",
        lambda: workforce_store,
    )
    monkeypatch.setattr(
        "bytedesk_omnigent.org_tool_intercept.get_connector_store",
        lambda: connector_store,
    )

    out = execute_org_tool(
        "org__get_effective_access",
        {},
        caller_agent_id="ag_maya",
    )
    payload = json.loads(out)

    assert payload["agentId"] == "ag_maya"
    assert payload["workforce"]["instructions"][0]["body"] == "Follow org rules."
    assert payload["directConnectorGrantSummary"] == {
        "active": 1,
        "disabled": 0,
        "total": 1,
    }
    assert payload["directConnectorGrants"][0]["serviceKey"] == "drive"
