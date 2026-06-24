"""Unit tests for AP-side ``sys_os_*`` builtins (``omnigent.tools.builtins.os_env``)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.inner.os_env import OSEnvironment, _DEFAULT_READ_LIMIT
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.os_env import (
    SysOsEditTool,
    SysOsReadTool,
    SysOsShellTool,
    SysOsWriteTool,
    _OSEnvBackedTool,
    build_os_env_tools,
)

_CTX = ToolContext(task_id="task_t", agent_id="ag_t", conversation_id="conv_t")


def _fake_os_env() -> MagicMock:
    """Minimal OSEnvironment stand-in with async method stubs."""
    env = MagicMock(spec=OSEnvironment)
    env.read = AsyncMock(return_value={"path": "/tmp/x", "content": "hello"})
    env.write = AsyncMock(return_value={"path": "/tmp/x", "bytes": 5})
    env.edit = AsyncMock(return_value={"path": "/tmp/x", "replacements": 1})
    env.shell = AsyncMock(
        return_value={"stdout": "ok", "stderr": "", "exit_code": 0},
    )
    return env


def test_build_os_env_tools_returns_four_distinct_tools() -> None:
    """Registration helper builds one tool per sys_os_* operation."""
    env = _fake_os_env()
    tools = build_os_env_tools(env)
    names = {t.name() for t in tools}
    assert names == {"sys_os_read", "sys_os_write", "sys_os_edit", "sys_os_shell"}
    assert all(getattr(t, "_os_env", None) is env for t in tools)


@pytest.mark.parametrize(
    ("tool_cls", "description_fragment"),
    [
        (SysOsReadTool, "read"),
        (SysOsWriteTool, "write"),
        (SysOsEditTool, "replacement"),
        (SysOsShellTool, "shell"),
    ],
)
def test_os_env_tool_identity_and_schema(
    tool_cls: type[_OSEnvBackedTool],
    description_fragment: str,
) -> None:
    """Each sys_os_* tool exposes stable name, description, and schema."""
    tool = tool_cls(_fake_os_env())
    assert tool.name() == tool_cls.name()
    assert description_fragment in tool.description().lower()
    schema = tool.get_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == tool.name()


def test_base_invoke_async_raises_not_implemented() -> None:
    """Subclasses must override the async dispatch hook."""
    tool = SysOsReadTool(_fake_os_env())
    with pytest.raises(NotImplementedError):
        asyncio.run(_OSEnvBackedTool._invoke_async(tool, {}))


def test_invoke_rejects_malformed_json() -> None:
    """Malformed JSON returns a structured error instead of crashing."""
    tool = SysOsReadTool(_fake_os_env())
    result = json.loads(tool.invoke("{bad json", _CTX))
    assert "error" in result
    assert "malformed arguments JSON" in result["error"]


def test_invoke_rejects_non_object_arguments() -> None:
    """Arguments must decode to a JSON object."""
    tool = SysOsReadTool(_fake_os_env())
    result = json.loads(tool.invoke('["not", "an", "object"]', _CTX))
    assert result == {"error": "arguments must be a JSON object"}


def test_invoke_surfaces_backend_exceptions() -> None:
    """Backend failures are logged and returned as structured errors."""
    env = _fake_os_env()
    env.read = AsyncMock(side_effect=RuntimeError("disk offline"))
    tool = SysOsReadTool(env)
    result = json.loads(tool.invoke('{"path": "/tmp/x"}', _CTX))
    assert result == {"error": "disk offline"}


def test_sys_os_read_forwards_path_offset_and_default_limit() -> None:
    """Read uses the agent-tool default limit when limit is omitted."""
    env = _fake_os_env()
    tool = SysOsReadTool(env)
    result = json.loads(tool.invoke('{"path": "/tmp/a.txt", "offset": 3}', _CTX))
    assert result["content"] == "hello"
    env.read.assert_awaited_once_with(
        path="/tmp/a.txt",
        offset=3,
        limit=_DEFAULT_READ_LIMIT,
    )


def test_sys_os_read_forwards_explicit_zero_limit() -> None:
    """Explicit limit=0 is forwarded for os_env validation (not replaced)."""
    env = _fake_os_env()
    tool = SysOsReadTool(env)
    tool.invoke('{"path": "/tmp/a.txt", "limit": 0}', _CTX)
    env.read.assert_awaited_once_with(path="/tmp/a.txt", offset=1, limit=0)


def test_sys_os_write_forwards_path_and_content() -> None:
    """Write delegates to OSEnvironment.write with parsed args."""
    env = _fake_os_env()
    tool = SysOsWriteTool(env)
    result = json.loads(
        tool.invoke('{"path": "/tmp/out.txt", "content": "payload"}', _CTX),
    )
    assert result["bytes"] == 5
    env.write.assert_awaited_once_with(path="/tmp/out.txt", content="payload")


def test_sys_os_edit_forwards_single_and_batch_edits() -> None:
    """Edit delegates oldText/newText and optional edits array."""
    env = _fake_os_env()
    tool = SysOsEditTool(env)
    payload = {
        "path": "/tmp/a.txt",
        "oldText": "foo",
        "newText": "bar",
        "edits": [{"oldText": "a", "newText": "b"}],
    }
    tool.invoke(json.dumps(payload), _CTX)
    env.edit.assert_awaited_once_with(
        path="/tmp/a.txt",
        old_text="foo",
        new_text="bar",
        edits=payload["edits"],
    )


def test_sys_os_shell_forwards_command_and_timeout() -> None:
    """Shell delegates command and optional timeout."""
    env = _fake_os_env()
    tool = SysOsShellTool(env)
    result = json.loads(
        tool.invoke('{"command": "echo hi", "timeout": 12}', _CTX),
    )
    assert result["exit_code"] == 0
    env.shell.assert_awaited_once_with(command="echo hi", timeout=12)