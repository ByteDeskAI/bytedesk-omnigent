"""Edge-case registration tests for :mod:`omnigent.tools.manager`.

Covers paths not exercised by the main :mod:`tests.tools.test_manager`
suite: UC-function schema tools, os_env/terminal/timer collision
discipline, unknown builtins, and local-tool registration guards.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec
from omnigent.runtime import _globals
from omnigent.spec.types import (
    AgentSpec,
    BuiltinToolConfig,
    ExecutorSpec,
    LocalToolInfo,
    ToolRuntime,
    ToolsConfig,
)
from omnigent.terminals.registry import TerminalRegistry
from omnigent.tools import ToolManager
from omnigent.tools.base import Tool, ToolContext, is_valid_tool_name
from omnigent.tools.builtins import SysTimerSetTool
from omnigent.tools.builtins.os_env import SysOsReadTool
from omnigent.tools.builtins.sys_terminal import SysTerminalLaunchTool
from omnigent.tools.manager import (
    BuildContext,
    _build_web_search,
    _UCFunctionSchemaTool,
)

_TEST_CTX = ToolContext(task_id="task_test", agent_id="agent_test")


@pytest.fixture(autouse=True)
def _no_host_skill_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.spec.parser.discover_host_skills",
        lambda *_args, **_kwargs: [],
    )


@pytest.fixture()
def terminal_registry(monkeypatch: pytest.MonkeyPatch) -> TerminalRegistry:
    """Install a fresh :class:`TerminalRegistry` singleton for terminal tests."""
    reg = TerminalRegistry()
    monkeypatch.setattr(_globals, "_terminal_registry", reg)
    return reg


# ── _UCFunctionSchemaTool + _build_web_search helpers ─────────


def test_uc_function_schema_tool_identity_and_description() -> None:
    """``_UCFunctionSchemaTool`` exposes name, description, and schema."""
    schema: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": "classify_text",
            "description": "Classify input text.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    tool = _UCFunctionSchemaTool("classify_text", schema)

    assert tool.name() == "classify_text"
    assert tool.description() == "Classify input text."
    assert tool.get_schema() is schema


def test_uc_function_schema_tool_description_empty_when_function_not_dict() -> None:
    """Malformed schema function payloads yield an empty description."""
    tool = _UCFunctionSchemaTool(
        "broken",
        {"type": "function", "function": "not-a-dict"},
    )
    assert tool.description() == ""


def test_build_web_search_infers_openai_provider_from_executor_model() -> None:
    """``_build_web_search`` reads ``executor.model`` (not only ``llm.model``)."""
    ctx = BuildContext(
        spec=AgentSpec(
            spec_version=1,
            executor=ExecutorSpec(model="openai/gpt-4.1"),
        )
    )
    tool = _build_web_search(None, ctx)
    assert tool._is_openai is True


def test_build_web_search_skips_provider_inference_for_databricks() -> None:
    """Databricks models skip ``parse_model_string`` — no OpenAI passthrough."""
    ctx = BuildContext(
        spec=AgentSpec(
            spec_version=1,
            executor=ExecutorSpec(model="databricks-gpt-5-4"),
        )
    )
    tool = _build_web_search(None, ctx)
    assert tool._is_openai is False


# ── Lifecycle helpers ─────────────────────────────────────


def test_start_marks_manager_started() -> None:
    mgr = ToolManager(AgentSpec(spec_version=1))
    assert mgr._started is False
    mgr.start()
    assert mgr._started is True


def test_get_tool_names_lists_registered_tools() -> None:
    mgr = ToolManager(AgentSpec(spec_version=1))
    names = mgr.get_tool_names()
    assert isinstance(names, list)
    assert "load_skill" in names


# ── Builtin registration ────────────────────────────────


def test_unknown_builtin_is_skipped_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec = AgentSpec(
        spec_version=1,
        tools=ToolsConfig(
            builtins=[BuiltinToolConfig(name="definitely_not_a_builtin_xyz")]
        ),
    )
    with caplog.at_level("WARNING"):
        mgr = ToolManager(spec)
    assert mgr.get_tool("definitely_not_a_builtin_xyz") is None
    assert "Unknown built-in tool" in caplog.text


def test_registry_builtin_registers_via_get_builtin_tool() -> None:
    spec = AgentSpec(
        spec_version=1,
        tools=ToolsConfig(builtins=[BuiltinToolConfig(name="list_files")]),
    )
    mgr = ToolManager(spec)
    assert mgr.get_tool("list_files") is not None


# ── Async / timer gating ────────────────────────────────


def test_async_disabled_skips_async_inbox_tools() -> None:
    from omnigent.tools.builtins import SysCallAsyncTool, SysReadInboxTool

    mgr = ToolManager(AgentSpec(spec_version=1, async_enabled=False))
    names = mgr.get_tool_names()
    assert SysCallAsyncTool.name() not in names
    assert SysReadInboxTool.name() not in names


def test_timers_true_registers_set_and_cancel() -> None:
    from omnigent.tools.builtins import SysTimerCancelTool, SysTimerSetTool

    mgr = ToolManager(AgentSpec(spec_version=1, timers=True))
    names = mgr.get_tool_names()
    assert SysTimerSetTool.name() in names
    assert SysTimerCancelTool.name() in names


def test_timer_collision_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class CollidingTimerSet(SysTimerSetTool):
        def name(self) -> str:
            return "load_skill"

    monkeypatch.setattr("omnigent.tools.manager.SysTimerSetTool", CollidingTimerSet)
    with pytest.raises(ValueError, match=r"sys_timer_\* tool .* collides"):
        ToolManager(AgentSpec(spec_version=1, timers=True))


# ── os_env registration ─────────────────────────────────


def test_os_env_spec_registers_sys_os_tools() -> None:
    spec = AgentSpec(
        spec_version=1,
        os_env=OSEnvSpec(type="caller_process"),
    )
    mgr = ToolManager(spec)
    assert mgr.get_tool("sys_os_read") is not None
    assert mgr._os_env is not None


def test_pre_resolved_os_env_is_reused() -> None:
    fake_env = MagicMock()
    spec = AgentSpec(spec_version=1, os_env=OSEnvSpec(type="caller_process"))
    mgr = ToolManager(spec, os_env=fake_env)
    assert mgr._os_env is fake_env
    assert mgr.get_tool("sys_os_read") is not None


def test_create_os_environment_none_skips_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.inner.os_env.create_os_environment",
        lambda _spec: None,
    )
    spec = AgentSpec(
        spec_version=1,
        os_env=OSEnvSpec(type="caller_process"),
    )
    mgr = ToolManager(spec)
    assert mgr.get_tool("sys_os_read") is None
    assert mgr._os_env is None


def test_os_env_collision_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class CollidingOsRead(SysOsReadTool):
        def name(self) -> str:
            return "load_skill"

    monkeypatch.setattr(
        "omnigent.tools.builtins.os_env.SysOsReadTool",
        CollidingOsRead,
    )
    monkeypatch.setattr(
        "omnigent.tools.builtins.os_env.build_os_env_tools",
        lambda _env: [CollidingOsRead(MagicMock())],
    )
    with pytest.raises(ValueError, match=r"sys_os_\* tool .* collides"):
        ToolManager(
            AgentSpec(spec_version=1, os_env=OSEnvSpec(type="caller_process"))
        )


# ── Terminal registration ───────────────────────────────


def test_terminals_block_registers_sys_terminal_tools(
    terminal_registry: TerminalRegistry,
) -> None:
    del terminal_registry
    spec = AgentSpec(
        spec_version=1,
        terminals={"bash": TerminalEnvSpec(command="bash")},
    )
    mgr = ToolManager(spec)
    names = mgr.get_tool_names()
    assert "sys_terminal_launch" in names
    assert "sys_terminal_send" in names
    assert "sys_terminal_read" in names
    assert "sys_terminal_list" in names
    assert "sys_terminal_close" in names


def test_managed_tool_permissions_filter_registered_tools(
    terminal_registry: TerminalRegistry,
) -> None:
    del terminal_registry
    spec = AgentSpec(
        spec_version=1,
        tools=ToolsConfig(builtins=[BuiltinToolConfig(name="web_search")]),
        os_env=OSEnvSpec(type="caller_process"),
        terminals={"bash": TerminalEnvSpec(command="bash")},
        params={
            "managed_tool_permissions": {
                "managed": [
                    "web_search",
                    "sys_os_write",
                    "sys_os_shell",
                    "sys_terminal_launch",
                ],
                "enabled": ["web_search", "sys_os_write"],
            }
        },
    )
    names = set(ToolManager(spec).get_tool_names())

    assert "web_search" in names
    assert "sys_os_write" in names
    assert "sys_os_shell" not in names
    assert "sys_terminal_launch" not in names


def test_terminal_collision_raises(
    terminal_registry: TerminalRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del terminal_registry
    class CollidingLaunch(SysTerminalLaunchTool):
        def name(self) -> str:
            return "load_skill"

    monkeypatch.setattr(
        "omnigent.tools.builtins.sys_terminal.SysTerminalLaunchTool",
        CollidingLaunch,
    )
    with pytest.raises(ValueError, match=r"sys_terminal_\* tool .* collides"):
        ToolManager(
            AgentSpec(
                spec_version=1,
                terminals={"bash": TerminalEnvSpec(command="bash")},
            )
        )


# ── Spec-declared local tools ───────────────────────────


def test_client_local_invalid_name_is_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    invalid_name = "tool with spaces"
    assert not is_valid_tool_name(invalid_name)
    info = LocalToolInfo(
        name=invalid_name,
        path=None,
        language="python",
        runtime=ToolRuntime.CLIENT,
        parameters={"type": "object", "properties": {}},
    )
    with caplog.at_level("WARNING"):
        mgr = ToolManager(AgentSpec(spec_version=1, local_tools=[info]))
    assert mgr.get_tool(invalid_name) is None
    assert "invalid name" in caplog.text


def test_client_local_collision_raises() -> None:
    info = LocalToolInfo(
        name="load_skill",
        path=None,
        language="python",
        runtime=ToolRuntime.CLIENT,
        parameters={"type": "object", "properties": {}},
    )
    with pytest.raises(ValueError, match=r"client local tool .* collides"):
        ToolManager(AgentSpec(spec_version=1, local_tools=[info]))


def test_client_local_missing_parameters_raises() -> None:
    info = LocalToolInfo(
        name="orphan_client",
        path=None,
        language="python",
        runtime=ToolRuntime.CLIENT,
        parameters=None,
    )
    with pytest.raises(ValueError, match="no ``parameters`` block"):
        ToolManager(AgentSpec(spec_version=1, local_tools=[info]))


def test_uc_function_tool_registers_schema_only_tool() -> None:
    info = LocalToolInfo(
        name="classify_text",
        path=None,
        language="omnigent-python-callable",
        runtime=ToolRuntime.UC_FUNCTION,
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        description="Classify arbitrary text.",
    )
    mgr = ToolManager(AgentSpec(spec_version=1, local_tools=[info]))
    tool = mgr.get_tool("classify_text")
    assert isinstance(tool, _UCFunctionSchemaTool)
    assert tool.description() == "Classify arbitrary text."
    schema = tool.get_schema()
    assert schema["function"]["parameters"]["required"] == ["text"]


def test_uc_function_invalid_name_is_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    invalid_name = "uc:bad"
    info = LocalToolInfo(
        name=invalid_name,
        path=None,
        language="omnigent-python-callable",
        runtime=ToolRuntime.UC_FUNCTION,
    )
    with caplog.at_level("WARNING"):
        mgr = ToolManager(AgentSpec(spec_version=1, local_tools=[info]))
    assert mgr.get_tool(invalid_name) is None
    assert "UC function tool" in caplog.text


def test_uc_function_collision_raises() -> None:
    info = LocalToolInfo(
        name="load_skill",
        path=None,
        language="omnigent-python-callable",
        runtime=ToolRuntime.UC_FUNCTION,
    )
    with pytest.raises(ValueError, match=r"UC function tool .* collides"):
        ToolManager(AgentSpec(spec_version=1, local_tools=[info]))


def test_uc_function_default_parameters_when_none() -> None:
    info = LocalToolInfo(
        name="noop_uc",
        path=None,
        language="omnigent-python-callable",
        runtime=ToolRuntime.UC_FUNCTION,
        parameters=None,
    )
    mgr = ToolManager(AgentSpec(spec_version=1, local_tools=[info]))
    tool = mgr.get_tool("noop_uc")
    assert tool is not None
    params = tool.get_schema()["function"]["parameters"]
    assert params == {"type": "object", "properties": {}}


class _NamedTool(Tool):
    def __init__(self, tool_name: str) -> None:
        self._tool_name = tool_name

    def name(self) -> str:  # type: ignore[override]
        return self._tool_name

    @classmethod
    def description(cls) -> str:
        return "Test tool."

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self._tool_name, "parameters": {}},
        }


def test_local_python_invalid_name_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    info = LocalToolInfo(
        name="echo_tool",
        path="tools/python/echo_tool.py",
        language="python",
    )
    monkeypatch.setattr(
        "omnigent.tools.manager.load_local_python_tools",
        lambda *_args, **_kwargs: [_NamedTool("bad name")],
    )
    with caplog.at_level("WARNING"):
        mgr = ToolManager(
            AgentSpec(spec_version=1, local_tools=[info]),
            workdir=tmp_path,
        )
    assert mgr.get_tool("bad name") is None
    assert "Local tool" in caplog.text and "invalid name" in caplog.text


def test_callable_local_invalid_name_skipped(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    info = LocalToolInfo(
        name="callable_tool",
        path="pkg.mod.fn",
        language="omnigent-python-callable",
    )
    monkeypatch.setattr(
        "omnigent.tools.local_callable.load_local_callable_tools",
        lambda _tools: [_NamedTool("bad:colon")],
    )
    with caplog.at_level("WARNING"):
        mgr = ToolManager(AgentSpec(spec_version=1, local_tools=[info]))
    assert mgr.get_tool("bad:colon") is None
    assert "Omnigent callable tool" in caplog.text


def test_callable_local_collision_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    info = LocalToolInfo(
        name="callable_tool",
        path="pkg.mod.fn",
        language="omnigent-python-callable",
    )
    monkeypatch.setattr(
        "omnigent.tools.local_callable.load_local_callable_tools",
        lambda _tools: [_NamedTool("load_skill")],
    )
    with pytest.raises(ValueError, match=r"omnigent callable tool .* collides"):
        ToolManager(AgentSpec(spec_version=1, local_tools=[info]))


def test_callable_local_tool_is_registered_and_callable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callable tools register even when ``workdir`` is ``None``."""
    package_dir = tmp_path / "mgr_callable_targets"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("")
    (package_dir / "tool_fns.py").write_text(
        "def add(a: int, b: int) -> int:\n"
        '    """Add two numbers."""\n'
        "    return a + b\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    info = LocalToolInfo(
        name="add",
        path="mgr_callable_targets.tool_fns.add",
        language="omnigent-python-callable",
        parameters={
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"},
            },
            "required": ["a", "b"],
        },
    )
    mgr = ToolManager(AgentSpec(spec_version=1, local_tools=[info]))
    result = mgr.call_tool("add", json.dumps({"a": 2, "b": 3}), _TEST_CTX)
    assert result == "5"
