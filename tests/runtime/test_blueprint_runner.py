"""Unit tests for the deterministic blueprint runner."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.blueprints import BlueprintRunner, ChildDispatchResult
from omnigent.spec.types import BlueprintLoopSpec, BlueprintNode, BlueprintSpec

pytestmark = pytest.mark.asyncio


async def test_runner_executes_dependencies_in_yaml_order() -> None:
    events: list[dict[str, Any]] = []
    spec = BlueprintSpec(
        nodes=[
            BlueprintNode(
                id="collect",
                kind="task",
                output={"text": "A"},
            ),
            BlueprintNode(
                id="draft",
                kind="task",
                depends_on=["collect"],
                output={"text": "{{ $.nodes.collect.output.text }}B"},
            ),
            BlueprintNode(
                id="final",
                kind="output",
                depends_on=["draft"],
                output={"text": "{{ $.nodes.draft.output.text }}C"},
            ),
        ],
        outputs={"text": "{{ $.nodes.final.output.text }}"},
    )

    runner = BlueprintRunner(spec, emit=lambda event: _append_event(events, event))
    result = await runner.run({"text": "ignored"})

    assert result.status == "completed"
    assert result.output == {"text": "ABC"}
    completed = [
        event["node_id"]
        for event in events
        if event["event_type"] == "node_status" and event["status"] == "completed"
    ]
    assert completed == ["collect", "draft", "final"]


async def test_runner_completes_bounded_loop_when_until_matches() -> None:
    spec = BlueprintSpec(
        nodes=[
            BlueprintNode(
                id="review",
                kind="loop",
                loop=BlueprintLoopSpec(
                    max_iterations=3,
                    until={"path": "$.nodes.approve.output.approved", "equals": True},
                    body=[
                        BlueprintNode(
                            id="approve",
                            kind="approval",
                            input={"approved": True},
                        )
                    ],
                ),
            )
        ]
    )

    result = await BlueprintRunner(spec).run({})

    assert result.status == "completed"
    assert result.node_states["review"]["status"] == "completed"


async def test_runner_fails_when_loop_exhausts_with_fail_policy() -> None:
    spec = BlueprintSpec(
        nodes=[
            BlueprintNode(
                id="review",
                kind="loop",
                loop=BlueprintLoopSpec(
                    max_iterations=2,
                    until={"value": False},
                    on_exhausted="fail",
                    body=[BlueprintNode(id="draft", kind="task")],
                ),
            )
        ]
    )

    result = await BlueprintRunner(spec).run({})

    assert result.status == "failed"
    assert result.node_states["review"]["status"] == "failed"


async def test_runner_records_child_session_result() -> None:
    async def dispatch(
        node: BlueprintNode,
        _context: dict[str, Any],
        loop_iteration: int | None,
    ) -> ChildDispatchResult:
        assert node.id == "child"
        assert loop_iteration is None
        return ChildDispatchResult(
            status="completed",
            child_session_id="conv_child",
            output="child output",
        )

    spec = BlueprintSpec(
        nodes=[
            BlueprintNode(id="child", kind="blueprint", target="nested"),
            BlueprintNode(
                id="final",
                kind="output",
                depends_on=["child"],
                output={"text": "{{ $.nodes.child.output }}"},
            ),
        ],
        outputs={"text": "{{ $.nodes.final.output.text }}"},
    )

    result = await BlueprintRunner(spec, dispatch_child=dispatch).run({})

    assert result.status == "completed"
    assert result.output == {"text": "child output"}
    assert result.node_states["child"]["child_session_id"] == "conv_child"


async def _append_event(events: list[dict[str, Any]], event: dict[str, Any]) -> None:
    events.append(event)
