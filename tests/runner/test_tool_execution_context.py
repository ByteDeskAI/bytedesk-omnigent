"""Tests for omnigent.runner.tool_execution_context (BDP-2327, Phase 4)."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from omnigent.identity.defaults import acting_identity_for
from omnigent.runner import tool_dispatch
from omnigent.runner.tool_execution_context import ToolExecutionContext
from omnigent.server.principal import Principal


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
    with pytest.raises(FrozenInstanceError):
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
        acting_identity=None,
    )
    assert ctx.session_inbox is inbox
    assert ctx.session_async_tasks is tasks
    assert ctx.conversation_id == "conv_1"


def test_build_context_passes_acting_identity() -> None:
    """The carrier keeps the caller identity available to registry dispatchers."""
    ident = acting_identity_for(Principal(user_id="alice@x"), agent_id="maya")
    ctx = tool_dispatch._build_tool_execution_context(
        tool_name="some_local_tool",
        arguments="{}",
        server_client=None,
        terminal_registry=None,
        resource_registry=None,
        agent_spec=None,
        conversation_id="conv_1",
        task_id=None,
        agent_id="maya",
        agent_name=None,
        runner_workspace=None,
        mcp_manager=None,
        session_inbox=None,
        session_async_tasks=None,
        harness_client=None,
        publish_event=None,
        filesystem_registry=None,
        acting_identity=ident,
    )

    assert ctx.acting_identity is ident
