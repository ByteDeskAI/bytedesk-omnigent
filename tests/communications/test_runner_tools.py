"""Tests for runner communication service composition."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omnigent.communications.runner_tools import CommunicationServiceProvider
from omnigent.runner.tool_execution_context import ToolExecutionContext


async def _unused_handler(*_args: Any, **_kwargs: Any) -> str:
    return "unused"


@pytest.mark.asyncio
async def test_runner_scope_preserves_context_references() -> None:
    """The communication provider keeps runner turn state by reference."""
    seen: dict[str, Any] = {}

    async def _inbox_handler(tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        seen.update(tool_name=tool_name, args=args, **kwargs)
        return "inbox-ok"

    provider = CommunicationServiceProvider.for_runner_tools(
        inbox_handler=_inbox_handler,
        session_send_handler=_unused_handler,
        session_create_handler=_unused_handler,
        session_query_handler=_unused_handler,
    )

    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}
    async with httpx.AsyncClient(base_url="http://harness") as harness_client:
        ctx = ToolExecutionContext(
            tool_name="sys_read_inbox",
            arguments="{}",
            session_inbox=inbox,
            session_async_tasks=tasks,
            harness_client=harness_client,
            conversation_id="conv_parent",
        )

        scope = provider.runner_scope(ctx)
        output = await scope.inbox.dispatch(ctx, {"tail": 1})

    assert output == "inbox-ok"
    assert scope.ctx is ctx
    assert seen["tool_name"] == "sys_read_inbox"
    assert seen["args"] == {"tail": 1}
    assert seen["session_inbox"] is inbox
    assert seen["session_async_tasks"] is tasks
    assert seen["harness_client"] is harness_client
    assert seen["conversation_id"] == "conv_parent"


@pytest.mark.asyncio
async def test_delegation_service_injects_send_and_create_handlers() -> None:
    """The provider wires create/send as explicit delegation service methods."""
    calls: list[tuple[str, dict[str, Any], str | None]] = []

    async def _send_handler(args: dict[str, Any], **kwargs: Any) -> str:
        calls.append(("send", args, kwargs.get("conversation_id")))
        return "sent"

    async def _create_handler(args: dict[str, Any], **kwargs: Any) -> str:
        calls.append(("create", args, kwargs.get("conversation_id")))
        return "created"

    provider = CommunicationServiceProvider.for_runner_tools(
        inbox_handler=_unused_handler,
        session_send_handler=_send_handler,
        session_create_handler=_create_handler,
        session_query_handler=_unused_handler,
    )
    ctx = ToolExecutionContext(
        tool_name="sys_session_send",
        arguments="{}",
        conversation_id="conv_parent",
    )

    scope = provider.runner_scope(ctx)
    send_output = await scope.delegation.send(ctx, {"session_id": "conv_child"})
    create_output = await scope.delegation.create(ctx, {"agent_id": "ag_child"})

    assert send_output == "sent"
    assert create_output == "created"
    assert calls == [
        ("send", {"session_id": "conv_child"}, "conv_parent"),
        ("create", {"agent_id": "ag_child"}, "conv_parent"),
    ]
