"""Tests for omnigent.runner.tool_dispatcher_registry (BDP-2327, Phase 5)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from omnigent.runner import tool_dispatch
from omnigent.runner import tool_dispatcher_registry as reg
from omnigent.runner.communication_dispatchers import (
    InboxDispatcher,
    SessionCreateDispatcher,
    SessionQueryDispatcher,
    SessionSendDispatcher,
)
from omnigent.runner.service_dispatchers import (
    AgentDispatcher,
    FileDispatcher,
    PolicyDispatcher,
    RestDispatcher,
    SkillAcquisitionDispatcher,
)
from omnigent.runner.tool_dispatcher_registry import (
    DispatcherRegistry,
    ToolDispatcher,
    _FunctionalDispatcher,
    build_default_registry,
    register_default_dispatchers,
)
from omnigent.runner.tool_execution_context import ToolExecutionContext
from omnigent.spec.types import AgentSpec, BuiltinToolConfig, ToolsConfig


def test_functional_dispatcher_satisfies_protocol() -> None:
    """The concrete adapter is a structural ToolDispatcher."""
    d = _FunctionalDispatcher(
        name="x",
        match=lambda _ctx, _args: True,
        run=_noop_run,
    )
    assert isinstance(d, ToolDispatcher)


async def _noop_run(ctx: ToolExecutionContext, args: dict[str, Any]) -> str:
    return "ok"


def test_default_registry_registers_all_branches() -> None:
    """The default registry mirrors the MCP guard plus 21 routing branches."""
    registry = build_default_registry()
    names = [d.name for d in registry.dispatchers]
    assert names == [
        "mcp",
        "os_env",
        "rest",
        "file",
        "terminal",
        "async_inbox",
        "subagent",
        "list_models",
        "session_create",
        "session_query",
        "web_fetch",
        "timer",
        "task_lifecycle",
        "skill",
        "comment",
        "agent",
        "policy",
        "skill_acq",
        "spec_builtin",
        "local_python",
        "uc_function",
        "spec_callable",
    ]


def test_default_registry_uses_class_backed_communication_dispatchers() -> None:
    """Communication and server-backed branches are concrete dispatchers."""
    registry = build_default_registry()
    by_name = {dispatcher.name: dispatcher for dispatcher in registry.dispatchers}

    assert isinstance(by_name["rest"], RestDispatcher)
    assert isinstance(by_name["file"], FileDispatcher)
    assert isinstance(by_name["async_inbox"], InboxDispatcher)
    assert isinstance(by_name["subagent"], SessionSendDispatcher)
    assert isinstance(by_name["session_create"], SessionCreateDispatcher)
    assert isinstance(by_name["session_query"], SessionQueryDispatcher)
    assert isinstance(by_name["agent"], AgentDispatcher)
    assert isinstance(by_name["policy"], PolicyDispatcher)
    assert isinstance(by_name["skill_acq"], SkillAcquisitionDispatcher)


def test_mcp_dispatcher_is_first_and_unconditional() -> None:
    """MCP precedes everything and matches purely on mcp_manager presence."""
    registry = build_default_registry()
    mcp = registry.dispatchers[0]
    assert mcp.name == "mcp"

    # Matches an OS-env tool name when an mcp_manager is present — MCP wins
    # over the os_env branch, exactly like the elif chain's leading
    # ``if mcp_manager is not None``.
    ctx_with_mcp = ToolExecutionContext(
        tool_name="sys_os_shell", arguments="{}", mcp_manager=object()
    )
    assert mcp.matches(ctx_with_mcp, {}) is True

    # No mcp_manager → MCP does not match, so dispatch falls through.
    ctx_no_mcp = ToolExecutionContext(tool_name="sys_os_shell", arguments="{}")
    assert mcp.matches(ctx_no_mcp, {}) is False


@pytest.mark.asyncio
async def test_registry_dispatches_file_tools_through_file_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FileDispatcher forwards the same parsed args and context values."""
    received: dict[str, Any] = {}

    async def _fake_file(
        tool_name: str,
        args: dict[str, Any],
        server_client: Any,
        **kwargs: Any,
    ) -> str:
        received.update(tool_name=tool_name, args=args, server_client=server_client, **kwargs)
        return "file-output"

    monkeypatch.setattr(tool_dispatch, "_execute_file_tool", _fake_file)
    server_client = object()
    spec = object()
    workspace = Path("/tmp/work")

    out = await build_default_registry().dispatch(
        ToolExecutionContext(
            tool_name="upload_file",
            arguments='{"path": "a.txt"}',
            server_client=server_client,
            conversation_id="conv_1",
            agent_spec=spec,
            runner_workspace=workspace,
        )
    )

    assert out == "file-output"
    assert received == {
        "tool_name": "upload_file",
        "args": {"path": "a.txt"},
        "server_client": server_client,
        "conversation_id": "conv_1",
        "agent_spec": spec,
        "runner_workspace": workspace,
    }


@pytest.mark.asyncio
async def test_registry_dispatches_agent_tools_through_agent_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgentDispatcher forwards the same parsed args and context values."""
    received: dict[str, Any] = {}

    async def _fake_agent(tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        received.update(tool_name=tool_name, args=args, **kwargs)
        return "agent-output"

    monkeypatch.setattr(tool_dispatch, "_execute_agent_tool", _fake_agent)
    server_client = object()
    spec = object()
    workspace = Path("/tmp/work")

    out = await build_default_registry().dispatch(
        ToolExecutionContext(
            tool_name="sys_agent_get",
            arguments='{"agent_id": "ag_1"}',
            server_client=server_client,
            conversation_id="conv_1",
            agent_spec=spec,
            runner_workspace=workspace,
        )
    )

    assert out == "agent-output"
    assert received == {
        "tool_name": "sys_agent_get",
        "args": {"agent_id": "ag_1"},
        "server_client": server_client,
        "agent_spec": spec,
        "conversation_id": "conv_1",
        "runner_workspace": workspace,
    }


@pytest.mark.asyncio
async def test_registry_dispatches_policy_tools_through_policy_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PolicyDispatcher preserves the raw argument string contract."""
    received: dict[str, Any] = {}

    async def _fake_policy(tool_name: str, args: str, **kwargs: Any) -> str:
        received.update(tool_name=tool_name, args=args, **kwargs)
        return "policy-output"

    monkeypatch.setattr(tool_dispatch, "_execute_policy_tool", _fake_policy)
    server_client = object()
    raw_args = '{"policy": "p"}'

    out = await build_default_registry().dispatch(
        ToolExecutionContext(
            tool_name="sys_add_policy",
            arguments=raw_args,
            server_client=server_client,
            conversation_id="conv_1",
        )
    )

    assert out == "policy-output"
    assert received == {
        "tool_name": "sys_add_policy",
        "args": raw_args,
        "conversation_id": "conv_1",
        "server_client": server_client,
    }


def test_catch_all_is_last_and_always_matches() -> None:
    """The trailing dispatcher always matches, so the registry is total."""
    registry = build_default_registry()
    catch_all = registry.dispatchers[-1]
    assert catch_all.name == "spec_callable"
    ctx = ToolExecutionContext(tool_name="anything_at_all", arguments="{}")
    assert catch_all.matches(ctx, {}) is True


@pytest.mark.asyncio
async def test_dispatch_routes_to_first_match() -> None:
    """The registry routes to the first matching dispatcher (first-match-wins)."""
    registry = DispatcherRegistry()
    calls: list[str] = []

    def _make(name: str, matches: bool) -> _FunctionalDispatcher:
        async def _run(ctx: ToolExecutionContext, args: dict[str, Any]) -> str:
            calls.append(name)
            return name

        return _FunctionalDispatcher(name=name, match=lambda _c, _a: matches, run=_run)

    registry.register(_make("first", matches=False))
    registry.register(_make("second", matches=True))
    registry.register(_make("third", matches=True))

    out = await registry.dispatch(ToolExecutionContext(tool_name="t", arguments="{}"))
    assert out == "second"
    assert calls == ["second"]  # later matches never run


@pytest.mark.asyncio
async def test_dispatch_parses_arguments_once() -> None:
    """``ctx.arguments`` is json.loads'd once and threaded to the match/run."""
    registry = DispatcherRegistry()
    seen: dict[str, Any] = {}

    async def _run(ctx: ToolExecutionContext, args: dict[str, Any]) -> str:
        seen["args"] = args
        return "done"

    registry.register(_FunctionalDispatcher(name="t", match=lambda _c, _a: True, run=_run))
    await registry.dispatch(ToolExecutionContext(tool_name="t", arguments='{"k": "v", "n": 1}'))
    assert seen["args"] == {"k": "v", "n": 1}


@pytest.mark.asyncio
async def test_dispatch_tolerates_invalid_json_arguments() -> None:
    """Malformed arguments fall back to {} (mirrors the elif chain)."""
    registry = DispatcherRegistry()
    seen: dict[str, Any] = {}

    async def _run(ctx: ToolExecutionContext, args: dict[str, Any]) -> str:
        seen["args"] = args
        return "done"

    registry.register(_FunctionalDispatcher(name="t", match=lambda _c, _a: True, run=_run))
    await registry.dispatch(ToolExecutionContext(tool_name="t", arguments="not json"))
    assert seen["args"] == {}


@pytest.mark.asyncio
async def test_dispatch_renders_exceptions_to_error_string() -> None:
    """A raising dispatcher yields the SAME 'Error: {type}: {msg}' shape."""
    registry = DispatcherRegistry()

    async def _boom(ctx: ToolExecutionContext, args: dict[str, Any]) -> str:
        raise ValueError("kaboom")

    registry.register(_FunctionalDispatcher(name="boom", match=lambda _c, _a: True, run=_boom))
    out = await registry.dispatch(ToolExecutionContext(tool_name="t", arguments="{}"))
    assert out == "Error: ValueError: kaboom"


@pytest.mark.asyncio
async def test_mcp_dispatch_calls_manager_with_parsed_args() -> None:
    """The MCP dispatcher routes through mcp_manager.call_tool(spec, name, args)."""
    registry = build_default_registry()
    received: dict[str, Any] = {}

    class _FakeMcp:
        async def call_tool(self, spec: Any, name: str, args: dict[str, Any]) -> str:
            received.update(spec=spec, name=name, args=args)
            return "mcp-output"

    spec = object()
    out = await registry.dispatch(
        ToolExecutionContext(
            tool_name="any_mcp_tool",
            arguments='{"q": 1}',
            agent_spec=spec,
            mcp_manager=_FakeMcp(),
        )
    )
    assert out == "mcp-output"
    assert received == {"spec": spec, "name": "any_mcp_tool", "args": {"q": 1}}


def test_native_relay_builtin_tools_unchanged() -> None:
    """Phase 5 does not touch _NATIVE_RELAY_BUILTIN_TOOLS membership."""
    # The registry imports the category sets from tool_dispatch (never
    # re-declares them), so the native relay surface is the SAME set the
    # elif chain uses. Assert the union the relay advertises is exactly the
    # sum of its constituent category sets — a drift in either side fails.
    expected = (
        tool_dispatch._COMMENT_TOOLS
        | tool_dispatch._SESSION_QUERY_TOOLS
        | tool_dispatch._ASYNC_INBOX_TOOLS
        | tool_dispatch._SUBAGENT_TOOLS
        | tool_dispatch._LIST_MODELS_TOOLS
        | tool_dispatch._SESSION_CREATE_TOOLS
        | tool_dispatch._TASK_LIFECYCLE_TOOLS
        | tool_dispatch._AGENT_TOOLS
        | tool_dispatch._POLICY_TOOLS
        | tool_dispatch._TERMINAL_TOOLS
    )
    assert expected == tool_dispatch._NATIVE_RELAY_BUILTIN_TOOLS


@pytest.mark.asyncio
async def test_registry_dispatches_spec_declared_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry path mirrors the elif chain's spec-builtin branch."""
    received: dict[str, Any] = {}

    async def _fake_spec_builtin(tool_name: str, args: str, **kwargs: Any) -> str:
        received.update(tool_name=tool_name, args=args, agent_spec=kwargs.get("agent_spec"))
        return "builtin-output"

    monkeypatch.setattr(tool_dispatch, "_execute_spec_builtin_tool", _fake_spec_builtin)
    spec = AgentSpec(
        spec_version=1,
        tools=ToolsConfig(builtins=[BuiltinToolConfig(name="export_agent")]),
    )

    out = await build_default_registry().dispatch(
        ToolExecutionContext(
            tool_name="export_agent",
            arguments='{"source": "a"}',
            agent_spec=spec,
        )
    )

    assert out == "builtin-output"
    assert received == {
        "tool_name": "export_agent",
        "args": '{"source": "a"}',
        "agent_spec": spec,
    }


@pytest.mark.asyncio
async def test_registry_dispatches_skill_acq_before_spec_builtin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sys_skill_* tools are runner-dispatched even when declared as builtins."""
    received: dict[str, Any] = {}

    async def _fake_skill_acq(tool_name: str, args: dict[str, Any], server_client: Any) -> str:
        received.update(tool_name=tool_name, args=args, server_client=server_client)
        return "skill-acq-output"

    async def _unexpected_spec_builtin(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("sys_skill_* must not fall through to spec_builtin")

    monkeypatch.setattr(tool_dispatch, "_execute_skill_acq_tool", _fake_skill_acq)
    monkeypatch.setattr(tool_dispatch, "_execute_spec_builtin_tool", _unexpected_spec_builtin)
    spec = AgentSpec(
        spec_version=1,
        tools=ToolsConfig(builtins=[BuiltinToolConfig(name="sys_skill_installed")]),
    )
    server_client = object()

    out = await build_default_registry().dispatch(
        ToolExecutionContext(
            tool_name="sys_skill_installed",
            arguments='{"agent_id": "ag1"}',
            agent_spec=spec,
            server_client=server_client,
        )
    )

    assert out == "skill-acq-output"
    assert received == {
        "tool_name": "sys_skill_installed",
        "args": {"agent_id": "ag1"},
        "server_client": server_client,
    }


@pytest.mark.asyncio
async def test_execute_tool_routes_through_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``execute_tool`` dispatches through the registry seam, carrying the SAME
    inbox object by reference end-to-end.
    """
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    captured: dict[str, ToolExecutionContext] = {}

    async def _fake_via_registry(ctx: ToolExecutionContext) -> str:
        captured["ctx"] = ctx
        return "stubbed"

    monkeypatch.setattr(reg, "dispatch_via_registry", _fake_via_registry)

    out = await tool_dispatch.execute_tool(
        tool_name="sys_read_inbox",
        arguments="{}",
        session_inbox=inbox,
    )
    assert out == "stubbed"
    assert captured["ctx"].session_inbox is inbox


@pytest.mark.asyncio
async def test_execute_tool_legacy_tool_dispatch_envs_do_not_gate_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retired tool-dispatch env switches no longer control the registry path."""
    monkeypatch.setenv("OMNIGENT_USE_TOOL_DISPATCHER_REGISTRY", "0")
    monkeypatch.setenv("OMNIGENT_USE_TOOL_EXECUTION_CONTEXT", "0")

    async def _fake_via_registry(ctx: ToolExecutionContext) -> str:
        return f"registry:{ctx.tool_name}"

    monkeypatch.setattr(reg, "dispatch_via_registry", _fake_via_registry)

    out = await tool_dispatch.execute_tool(
        tool_name="sys_read_inbox",
        arguments="{}",
    )
    assert out == "registry:sys_read_inbox"


def test_register_default_dispatchers_is_idempotent_into_fresh_registry() -> None:
    """register_default_dispatchers populates a caller-owned registry."""
    registry = DispatcherRegistry()
    assert registry.dispatchers == ()
    register_default_dispatchers(registry)
    assert len(registry.dispatchers) == 22
    assert registry.dispatchers[0].name == "mcp"
