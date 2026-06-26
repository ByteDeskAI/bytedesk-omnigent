"""Deterministic blueprint runner."""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from omnigent.spec.types import BlueprintNode, BlueprintSpec

NodeStatus = Literal["pending", "running", "waiting", "completed", "failed", "skipped"]


@dataclass(frozen=True)
class ChildDispatchResult:
    """Result returned by a host-provided child-session dispatcher."""

    status: NodeStatus
    child_session_id: str | None = None
    output: Any | None = None
    error: str | None = None


@dataclass
class BlueprintRunResult:
    """Terminal result of a blueprint run."""

    blueprint_run_id: str
    status: Literal["completed", "failed", "waiting"]
    output: Any | None = None
    node_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str | None = None


EmitCallback = Callable[[dict[str, Any]], Awaitable[None]]
DispatchChildCallback = Callable[
    [BlueprintNode, dict[str, Any], int | None],
    Awaitable[ChildDispatchResult],
]


class BlueprintRunner:
    """
    Execute a parsed blueprint graph deterministically.

    The runner is storage-agnostic. It emits structured events and delegates
    child-session creation to an injected dispatcher so server routes can reuse
    the existing parent-inbox/session contract.
    """

    def __init__(
        self,
        blueprint: BlueprintSpec,
        *,
        emit: EmitCallback | None = None,
        dispatch_child: DispatchChildCallback | None = None,
        blueprint_run_id: str | None = None,
    ) -> None:
        self.blueprint = blueprint
        self._emit = emit
        self._dispatch_child = dispatch_child
        self.blueprint_run_id = blueprint_run_id or f"bpr_{uuid.uuid4().hex}"
        self.node_states: dict[str, dict[str, Any]] = {}

    async def run(self, initial_input: dict[str, Any] | None = None) -> BlueprintRunResult:
        """
        Run the blueprint from root nodes to terminal output.

        :param initial_input: Caller input context, usually the user message.
        :returns: Terminal result.
        """
        context: dict[str, Any] = {
            "input": initial_input or {},
            "nodes": {},
            "outputs": {},
        }
        await self._emit_event("run_started", payload={"input": context["input"]})
        try:
            status = await self._run_node_list(self.blueprint.nodes, context)
            if status == "waiting":
                await self._emit_event("run_waiting", payload={"nodes": self.node_states})
                return BlueprintRunResult(
                    blueprint_run_id=self.blueprint_run_id,
                    status="waiting",
                    output=self._project_final_output(context),
                    node_states=self.node_states,
                )
            failed = [
                state
                for state in self.node_states.values()
                if state.get("status") == "failed"
            ]
            if failed or status == "failed":
                error = failed[0].get("error") if failed else "blueprint failed"
                await self._emit_event("run_failed", status="failed", payload={"error": error})
                return BlueprintRunResult(
                    blueprint_run_id=self.blueprint_run_id,
                    status="failed",
                    output=self._project_final_output(context),
                    node_states=self.node_states,
                    error=str(error),
                )
            output = self._project_final_output(context)
            await self._emit_event("run_completed", status="completed", payload={"output": output})
            return BlueprintRunResult(
                blueprint_run_id=self.blueprint_run_id,
                status="completed",
                output=output,
                node_states=self.node_states,
            )
        except Exception as exc:  # noqa: BLE001 - convert to durable run failure
            await self._emit_event("run_failed", status="failed", payload={"error": str(exc)})
            return BlueprintRunResult(
                blueprint_run_id=self.blueprint_run_id,
                status="failed",
                output=None,
                node_states=self.node_states,
                error=str(exc),
            )

    async def _run_node_list(
        self,
        nodes: list[BlueprintNode],
        context: dict[str, Any],
        *,
        loop_iteration: int | None = None,
    ) -> NodeStatus:
        pending = {node.id for node in nodes}
        node_by_id = {node.id: node for node in nodes}
        terminal_status: NodeStatus = "completed"
        while pending:
            progressed = False
            for node in nodes:
                node_id = node.id
                if node_id not in pending:
                    continue
                dependency_states = [
                    context["nodes"].get(dep, {}).get("status") for dep in node.depends_on
                ]
                if not all(state in {"completed", "skipped"} for state in dependency_states):
                    continue
                progressed = True
                pending.remove(node_id)
                status = await self._run_node(node, context, loop_iteration=loop_iteration)
                if status == "waiting":
                    terminal_status = "waiting"
                elif status == "failed" and terminal_status != "waiting":
                    terminal_status = "failed"
            if not progressed:
                blocked = ", ".join(sorted(pending))
                for node_id in sorted(pending):
                    await self._record_node(
                        node_by_id[node_id],
                        "failed",
                        loop_iteration=loop_iteration,
                        payload={"error": f"unresolved dependencies: {blocked}"},
                    )
                return "failed"
        return terminal_status

    async def _run_node(
        self,
        node: BlueprintNode,
        context: dict[str, Any],
        *,
        loop_iteration: int | None = None,
    ) -> NodeStatus:
        if node.when is not None and not _condition_matches(node.when, context):
            await self._record_node(node, "skipped", loop_iteration=loop_iteration)
            context["nodes"][node.id] = {"status": "skipped", "output": None}
            return "skipped"

        await self._record_node(node, "running", loop_iteration=loop_iteration)
        if node.kind == "loop":
            return await self._run_loop_node(node, context)
        if node.kind in {"agent", "blueprint"}:
            return await self._run_child_node(node, context, loop_iteration=loop_iteration)
        if node.kind in {"approval", "wait_for_event"} and not _auto_continue(node):
            await self._record_node(
                node,
                "waiting",
                loop_iteration=loop_iteration,
                payload={"input": _render_value(node.input, context)},
            )
            context["nodes"][node.id] = {"status": "waiting", "output": None}
            return "waiting"

        output = self._node_output(node, context)
        context["nodes"][node.id] = {
            "status": "completed",
            "kind": node.kind,
            "output": output,
        }
        await self._record_node(
            node,
            "completed",
            loop_iteration=loop_iteration,
            payload={"output": output},
        )
        return "completed"

    async def _run_child_node(
        self,
        node: BlueprintNode,
        context: dict[str, Any],
        *,
        loop_iteration: int | None,
    ) -> NodeStatus:
        if self._dispatch_child is None:
            await self._record_node(
                node,
                "failed",
                loop_iteration=loop_iteration,
                payload={"error": "child dispatcher is not configured"},
            )
            context["nodes"][node.id] = {
                "status": "failed",
                "kind": node.kind,
                "output": None,
            }
            return "failed"
        result = await self._dispatch_child(node, context, loop_iteration)
        payload = {
            "child_session_id": result.child_session_id,
            "output": result.output,
            "error": result.error,
        }
        await self._record_node(
            node,
            result.status,
            loop_iteration=loop_iteration,
            child_session_id=result.child_session_id,
            payload=payload,
        )
        context["nodes"][node.id] = {
            "status": result.status,
            "kind": node.kind,
            "output": result.output,
            "child_session_id": result.child_session_id,
            "error": result.error,
        }
        return result.status

    async def _run_loop_node(
        self,
        node: BlueprintNode,
        context: dict[str, Any],
    ) -> NodeStatus:
        loop = node.loop
        if loop is None:
            await self._record_node(node, "failed", payload={"error": "loop config missing"})
            context["nodes"][node.id] = {"status": "failed", "output": None}
            return "failed"
        iteration_outputs: list[dict[str, Any]] = []
        for iteration in range(1, loop.max_iterations + 1):
            await self._emit_event(
                "loop_iteration",
                node_id=node.id,
                node_kind=node.kind,
                loop_iteration=iteration,
                payload={"max_iterations": loop.max_iterations},
            )
            status = await self._run_node_list(loop.body, context, loop_iteration=iteration)
            iteration_outputs.append(
                {
                    "iteration": iteration,
                    "status": status,
                    "nodes": {
                        body.id: context["nodes"].get(body.id, {})
                        for body in loop.body
                    },
                }
            )
            if status in {"failed", "waiting"}:
                await self._record_node(
                    node,
                    status,
                    payload={"iterations": iteration_outputs},
                )
                context["nodes"][node.id] = {
                    "status": status,
                    "kind": node.kind,
                    "output": iteration_outputs,
                }
                return status
            if _condition_matches(loop.until, context):
                context["nodes"][node.id] = {
                    "status": "completed",
                    "kind": node.kind,
                    "output": iteration_outputs,
                }
                await self._record_node(
                    node,
                    "completed",
                    payload={"iterations": iteration_outputs},
                )
                return "completed"
        exhausted_status: NodeStatus = "failed" if loop.on_exhausted == "fail" else "completed"
        if loop.on_exhausted == "skip":
            exhausted_status = "skipped"
        context["nodes"][node.id] = {
            "status": exhausted_status,
            "kind": node.kind,
            "output": iteration_outputs,
        }
        await self._record_node(
            node,
            exhausted_status,
            payload={
                "iterations": iteration_outputs,
                "exhausted": True,
                "on_exhausted": loop.on_exhausted,
            },
        )
        return exhausted_status

    def _node_output(self, node: BlueprintNode, context: dict[str, Any]) -> Any:
        if node.kind == "output":
            source = node.output if node.output is not None else node.input
            return _render_value(source, context)
        if node.output is not None:
            return _render_value(node.output, context)
        if node.return_mapping is not None:
            return _render_value(node.return_mapping, context)
        if node.input is not None:
            return _render_value(node.input, context)
        if node.kind == "approval":
            return {"approved": True}
        return {"status": "completed", "node_id": node.id}

    def _project_final_output(self, context: dict[str, Any]) -> Any:
        if self.blueprint.outputs:
            return _render_value(self.blueprint.outputs, context)
        for state in reversed(list(context["nodes"].values())):
            if state.get("status") == "completed" and state.get("output") is not None:
                return state["output"]
        return context["nodes"]

    async def _record_node(
        self,
        node: BlueprintNode,
        status: NodeStatus,
        *,
        loop_iteration: int | None = None,
        child_session_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        state = {
            "id": node.id,
            "kind": node.kind,
            "status": status,
            "loop_iteration": loop_iteration,
            "child_session_id": child_session_id,
            **(payload or {}),
        }
        self.node_states[node.id] = state
        await self._emit_event(
            "node_status",
            node_id=node.id,
            node_kind=node.kind,
            status=status,
            loop_iteration=loop_iteration,
            child_session_id=child_session_id,
            payload=payload or {},
        )

    async def _emit_event(
        self,
        event_type: str,
        *,
        node_id: str | None = None,
        node_kind: str | None = None,
        status: str | None = None,
        loop_iteration: int | None = None,
        child_session_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self._emit is None:
            return
        await self._emit(
            {
                "blueprint_run_id": self.blueprint_run_id,
                "event_type": event_type,
                "node_id": node_id,
                "node_kind": node_kind,
                "status": status,
                "loop_iteration": loop_iteration,
                "child_session_id": child_session_id,
                "payload": payload or {},
            }
        )


def _auto_continue(node: BlueprintNode) -> bool:
    if node.metadata.get("auto_continue") is True or node.metadata.get("auto_approve") is True:
        return True
    if isinstance(node.input, dict):
        return node.input.get("auto_continue") is True or node.input.get("approved") is True
    return False


_TEMPLATE_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def _render_value(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, str):
        match = _TEMPLATE_RE.fullmatch(value)
        if match:
            return _resolve_path(match.group(1).strip(), context)
        return _TEMPLATE_RE.sub(
            lambda m: str(_resolve_path(m.group(1).strip(), context) or ""),
            value,
        )
    if isinstance(value, list):
        return [_render_value(item, context) for item in value]
    if isinstance(value, dict):
        return {str(key): _render_value(item, context) for key, item in value.items()}
    return value


def render_blueprint_value(value: Any, context: dict[str, Any]) -> Any:
    """Public wrapper for rendering blueprint YAML values against run context."""
    return _render_value(value, context)


def _condition_matches(condition: Any, context: dict[str, Any]) -> bool:
    if isinstance(condition, bool):
        return condition
    if condition is None:
        return False
    if isinstance(condition, str):
        return bool(_resolve_path(condition, context))
    if isinstance(condition, dict):
        if "value" in condition:
            return bool(condition["value"])
        if "path" in condition:
            actual = _resolve_path(str(condition["path"]), context)
            if "equals" in condition:
                return actual == condition["equals"]
            if "not_equals" in condition:
                return actual != condition["not_equals"]
            return bool(actual)
        if "node_status" in condition:
            node_state = context["nodes"].get(str(condition["node_status"]), {})
            expected = condition.get("equals", "completed")
            return node_state.get("status") == expected
    return False


def _resolve_path(path: str, context: dict[str, Any]) -> Any:
    raw = path.strip()
    if raw.startswith("$."):
        raw = raw[2:]
    elif raw.startswith("$"):
        raw = raw[1:]
    if not raw:
        return context
    current: Any = context
    for part in raw.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            current = getattr(current, part, None)
        if current is None:
            return None
    return current
