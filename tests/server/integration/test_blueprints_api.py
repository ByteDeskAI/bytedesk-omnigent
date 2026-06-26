"""Integration coverage for blueprint graph and run APIs."""

from __future__ import annotations

import hashlib
import io
import tarfile
from typing import Any

import httpx
import pytest
import yaml

from omnigent.runtime import get_agent_store, get_artifact_store
from omnigent.stores.agent_store import AgentStore
from omnigent.stores.artifact_store import ArtifactStore

pytestmark = pytest.mark.asyncio


def _bundle(config: dict[str, Any]) -> bytes:
    config_bytes = yaml.dump(config, sort_keys=False).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="config.yaml")
        info.size = len(config_bytes)
        tf.addfile(info, io.BytesIO(config_bytes))
    return buf.getvalue()


def _register_agent(
    *,
    agent_id: str,
    config: dict[str, Any],
    category: str = "workflow",
) -> None:
    agent_store = get_agent_store()
    artifact_store = get_artifact_store()
    assert artifact_store is not None
    assert isinstance(agent_store, AgentStore)
    assert isinstance(artifact_store, ArtifactStore)
    bundle = _bundle(config)
    key = f"{agent_id}/{hashlib.sha256(bundle).hexdigest()}"
    artifact_store.put(key, bundle)
    agent_store.create(
        agent_id,
        config["name"],
        key,
        description=config.get("description"),
    )
    agent_store.set_category(agent_id, category)


def _blueprint_config(
    name: str,
    nodes: list[dict[str, Any]],
    outputs: dict[str, Any],
) -> dict[str, Any]:
    return {
        "spec_version": 1,
        "name": name,
        "description": f"{name} test blueprint",
        "params": {"workflow": True},
        "executor": {"type": "blueprint"},
        "blueprint": {
            "name": name,
            "nodes": nodes,
            "outputs": outputs,
        },
    }


async def test_get_agent_blueprint_returns_static_graph(client: httpx.AsyncClient) -> None:
    _register_agent(
        agent_id="ag_blueprint_static",
        config=_blueprint_config(
            "static-blueprint",
            [
                {"id": "collect", "kind": "task"},
                {"id": "done", "kind": "output", "depends_on": ["collect"]},
            ],
            {"text": "done"},
        ),
    )

    resp = await client.get("/v1/agents/ag_blueprint_static/blueprint")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object"] == "blueprint"
    assert [node["id"] for node in body["nodes"]] == ["collect", "done"]
    assert body["edges"] == [{"id": "collect->done", "source": "collect", "target": "done"}]


async def test_blueprint_session_runs_without_runner_and_persists_events(
    client: httpx.AsyncClient,
) -> None:
    _register_agent(
        agent_id="ag_blueprint_run",
        config=_blueprint_config(
            "run-blueprint",
            [
                {
                    "id": "draft",
                    "kind": "task",
                    "output": {"text": "Motto for {{ $.input.text }}"},
                },
                {
                    "id": "final",
                    "kind": "output",
                    "depends_on": ["draft"],
                    "output": {"text": "{{ $.nodes.draft.output.text }}"},
                },
            ],
            {"text": "{{ $.nodes.final.output.text }}"},
        ),
    )
    session = (
        await client.post("/v1/sessions", json={"agent_id": "ag_blueprint_run"})
    ).json()

    event_resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "Support"}],
            },
        },
    )

    assert event_resp.status_code == 202, event_resp.text
    run_resp = await client.get(f"/v1/sessions/{session['id']}/blueprint-run")
    assert run_resp.status_code == 200, run_resp.text
    run = run_resp.json()
    assert run["status"] == "completed"
    assert {node["id"]: node["status"] for node in run["nodes"]}["final"] == "completed"

    items = (await client.get(f"/v1/sessions/{session['id']}/items")).json()["data"]
    assistant_texts = [
        _item_text(item)
        for item in items
        if item["type"] == "message" and _item_role(item) == "assistant"
    ]
    assert any("Motto for Support" in text for text in assistant_texts)


async def test_parent_blueprint_calls_child_blueprint_once(
    client: httpx.AsyncClient,
) -> None:
    _register_agent(
        agent_id="ag_child_blueprint",
        config=_blueprint_config(
            "child-blueprint",
            [
                {
                    "id": "ideas",
                    "kind": "output",
                    "output": "Clear eyes, kind hands",
                }
            ],
            {"text": "{{ $.nodes.ideas.output }}"},
        ),
    )
    _register_agent(
        agent_id="ag_parent_blueprint",
        config=_blueprint_config(
            "parent-blueprint",
            [
                {
                    "id": "collect",
                    "kind": "blueprint",
                    "target": "child-blueprint",
                    "input": "Collect motto ideas for {{ $.input.text }}",
                },
                {
                    "id": "final",
                    "kind": "output",
                    "depends_on": ["collect"],
                    "output": "Final motto: {{ $.nodes.collect.output }}",
                },
            ],
            {"text": "{{ $.nodes.final.output }}"},
        ),
    )
    parent = (
        await client.post("/v1/sessions", json={"agent_id": "ag_parent_blueprint"})
    ).json()

    resp = await client.post(
        f"/v1/sessions/{parent['id']}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "Ops"}],
            },
        },
    )

    assert resp.status_code == 202, resp.text
    children = (await client.get(f"/v1/sessions/{parent['id']}/child_sessions")).json()["data"]
    assert len(children) == 1
    assert children[0]["labels"]["omnigent.blueprint.node_id"] == "collect"

    parent_items = (await client.get(f"/v1/sessions/{parent['id']}/items")).json()["data"]
    meta_returns = [
        item
        for item in parent_items
        if item["type"] == "message" and _item_is_meta(item)
    ]
    assert len(meta_returns) == 1
    assert "child session" in _item_text(meta_returns[0])

    run = (await client.get(f"/v1/sessions/{parent['id']}/blueprint-run")).json()
    collect = next(node for node in run["nodes"] if node["id"] == "collect")
    assert collect["child_session_id"] == children[0]["id"]
    assert collect["status"] == "completed"


def _item_text(item: dict[str, Any]) -> str:
    payload = item.get("data") if isinstance(item.get("data"), dict) else item
    return "\n".join(
        str(block.get("text", ""))
        for block in payload.get("content", [])
        if isinstance(block, dict)
    )


def _item_role(item: dict[str, Any]) -> str | None:
    payload = item.get("data") if isinstance(item.get("data"), dict) else item
    role = payload.get("role")
    return str(role) if role is not None else None


def _item_is_meta(item: dict[str, Any]) -> bool:
    payload = item.get("data") if isinstance(item.get("data"), dict) else item
    return payload.get("is_meta") is True
