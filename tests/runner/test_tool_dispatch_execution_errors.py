"""Error-path tests for runner local tool execution helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.runner.tool_dispatch import (
    _builtin_exec,
    _execute_local_python_tool,
    _execute_spec_builtin_tool,
)


@pytest.mark.asyncio
async def test_execute_spec_builtin_tool_returns_error_without_agent_spec() -> None:
    result = await _execute_spec_builtin_tool(
        "spawn",
        "{}",
        agent_spec=None,
        conversation_id=None,
        task_id=None,
        agent_id=None,
        runner_workspace=None,
    )
    assert "no agent spec" in result


@pytest.mark.asyncio
async def test_execute_spec_builtin_tool_returns_error_on_manager_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec = SimpleNamespace(name="runner-agent", local_tools=[])

    class _BoomManager:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def call_tool(self, *_args, **_kwargs) -> str:
            raise RuntimeError("tool manager down")

    monkeypatch.setattr(_builtin_exec, "ToolManager", _BoomManager)

    result = await _execute_spec_builtin_tool(
        "spawn",
        "{}",
        agent_spec=spec,
        conversation_id="conv_tool",
        task_id=None,
        agent_id="ag_tool",
        runner_workspace=tmp_path,
    )

    assert "RuntimeError" in result
    assert "tool manager down" in result


@pytest.mark.asyncio
async def test_execute_spec_builtin_tool_creates_per_conversation_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec = SimpleNamespace(name="runner-agent", local_tools=[])
    captured: list[object] = []

    class _RecordingManager:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def call_tool(self, tool_name, args, ctx) -> str:
            captured.append((tool_name, args, ctx.workspace))
            return "ok"

    monkeypatch.setattr(_builtin_exec, "ToolManager", _RecordingManager)

    workspace_root = tmp_path / "runner"
    workspace_root.mkdir()
    result = await _execute_spec_builtin_tool(
        "spawn",
        '{"x": 1}',
        agent_spec=spec,
        conversation_id="conv_ws",
        task_id="task_1",
        agent_id="ag_ws",
        runner_workspace=workspace_root,
    )

    assert result == "ok"
    assert captured
    ws = captured[0][2]
    assert isinstance(ws, Path)
    assert ws == workspace_root / "conv_ws"
    assert ws.is_dir()


@pytest.mark.asyncio
async def test_execute_local_python_tool_returns_error_on_manager_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = SimpleNamespace(name="runner-agent", local_tools=[])

    class _BoomManager:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def call_tool(self, *_args, **_kwargs) -> str:
            raise KeyError("missing tool")

    monkeypatch.setattr(_builtin_exec, "ToolManager", _BoomManager)

    result = await _execute_local_python_tool(
        "echo",
        "{}",
        agent_spec=spec,
        conversation_id="conv_py",
        task_id=None,
        agent_id=None,
        runner_workspace=None,
    )

    assert "KeyError" in result
    assert "missing tool" in result


@pytest.mark.asyncio
async def test_execute_local_python_tool_uses_conversation_fallback_task_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = SimpleNamespace(name="demo-agent", local_tools=[])
    captured_ctx: list[object] = []

    class _RecordingManager:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def call_tool(self, _tool_name, _args, ctx) -> str:
            captured_ctx.append(ctx)
            return "done"

    monkeypatch.setattr(_builtin_exec, "ToolManager", _RecordingManager)

    await _execute_local_python_tool(
        "echo",
        "{}",
        agent_spec=spec,
        conversation_id="conv_fallback",
        task_id=None,
        agent_id=None,
        runner_workspace=None,
    )

    assert captured_ctx
    ctx = captured_ctx[0]
    assert ctx.task_id == "conv_fallback"
    assert ctx.agent_id == "demo-agent"
