"""Class-driven communication services for runner-local tool dispatch."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx


class RunnerToolContext(Protocol):
    """Subset of ``ToolExecutionContext`` needed by communication services."""

    tool_name: str
    arguments: str
    server_client: httpx.AsyncClient | None
    terminal_registry: Any
    resource_registry: Any
    agent_spec: Any
    conversation_id: str | None
    task_id: str | None
    agent_id: str | None
    agent_name: str | None
    runner_workspace: Path | None
    mcp_manager: Any
    session_inbox: asyncio.Queue[dict[str, Any]] | None
    session_async_tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] | None
    harness_client: httpx.AsyncClient | None
    publish_event: Callable[[str, dict[str, Any]], None] | None
    filesystem_registry: Any


InboxHandler = Callable[..., Awaitable[str]]
SessionSendHandler = Callable[..., Awaitable[str]]
SessionCreateHandler = Callable[..., Awaitable[str]]
SessionQueryHandler = Callable[..., Awaitable[str]]


class InboxService:
    """Dispatch async inbox communication tools through injected behavior."""

    def __init__(self, handler: InboxHandler) -> None:
        self._handler = handler

    async def dispatch(self, ctx: RunnerToolContext, args: dict[str, Any]) -> str:
        return await self._handler(
            ctx.tool_name,
            args,
            session_inbox=ctx.session_inbox,
            session_async_tasks=ctx.session_async_tasks,
            harness_client=ctx.harness_client,
            server_client=ctx.server_client,
            terminal_registry=ctx.terminal_registry,
            resource_registry=ctx.resource_registry,
            agent_spec=ctx.agent_spec,
            conversation_id=ctx.conversation_id,
            task_id=ctx.task_id,
            agent_id=ctx.agent_id,
            agent_name=ctx.agent_name,
            runner_workspace=ctx.runner_workspace,
            mcp_manager=ctx.mcp_manager,
            filesystem_registry=ctx.filesystem_registry,
        )


class DelegationService:
    """Dispatch child-session communication tools through injected behavior."""

    def __init__(
        self,
        *,
        send_handler: SessionSendHandler,
        create_handler: SessionCreateHandler,
    ) -> None:
        self._send_handler = send_handler
        self._create_handler = create_handler

    async def send(self, ctx: RunnerToolContext, args: dict[str, Any]) -> str:
        return await self._send_handler(
            args,
            server_client=ctx.server_client,
            conversation_id=ctx.conversation_id,
            agent_spec=ctx.agent_spec,
            publish_event=ctx.publish_event,
            session_inbox=ctx.session_inbox,
        )

    async def create(self, ctx: RunnerToolContext, args: dict[str, Any]) -> str:
        return await self._create_handler(
            args,
            server_client=ctx.server_client,
            conversation_id=ctx.conversation_id,
            publish_event=ctx.publish_event,
            agent_spec=ctx.agent_spec,
            runner_workspace=ctx.runner_workspace,
        )


class SessionQueryToolService:
    """Dispatch session query tools through injected behavior."""

    def __init__(self, handler: SessionQueryHandler) -> None:
        self._handler = handler

    async def dispatch(self, ctx: RunnerToolContext) -> str:
        return await self._handler(
            ctx.tool_name,
            ctx.arguments,
            conversation_id=ctx.conversation_id,
            server_client=ctx.server_client,
        )


@dataclass(frozen=True)
class RunnerCommunicationScope:
    """Per-dispatch communication scope built from a runner tool context."""

    ctx: RunnerToolContext
    inbox: InboxService
    delegation: DelegationService
    session_query: SessionQueryToolService


class CommunicationServiceProvider:
    """Composition root for communication services at runner turn scope."""

    def __init__(
        self,
        *,
        inbox_service: InboxService,
        delegation_service: DelegationService,
        session_query_service: SessionQueryToolService,
    ) -> None:
        self._inbox_service = inbox_service
        self._delegation_service = delegation_service
        self._session_query_service = session_query_service

    @classmethod
    def for_runner_tools(
        cls,
        *,
        inbox_handler: InboxHandler,
        session_send_handler: SessionSendHandler,
        session_create_handler: SessionCreateHandler,
        session_query_handler: SessionQueryHandler,
    ) -> CommunicationServiceProvider:
        """Build the runner communication provider from injected handlers."""
        return cls(
            inbox_service=InboxService(inbox_handler),
            delegation_service=DelegationService(
                send_handler=session_send_handler,
                create_handler=session_create_handler,
            ),
            session_query_service=SessionQueryToolService(session_query_handler),
        )

    def runner_scope(self, ctx: RunnerToolContext) -> RunnerCommunicationScope:
        """Create a runner turn scope while preserving context references."""
        return RunnerCommunicationScope(
            ctx=ctx,
            inbox=self._inbox_service,
            delegation=self._delegation_service,
            session_query=self._session_query_service,
        )


__all__ = [
    "CommunicationServiceProvider",
    "DelegationService",
    "InboxService",
    "RunnerCommunicationScope",
    "RunnerToolContext",
    "SessionQueryToolService",
]
