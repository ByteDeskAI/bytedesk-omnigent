"""Tests for omnigent.runner.tool_execution_context (BDP-2327, Phase 4)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.runner import tool_dispatch
from omnigent.runner.tool_execution_context import ToolExecutionContext


def test_context_holds_session_inbox_by_reference() -> None:
    """The frozen context stores the SAME queue object, not a copy."""
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    ctx = ToolExecutionContext(
        tool_name="sys_read_inbox",
        arguments="{}",
        session_inbox=inbox,
    )
    assert ctx.session_inbox is inbox


def test_context_holds_async_tasks_map_by_reference() -> None:
    """The handle->(Task, Event) map is held by reference too."""
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}
    ctx = ToolExecutionContext(
        tool_name="sys_cancel_async",
        arguments="{}",
        session_async_tasks=tasks,
    )
    assert ctx.session_async_tasks is tasks


def test_context_bindings_are_frozen() -> None:
    """frozen=True freezes the bindings — you cannot rebind a field."""
    ctx = ToolExecutionContext(tool_name="x", arguments="{}")
    with pytest.raises(Exception):  # FrozenInstanceError
        ctx.session_inbox = asyncio.Queue()  # type: ignore[misc]


def test_optional_fields_default_to_none() -> None:
    """Only tool_name/arguments are required; the rest default to None."""
    ctx = ToolExecutionContext(tool_name="sys_os_read", arguments="{}")
    assert ctx.server_client is None
    assert ctx.session_inbox is None
    assert ctx.session_async_tasks is None
    assert ctx.publish_event is None


@pytest.mark.asyncio
async def test_background_mutation_of_inbox_visible_to_caller() -> None:
    """
    A background task mutating ``context.session_inbox`` is observed by the
    caller that still holds the same queue (reference semantics).

    This is the invariant async tool dispatch depends on: ``sys_call_async``
    pushes onto the inbox from a background task while ``sys_read_inbox``
    drains the caller's handle to the same queue.
    """
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    ctx = ToolExecutionContext(
        tool_name="sys_read_inbox",
        arguments="{}",
        session_inbox=inbox,
    )

    async def _background_push() -> None:
        await ctx.session_inbox.put({"handle_id": "h1", "output": "done"})

    await asyncio.create_task(_background_push())

    # The caller's own handle (``inbox``) sees the background mutation
    # because the context never copied the queue.
    assert inbox.qsize() == 1
    assert (await inbox.get())["output"] == "done"


def test_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the env var unset, the Phase 4 flag reads false (default OFF)."""
    from omnigent.server.auth import env_var_is_truthy

    monkeypatch.delenv("OMNIGENT_USE_TOOL_EXECUTION_CONTEXT", raising=False)
    assert env_var_is_truthy(tool_dispatch._USE_TOOL_EXECUTION_CONTEXT_ENV) is False


def test_build_context_passes_inbox_by_reference() -> None:
    """The internal builder forwards the inbox/map by reference, not copied."""
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}
    ctx = tool_dispatch._build_tool_execution_context(
        tool_name="sys_read_inbox",
        arguments="{}",
        server_client=None,
        terminal_registry=None,
        resource_registry=None,
        agent_spec=None,
        conversation_id="conv_1",
        task_id=None,
        agent_id=None,
        agent_name=None,
        runner_workspace=None,
        mcp_manager=None,
        session_inbox=inbox,
        session_async_tasks=tasks,
        harness_client=None,
        publish_event=None,
        filesystem_registry=None,
    )
    assert ctx.session_inbox is inbox
    assert ctx.session_async_tasks is tasks
    assert ctx.conversation_id == "conv_1"


@pytest.mark.asyncio
async def test_flag_on_routes_through_context_preserving_inbox_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With the flag ON, ``execute_tool`` dispatches through the context path,
    and the per-reference inbox survives the round-trip end-to-end.

    We stub ``_execute_tool_from_context`` to capture the context the seam
    built, proving (a) the flag-on branch is taken and (b) the SAME inbox
    object is carried through to the consumer.
    """
    monkeypatch.setenv("OMNIGENT_USE_TOOL_EXECUTION_CONTEXT", "1")
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    captured: dict[str, ToolExecutionContext] = {}

    async def _fake_consumer(ctx: ToolExecutionContext) -> str:
        captured["ctx"] = ctx
        return "stubbed"

    monkeypatch.setattr(tool_dispatch, "_execute_tool_from_context", _fake_consumer)

    out = await tool_dispatch.execute_tool(
        tool_name="sys_read_inbox",
        arguments="{}",
        session_inbox=inbox,
    )

    assert out == "stubbed"
    assert captured["ctx"].session_inbox is inbox


@pytest.mark.asyncio
async def test_flag_off_skips_context_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the flag OFF, the context consumer is never invoked."""
    monkeypatch.delenv("OMNIGENT_USE_TOOL_EXECUTION_CONTEXT", raising=False)
    called = {"n": 0}

    async def _fake_consumer(ctx: ToolExecutionContext) -> str:
        called["n"] += 1
        return "should-not-run"

    monkeypatch.setattr(tool_dispatch, "_execute_tool_from_context", _fake_consumer)

    # Unknown tool with no agent_spec falls through to the default chain and
    # returns an error string — but crucially the context consumer is skipped.
    out = await tool_dispatch.execute_tool(
        tool_name="definitely_not_a_real_tool",
        arguments="{}",
    )

    assert called["n"] == 0
    assert "should-not-run" not in out
