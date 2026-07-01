"""Class-backed dispatchers for runner communication tools."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from omnigent.communications.runner_tools import CommunicationServiceProvider

if TYPE_CHECKING:
    from omnigent.runner.tool_execution_context import ToolExecutionContext


@dataclass(frozen=True)
class InboxDispatcher:
    """Dispatch ``sys_call_async`` / ``sys_read_inbox`` through ``InboxService``."""

    tools: Collection[str]
    provider: CommunicationServiceProvider
    name: str = "async_inbox"

    def matches(self, ctx: ToolExecutionContext, parsed_args: dict[str, Any]) -> bool:
        del parsed_args
        return ctx.tool_name in self.tools

    async def dispatch(self, ctx: ToolExecutionContext, parsed_args: dict[str, Any]) -> str:
        scope = self.provider.runner_scope(ctx)
        return await scope.inbox.dispatch(ctx, parsed_args)


@dataclass(frozen=True)
class SessionSendDispatcher:
    """Dispatch ``sys_session_send`` through ``DelegationService``."""

    tools: Collection[str]
    provider: CommunicationServiceProvider
    name: str = "subagent"

    def matches(self, ctx: ToolExecutionContext, parsed_args: dict[str, Any]) -> bool:
        del parsed_args
        return ctx.tool_name in self.tools

    async def dispatch(self, ctx: ToolExecutionContext, parsed_args: dict[str, Any]) -> str:
        scope = self.provider.runner_scope(ctx)
        return await scope.delegation.send(ctx, parsed_args)


@dataclass(frozen=True)
class SessionCreateDispatcher:
    """Dispatch ``sys_session_create`` through ``DelegationService``."""

    tools: Collection[str]
    provider: CommunicationServiceProvider
    name: str = "session_create"

    def matches(self, ctx: ToolExecutionContext, parsed_args: dict[str, Any]) -> bool:
        del parsed_args
        return ctx.tool_name in self.tools

    async def dispatch(self, ctx: ToolExecutionContext, parsed_args: dict[str, Any]) -> str:
        scope = self.provider.runner_scope(ctx)
        return await scope.delegation.create(ctx, parsed_args)


@dataclass(frozen=True)
class SessionQueryDispatcher:
    """Dispatch read/close session tools through ``SessionQueryToolService``."""

    tools: Collection[str]
    provider: CommunicationServiceProvider
    name: str = "session_query"

    def matches(self, ctx: ToolExecutionContext, parsed_args: dict[str, Any]) -> bool:
        del parsed_args
        return ctx.tool_name in self.tools

    async def dispatch(self, ctx: ToolExecutionContext, parsed_args: dict[str, Any]) -> str:
        del parsed_args
        scope = self.provider.runner_scope(ctx)
        return await scope.session_query.dispatch(ctx)


__all__ = [
    "InboxDispatcher",
    "SessionCreateDispatcher",
    "SessionQueryDispatcher",
    "SessionSendDispatcher",
]
