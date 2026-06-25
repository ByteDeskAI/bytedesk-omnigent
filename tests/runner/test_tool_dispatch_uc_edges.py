"""Edge tests for UC function dispatch in tool_dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from omnigent.runner.tool_dispatch import _execute_uc_function_tool
from omnigent.spec.types import LocalToolInfo, ToolRuntime


@dataclass
class _FakeExecutorSpec:
    auth: Any = None
    profile: str | None = None
    config: dict[str, str] = field(default_factory=dict)


@dataclass
class _FakeAgentSpec:
    local_tools: list[LocalToolInfo] = field(default_factory=list)
    executor: _FakeExecutorSpec | None = None


@pytest.mark.asyncio
async def test_execute_uc_function_tool_errors_when_catalog_path_missing() -> None:
    spec = _FakeAgentSpec(
        local_tools=[
            LocalToolInfo(
                name="classify",
                path=None,
                language="omnigent-python-callable",
                runtime=ToolRuntime.UC_FUNCTION,
                catalog_path=None,
            )
        ]
    )
    result = await _execute_uc_function_tool("classify", {"text": "hi"}, agent_spec=spec)
    assert "not a UC function tool" in result


@pytest.mark.asyncio
async def test_execute_uc_function_tool_errors_when_tool_not_declared() -> None:
    spec = _FakeAgentSpec(local_tools=[])
    result = await _execute_uc_function_tool("missing", {}, agent_spec=spec)
    assert "not a UC function tool" in result


@pytest.mark.asyncio
async def test_execute_uc_function_tool_delegates_to_uc_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _FakeAgentSpec(
        local_tools=[
            LocalToolInfo(
                name="classify",
                path=None,
                language="omnigent-python-callable",
                runtime=ToolRuntime.UC_FUNCTION,
                catalog_path="cat.schema.classify",
                warehouse_id="wh-9",
            )
        ],
        executor=_FakeExecutorSpec(profile="prod"),
    )
    captured: dict[str, object] = {}

    async def _fake_execute(**kwargs: object) -> str:
        captured.update(kwargs)
        return "positive"

    monkeypatch.setattr(
        "omnigent.runner.uc_function.execute_uc_function",
        _fake_execute,
    )

    result = await _execute_uc_function_tool("classify", {"text": "good"}, agent_spec=spec)

    assert result == "positive"
    assert captured["catalog_path"] == "cat.schema.classify"
    assert captured["args"] == {"text": "good"}
    assert captured["profile"] == "prod"
    assert captured["warehouse_id"] == "wh-9"
