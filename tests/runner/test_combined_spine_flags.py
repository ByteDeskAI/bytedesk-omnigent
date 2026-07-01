"""Compatibility tests for retired tool-dispatch spine flags.

The ToolExecutionContext carrier and ToolDispatcher registry are canonical
runtime paths now. Their former rollout flags must not change dispatch
selection when present in an operator environment.
"""

from __future__ import annotations

import pytest

from omnigent.runner import tool_dispatch
from omnigent.runner import tool_dispatcher_registry as reg
from omnigent.runner.tool_execution_context import ToolExecutionContext


@pytest.mark.asyncio
async def test_retired_tool_dispatch_flags_do_not_bypass_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_USE_TOOL_DISPATCHER_REGISTRY", "0")
    monkeypatch.setenv("OMNIGENT_USE_TOOL_EXECUTION_CONTEXT", "0")

    captured: dict[str, ToolExecutionContext] = {}

    async def _fake_registry(ctx: ToolExecutionContext) -> str:
        captured["ctx"] = ctx
        return "registry"

    monkeypatch.setattr(reg, "dispatch_via_registry", _fake_registry)

    out = await tool_dispatch.execute_tool(tool_name="sys_read_inbox", arguments="{}")

    assert out == "registry"
    assert captured["ctx"].tool_name == "sys_read_inbox"
