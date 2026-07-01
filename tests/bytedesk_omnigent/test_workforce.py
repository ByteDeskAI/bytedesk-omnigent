from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from bytedesk_omnigent.connectors.manifests import bytedesk_connector_manifests
from bytedesk_omnigent.connectors.providers import (
    AtlassianConnectorProvider,
    GoogleWorkspaceConnectorProvider,
)
from bytedesk_omnigent.connectors.registry import ConnectorRegistry
from bytedesk_omnigent.connectors.store import SqlAlchemyConnectorStore
from bytedesk_omnigent.routes.workforce import create_workforce_router
from bytedesk_omnigent.workforce import (
    SqlAlchemyWorkforceStore,
    disable_connector_grants_for_agent,
    disable_connector_grants_for_missing_agents,
    effective_workforce_for_agent,
    instruction_fragments,
    reconcile_connectors_for_agent,
    reconcile_tools_for_agent,
)
from omnigent.entities import Agent, HarnessAgent
from omnigent.errors import OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.bundles import bundle_location
from omnigent.spec.tar_utils import build_bundle_bytes
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.tools import ToolManager


class _AgentStore:
    def __init__(self, agents):
        self._agents = {agent.id: agent for agent in agents}

    def get(self, agent_id: str):
        return self._agents.get(agent_id)

    def list(self, limit=1000, after=None, before=None, order="asc", category=None):
        del limit, after, before, order
        data = list(self._agents.values())
        if category is not None:
            data = [agent for agent in data if agent.category == category]
        return SimpleNamespace(data=data)


class _AgentCache:
    def __init__(self, departments: dict[str, str | None]):
        self._departments = departments

    def load(self, agent_id: str, bundle_location: str, expand_env: bool = False):
        del bundle_location, expand_env
        department = self._departments.get(agent_id)
        params = {"department": department} if department else {}
        return SimpleNamespace(spec=SimpleNamespace(params=params))


def _employee(agent_id: str = "ag_maya") -> Agent:
    return Agent(
        id=agent_id,
        created_at=1,
        name=agent_id,
        bundle_location=f"{agent_id}/bundle",
    )


def _harness() -> HarnessAgent:
    return HarnessAgent(
        id="ag_codex",
        created_at=1,
        name="codex-native-ui",
        bundle_location="ag_codex/bundle",
    )


def _registry() -> ConnectorRegistry:
    return ConnectorRegistry(
        {m.provider: m for m in bytedesk_connector_manifests()},
        {
            "atlassian": AtlassianConnectorProvider(),
            "google_workspace": GoogleWorkspaceConnectorProvider(),
        },
    )


def _seed_template_agent_image(
    db_uri: str,
    tmp_path: Path,
    *,
    agent_id: str = "ag_maya",
    name: str = "platform-developer",
    department: str = "Engineering",
) -> tuple[SqlAlchemyAgentStore, LocalArtifactStore, AgentCache]:
    root = tmp_path / f"{agent_id}_image"
    root.mkdir()
    config = {
        "spec_version": 1,
        "name": name,
        "executor": {"type": "omnigent", "config": {"harness": "codex"}},
        "params": {"department": department},
    }
    (root / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    (root / "AGENTS.md").write_text("Use repo evidence.\n")
    bundle_bytes = build_bundle_bytes(root)
    location = bundle_location(agent_id, bundle_bytes)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    artifact_store.put(location, bundle_bytes)
    agent_store = SqlAlchemyAgentStore(db_uri)
    agent_store.create(agent_id, name=name, bundle_location=location)
    agent_cache = AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache")
    return agent_store, artifact_store, agent_cache


def test_workforce_store_resolves_effective_instruction_and_override(db_uri: str) -> None:
    store = SqlAlchemyWorkforceStore(db_uri)
    agent_store = _AgentStore([_employee()])
    agent_cache = _AgentCache({"ag_maya": "Engineering"})
    store.set_instruction(scope_kind="organization", scope_id=None, body="Follow org rules.")
    store.set_instruction(scope_kind="department", scope_id="Engineering", body="Ship carefully.")
    store.set_instruction(scope_kind="agent", scope_id="ag_maya", body="Own the final answer.")
    store.upsert_skill_assignment(
        scope_kind="department",
        scope_id="Engineering",
        skill_name="code-review",
        source="skills",
        source_ref="ByteDeskAI/skills@code-review",
        enabled=True,
    )
    store.upsert_agent_override(
        agent_id="ag_maya",
        item_kind="skill",
        item_key="code-review",
        enabled=False,
    )

    effective = effective_workforce_for_agent(
        "ag_maya",
        store=store,
        agent_store=agent_store,
        agent_cache=agent_cache,
    )

    assert [row["body"] for row in effective["instructions"]] == [
        "Follow org rules.",
        "Ship carefully.",
        "Own the final answer.",
    ]
    assert effective["skills"][0]["skillName"] == "code-review"
    assert effective["skills"][0]["enabled"] is False


def test_workforce_store_resolves_builtin_tool_inheritance_and_overrides(db_uri: str) -> None:
    store = SqlAlchemyWorkforceStore(db_uri)
    agent_store = _AgentStore([_employee()])
    agent_cache = _AgentCache({"ag_maya": "Engineering"})
    store.upsert_tool_assignment(
        scope_kind="organization",
        scope_id=None,
        tool_key="web_search",
        enabled=True,
    )
    store.upsert_tool_assignment(
        scope_kind="organization",
        scope_id=None,
        tool_key="sys_os_shell",
        enabled=True,
    )
    store.upsert_tool_assignment(
        scope_kind="department",
        scope_id="Engineering",
        tool_key="sys_os_shell",
        enabled=False,
    )
    store.upsert_agent_override(
        agent_id="ag_maya",
        item_kind="tool",
        item_key="sys_os_write",
        enabled=True,
    )

    effective = effective_workforce_for_agent(
        "ag_maya",
        store=store,
        agent_store=agent_store,
        agent_cache=agent_cache,
    )
    tools = {row["toolKey"]: row for row in effective["tools"]}

    assert tools["web_search"]["enabled"] is True
    assert tools["sys_os_shell"]["enabled"] is False
    assert tools["sys_os_shell"]["inheritedFrom"][-1]["scopeKind"] == "department"
    assert tools["sys_os_write"]["enabled"] is True
    assert tools["sys_os_write"]["inherited"] is False


def test_instruction_fragments_apply_to_employees_not_harnesses(
    monkeypatch,
    db_uri: str,
) -> None:
    store = SqlAlchemyWorkforceStore(db_uri)
    store.set_instruction(scope_kind="organization", scope_id=None, body="Org guidance.")
    store.set_instruction(scope_kind="department", scope_id="Engineering", body="Eng guidance.")
    store.set_instruction(scope_kind="agent", scope_id="ag_maya", body="Agent guidance.")
    agent_store = _AgentStore([_employee(), _harness()])
    agent_cache = _AgentCache({"ag_maya": "Engineering", "ag_codex": "Engineering"})
    monkeypatch.setattr("bytedesk_omnigent.workforce.get_workforce_store", lambda: store)
    monkeypatch.setattr("omnigent.runtime.get_agent_store", lambda: agent_store)
    monkeypatch.setattr("omnigent.runtime.get_agent_cache", lambda: agent_cache)

    employee_fragments = instruction_fragments(
        agent_id="ag_maya",
        spec=SimpleNamespace(name="maya"),
    )
    harness_fragments = instruction_fragments(
        agent_id="ag_codex",
        spec=SimpleNamespace(name="codex"),
    )

    assert employee_fragments == [
        "Organization instructions:\nOrg guidance.",
        "Department: engineering instructions:\nEng guidance.",
        "Agent instructions:\nAgent guidance.",
    ]
    assert harness_fragments == []


def test_instruction_fragments_skip_no_department_employee_artifacts(
    monkeypatch,
    db_uri: str,
) -> None:
    store = SqlAlchemyWorkforceStore(db_uri)
    store.set_instruction(scope_kind="organization", scope_id=None, body="Org guidance.")
    agent_store = _AgentStore([_employee("ag_hello_world")])
    agent_cache = _AgentCache({"ag_hello_world": None})
    monkeypatch.setattr("bytedesk_omnigent.workforce.get_workforce_store", lambda: store)
    monkeypatch.setattr("omnigent.runtime.get_agent_store", lambda: agent_store)
    monkeypatch.setattr("omnigent.runtime.get_agent_cache", lambda: agent_cache)

    fragments = instruction_fragments(
        agent_id="ag_hello_world",
        spec=SimpleNamespace(name="hello_world"),
    )

    assert fragments == []


def test_instruction_fragments_skip_before_runtime_initialization(monkeypatch) -> None:
    monkeypatch.setattr(
        "bytedesk_omnigent.workforce.get_workforce_store",
        lambda: (_ for _ in ()).throw(RuntimeError("runtime not initialized — call init() first")),
    )

    assert instruction_fragments(agent_id="ag_maya", spec=SimpleNamespace(name="maya")) == []


def test_connector_reconcile_materializes_inherited_grants_and_disable_override(
    monkeypatch,
    db_uri: str,
) -> None:
    workforce_store = SqlAlchemyWorkforceStore(db_uri)
    connector_store = SqlAlchemyConnectorStore(db_uri)
    conn = connector_store.upsert_connection(
        provider="google_workspace",
        display_name="Workspace",
        auth_type="google_domain_wide_delegation",
        scopes=[],
        metadata={"delegated_subject": "admin@bytedesk.test"},
        secret_ref="secret-ref",
    )
    GoogleWorkspaceConnectorProvider().bootstrap_services(connector_store, conn)
    workforce_store.upsert_connector_assignment(
        scope_kind="department",
        scope_id="Engineering",
        connection_id=conn.id,
        service_key="drive",
        tool_key="search",
        enabled=True,
    )
    agent_store = _AgentStore([_employee()])
    agent_cache = _AgentCache({"ag_maya": "Engineering"})
    materialized: list[str] = []
    monkeypatch.setattr(
        "bytedesk_omnigent.connectors.store.get_connector_store",
        lambda: connector_store,
    )
    monkeypatch.setattr(
        "bytedesk_omnigent.connectors.grants.materialize_agent_connector_grant",
        lambda **kwargs: materialized.append(kwargs["agent_id"]),
    )

    reconcile_connectors_for_agent(
        "ag_maya",
        store=workforce_store,
        agent_store=agent_store,
        agent_cache=agent_cache,
    )

    grant = connector_store.list_agent_grants(agent_id="ag_maya")[0]
    assert (grant.service_key, grant.tool_key, grant.enabled) == ("drive", "search", True)
    assert grant.metadata["workforceManaged"] is True
    assert materialized == ["ag_maya"]

    workforce_store.upsert_agent_override(
        agent_id="ag_maya",
        item_kind="connector",
        item_key=f"{conn.id}:drive:search",
        enabled=False,
    )
    reconcile_connectors_for_agent(
        "ag_maya",
        store=workforce_store,
        agent_store=agent_store,
        agent_cache=agent_cache,
    )

    disabled = connector_store.list_agent_grants(agent_id="ag_maya")[0]
    assert disabled.enabled is False
    assert disabled.metadata["override"]["enabled"] is False


def test_tool_reconcile_materializes_agent_image_and_runtime_filter(
    db_uri: str,
    tmp_path: Path,
) -> None:
    workforce_store = SqlAlchemyWorkforceStore(db_uri)
    agent_store, artifact_store, agent_cache = _seed_template_agent_image(db_uri, tmp_path)
    workforce_store.upsert_tool_assignment(
        scope_kind="organization",
        scope_id=None,
        tool_key="web_search",
        enabled=True,
    )
    workforce_store.upsert_tool_assignment(
        scope_kind="organization",
        scope_id=None,
        tool_key="sys_os_write",
        enabled=True,
    )
    workforce_store.upsert_tool_assignment(
        scope_kind="department",
        scope_id="Engineering",
        tool_key="sys_os_shell",
        enabled=False,
    )

    reconcile_tools_for_agent(
        "ag_maya",
        store=workforce_store,
        agent_store=agent_store,
        agent_cache=agent_cache,
        artifact_store=artifact_store,
    )

    updated = agent_store.get("ag_maya")
    assert updated is not None
    loaded = agent_cache.load(updated.id, updated.bundle_location, expand_env=False)
    config = yaml.safe_load((loaded.workdir / "config.yaml").read_text())
    builtins = config["tools"]["builtins"]
    permissions = config["params"]["managed_tool_permissions"]
    materialized = {
        item.item_key: item.active
        for item in workforce_store.list_materializations(agent_id="ag_maya", item_kind="tool")
    }

    assert "web_search" in builtins
    assert config["os_env"]["sandbox"]["type"] == "none"
    assert permissions["enabled"] == ["sys_os_write", "web_search"]
    assert "sys_os_shell" in permissions["managed"]
    assert materialized["web_search"] is True
    assert materialized["sys_os_shell"] is False

    runtime_names = set(ToolManager(loaded.spec).get_tool_names())
    assert "web_search" in runtime_names
    assert "sys_os_write" in runtime_names
    assert "sys_os_shell" not in runtime_names


def test_disable_stale_connector_grants_for_deleted_and_missing_agents(
    db_uri: str,
) -> None:
    connector_store = SqlAlchemyConnectorStore(db_uri)
    conn = connector_store.upsert_connection(
        provider="google_workspace",
        display_name="Workspace",
        auth_type="google_domain_wide_delegation",
        scopes=[],
    )
    connector_store.upsert_agent_grant(
        connection_id=conn.id,
        agent_id="ag_missing",
        service_key="drive",
        tool_key="search",
        enabled=True,
        metadata={"source": "legacy-direct"},
    )
    connector_store.upsert_agent_grant(
        connection_id=conn.id,
        agent_id="ag_deleted",
        service_key="drive",
        tool_key="read",
        enabled=True,
    )
    connector_store.upsert_agent_grant(
        connection_id=conn.id,
        agent_id="ag_maya",
        service_key="drive",
        tool_key="create",
        enabled=True,
    )
    agent_store = _AgentStore([_employee("ag_maya")])

    deleted = disable_connector_grants_for_agent(
        "ag_deleted",
        connector_store=connector_store,
        reason="agent_deleted",
    )
    missing = disable_connector_grants_for_missing_agents(
        agent_store=agent_store,
        connector_store=connector_store,
    )

    assert deleted == ["ag_deleted"]
    assert missing == ["ag_missing"]
    grants = {grant.agent_id: grant for grant in connector_store.list_agent_grants()}
    assert grants["ag_missing"].enabled is False
    assert grants["ag_missing"].status == "disabled"
    assert grants["ag_missing"].metadata["source"] == "legacy-direct"
    assert grants["ag_missing"].metadata["staleMissingAgent"] is True
    assert grants["ag_deleted"].enabled is False
    assert grants["ag_deleted"].metadata["staleReason"] == "agent_deleted"
    assert grants["ag_maya"].enabled is True


def _app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(
        OmnigentError,
        lambda request, exc: JSONResponse(
            status_code=exc.http_status,
            content={"error": exc.code},
        ),
    )
    app.include_router(create_workforce_router(), prefix="/v1")
    return app


def test_workforce_route_persists_department_connector_scope(
    monkeypatch,
    db_uri: str,
) -> None:
    workforce_store = SqlAlchemyWorkforceStore(db_uri)
    connector_store = SqlAlchemyConnectorStore(db_uri)
    conn = connector_store.upsert_connection(
        provider="google_workspace",
        display_name="Workspace",
        auth_type="google_domain_wide_delegation",
        scopes=[],
        secret_ref="secret-ref",
    )
    GoogleWorkspaceConnectorProvider().bootstrap_services(connector_store, conn)
    monkeypatch.setattr(
        "bytedesk_omnigent.routes.workforce.get_workforce_store",
        lambda: workforce_store,
    )
    monkeypatch.setattr(
        "bytedesk_omnigent.routes.workforce.get_connector_store",
        lambda: connector_store,
    )
    monkeypatch.setattr(
        "bytedesk_omnigent.connectors.registry.build_connector_registry",
        _registry,
    )

    resp = TestClient(_app()).post(
        "/v1/workforce/scopes/department/Engineering/connectors",
        json={
            "connectionId": conn.id,
            "tools": ["drive:search"],
            "reconcile": False,
        },
    )

    assert resp.status_code == 200, resp.text
    assignment = resp.json()["assignments"][0]
    assert assignment["scopeId"] == "engineering"
    assert assignment["connectionId"] == conn.id
    assert assignment["serviceKey"] == "drive"
    assert assignment["toolKey"] == "search"


def test_workforce_route_persists_department_tool_scope(
    monkeypatch,
    db_uri: str,
) -> None:
    workforce_store = SqlAlchemyWorkforceStore(db_uri)
    monkeypatch.setattr(
        "bytedesk_omnigent.routes.workforce.get_workforce_store",
        lambda: workforce_store,
    )

    resp = TestClient(_app()).post(
        "/v1/workforce/scopes/department/Engineering/tools",
        json={
            "toolKey": "sys_os_write",
            "enabled": True,
            "reconcile": False,
        },
    )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["assignment"]["scopeId"] == "engineering"
    assert payload["assignment"]["toolKey"] == "sys_os_write"
    assert payload["scope"]["tools"][0]["toolKey"] == "sys_os_write"


def test_workforce_route_persists_agent_instructions(
    monkeypatch,
    db_uri: str,
) -> None:
    workforce_store = SqlAlchemyWorkforceStore(db_uri)
    agent_store = _AgentStore([_employee("ag_maya")])
    agent_cache = _AgentCache({"ag_maya": "Engineering"})
    monkeypatch.setattr(
        "bytedesk_omnigent.routes.workforce.get_workforce_store",
        lambda: workforce_store,
    )
    monkeypatch.setattr("bytedesk_omnigent.workforce.get_workforce_store", lambda: workforce_store)
    monkeypatch.setattr("omnigent.runtime.get_agent_store", lambda: agent_store)
    monkeypatch.setattr("omnigent.runtime.get_agent_cache", lambda: agent_cache)

    resp = TestClient(_app()).put(
        "/v1/workforce/agents/ag_maya/instructions",
        json={"body": "Prefer current repo evidence before answering."},
    )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["instruction"]["scopeKind"] == "agent"
    assert payload["instruction"]["scopeId"] == "ag_maya"
    assert payload["effective"]["instructions"][0]["body"] == (
        "Prefer current repo evidence before answering."
    )
