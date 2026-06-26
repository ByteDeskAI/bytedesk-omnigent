"""Blueprint graph and run-state projections."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from omnigent.spec.types import BlueprintNode, BlueprintSpec


def blueprint_to_graph(
    blueprint: BlueprintSpec,
    *,
    agent_id: str | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """
    Project a parsed blueprint into a static graph response.

    :param blueprint: Parsed blueprint spec.
    :param agent_id: Optional owning agent id.
    :param agent_name: Optional owning agent name.
    :returns: JSON-serializable graph with nodes and dependency edges.
    """
    nodes, edges = _nodes_to_graph(blueprint.nodes)
    return {
        "object": "blueprint",
        "agent_id": agent_id,
        "agent_name": agent_name,
        "name": blueprint.name,
        "description": blueprint.description,
        "version": blueprint.version,
        "nodes": nodes,
        "edges": edges,
        "outputs": blueprint.outputs,
    }


def _nodes_to_graph(
    nodes: list[BlueprintNode],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    graph_nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    for node in nodes:
        graph_node = _node_to_graph(node)
        graph_nodes.append(graph_node)
        for dep in node.depends_on:
            edges.append({"id": f"{dep}->{node.id}", "source": dep, "target": node.id})
    return graph_nodes, edges


def _node_to_graph(node: BlueprintNode) -> dict[str, Any]:
    graph_node: dict[str, Any] = {
        "id": node.id,
        "kind": node.kind,
        "depends_on": list(node.depends_on),
        "target": node.target,
        "when": node.when,
        "input": node.input,
        "return": node.return_mapping,
        "output": node.output,
        "metadata": dict(node.metadata),
    }
    if node.loop is not None:
        body_nodes, body_edges = _nodes_to_graph(node.loop.body)
        graph_node["loop"] = {
            "max_iterations": node.loop.max_iterations,
            "until": node.loop.until,
            "on_exhausted": node.loop.on_exhausted,
            "reuse_session": node.loop.reuse_session,
            "nodes": body_nodes,
            "edges": body_edges,
        }
    return graph_node


def blueprint_events_to_run(items: Iterable[Any]) -> dict[str, Any]:
    """
    Aggregate persisted ``blueprint_event`` items into a live run snapshot.

    :param items: Conversation items, newest or oldest order accepted.
    :returns: JSON-serializable run state.
    """
    events: list[dict[str, Any]] = []
    node_states: dict[str, dict[str, Any]] = {}
    loop_iterations: list[dict[str, Any]] = []
    run_id: str | None = None
    status = "pending"
    for item in sorted(items, key=lambda value: getattr(value, "created_at", 0)):
        data_obj = getattr(item, "data", None)
        if data_obj is None:
            continue
        data = data_obj.model_dump() if hasattr(data_obj, "model_dump") else dict(data_obj)
        if data.get("blueprint_run_id"):
            run_id = str(data["blueprint_run_id"])
        event = {
            "id": getattr(item, "id", None),
            "created_at": getattr(item, "created_at", None),
            **data,
        }
        events.append(event)
        event_type = data.get("event_type")
        if event_type == "run_started":
            status = "running"
        elif event_type == "run_completed":
            status = "completed"
        elif event_type == "run_failed":
            status = "failed"
        elif event_type == "node_status" and data.get("node_id"):
            node_id = str(data["node_id"])
            node_states[node_id] = {
                "id": node_id,
                "kind": data.get("node_kind"),
                "status": data.get("status"),
                "loop_iteration": data.get("loop_iteration"),
                "child_session_id": data.get("child_session_id"),
                "payload": data.get("payload") or {},
                "updated_at": getattr(item, "created_at", None),
            }
        elif event_type == "loop_iteration":
            loop_iterations.append(event)
    return {
        "object": "blueprint_run",
        "blueprint_run_id": run_id,
        "status": status,
        "nodes": list(node_states.values()),
        "loop_iterations": loop_iterations,
        "events": events,
    }
