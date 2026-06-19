"""Tests for omnigent.runner.tool_dispatcher_registry (BDP-2327, Phase 5)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.runner import tool_dispatch
from omnigent.runner import tool_dispatcher_registry as reg
from omnigent.runner.tool_dispatcher_registry import (
    DispatcherRegistry,
    ToolDispatcher,
    _FunctionalDispatcher,
    build_default_registry,
    register_default_dispatchers,
    use_tool_dispatcher_registry,
)
from omnigent.runner.tool_execution_context import ToolExecutionContext


def test_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the env var unset, the Phase 5 flag reads false (default OFF)."""
    monkeypatch.delenv("OMNIGENT_USE_TOOL_DISPATCHER_REGISTRY", raising=False)
    assert use_tool_dispatcher_registry() is False


def test_flag_reads_truthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag uses the shared truthy convention (1/true/yes)."""
    for value in ("1", "true", "YES"):
        monkeypatch.setenv("OMNIGENT_USE_TOOL_DISPATCHER_REGISTRY", value)
        assert use_tool_dispatcher_registry() is True
    monkeypatch.setenv("OMNIGENT_USE_TOOL_DISPATCHER_REGISTRY", "0")
    assert use_tool_dispatcher_registry() is False


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
    """The default registry mirrors the 20 elif branches, in order."""
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
        "local_python",
        "uc_function",
        "spec_callable",
    ]


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

    registry.register(
        _FunctionalDispatcher(name="t", match=lambda _c, _a: True, run=_run)
    )
    await registry.dispatch(
        ToolExecutionContext(tool_name="t", arguments='{"k": "v", "n": 1}')
    )
    assert seen["args"] == {"k": "v", "n": 1}


@pytest.mark.asyncio
async def test_dispatch_tolerates_invalid_json_arguments() -> None:
    """Malformed arguments fall back to {} (mirrors the elif chain)."""
    registry = DispatcherRegistry()
    seen: dict[str, Any] = {}

    async def _run(ctx: ToolExecutionContext, args: dict[str, Any]) -> str:
        seen["args"] = args
        return "done"

    registry.register(
        _FunctionalDispatcher(name="t", match=lambda _c, _a: True, run=_run)
    )
    await registry.dispatch(ToolExecutionContext(tool_name="t", arguments="not json"))
    assert seen["args"] == {}


@pytest.mark.asyncio
async def test_dispatch_renders_exceptions_to_error_string() -> None:
    """A raising dispatcher yields the SAME 'Error: {type}: {msg}' shape."""
    registry = DispatcherRegistry()

    async def _boom(ctx: ToolExecutionContext, args: dict[str, Any]) -> str:
        raise ValueError("kaboom")

    registry.register(
        _FunctionalDispatcher(name="boom", match=lambda _c, _a: True, run=_boom)
    )
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
    assert tool_dispatch._NATIVE_RELAY_BUILTIN_TOOLS == expected


@pytest.mark.asyncio
async def test_execute_tool_flag_off_skips_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the flag OFF, execute_tool never calls the registry seam."""
    monkeypatch.delenv("OMNIGENT_USE_TOOL_DISPATCHER_REGISTRY", raising=False)
    monkeypatch.delenv("OMNIGENT_USE_TOOL_EXECUTION_CONTEXT", raising=False)
    called = {"n": 0}

    async def _fake_via_registry(ctx: ToolExecutionContext) -> str:
        called["n"] += 1
        return "should-not-run"

    monkeypatch.setattr(reg, "dispatch_via_registry", _fake_via_registry)

    out = await tool_dispatch.execute_tool(
        tool_name="definitely_not_a_real_tool",
        arguments="{}",
    )
    assert called["n"] == 0
    assert "should-not-run" not in out


@pytest.mark.asyncio
async def test_execute_tool_flag_on_routes_through_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With the flag ON, ``execute_tool`` dispatches through the registry seam,
    carrying the SAME inbox object by reference end-to-end.
    """
    monkeypatch.setenv("OMNIGENT_USE_TOOL_DISPATCHER_REGISTRY", "1")
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


def test_register_default_dispatchers_is_idempotent_into_fresh_registry() -> None:
    """register_default_dispatchers populates a caller-owned registry."""
    registry = DispatcherRegistry()
    assert registry.dispatchers == ()
    register_default_dispatchers(registry)
    assert len(registry.dispatchers) == 20
    assert registry.dispatchers[0].name == "mcp"
