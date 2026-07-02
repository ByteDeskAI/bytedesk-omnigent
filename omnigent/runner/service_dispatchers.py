"""Class-backed dispatchers for server-backed runner tools."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnigent.runner.tool_execution_context import ToolExecutionContext


@dataclass(frozen=True)
class RestDispatcher:
    """Dispatch REST helper tools through the server client."""

    tools: Collection[str]
    name: str = "rest"

    def matches(self, ctx: ToolExecutionContext, parsed_args: dict[str, Any]) -> bool:
        del parsed_args
        return ctx.tool_name in self.tools

    async def dispatch(self, ctx: ToolExecutionContext, parsed_args: dict[str, Any]) -> str:
        from omnigent.runner import tool_dispatch as td

        return await td._execute_rest_tool(
            ctx.tool_name,
            parsed_args,
            ctx.server_client,
            agent_id=ctx.agent_id,
            conversation_id=ctx.conversation_id,
        )


@dataclass(frozen=True)
class FileDispatcher:
    """Dispatch file upload/download tools through the server client."""

    tools: Collection[str]
    name: str = "file"

    def matches(self, ctx: ToolExecutionContext, parsed_args: dict[str, Any]) -> bool:
        del parsed_args
        return ctx.tool_name in self.tools

    async def dispatch(self, ctx: ToolExecutionContext, parsed_args: dict[str, Any]) -> str:
        from omnigent.runner import tool_dispatch as td

        return await td._execute_file_tool(
            ctx.tool_name,
            parsed_args,
            ctx.server_client,
            conversation_id=ctx.conversation_id,
            agent_spec=ctx.agent_spec,
            runner_workspace=ctx.runner_workspace,
        )


@dataclass(frozen=True)
class AgentDispatcher:
    """Dispatch agent-management tools through the server client."""

    tools: Collection[str]
    name: str = "agent"

    def matches(self, ctx: ToolExecutionContext, parsed_args: dict[str, Any]) -> bool:
        del parsed_args
        return ctx.tool_name in self.tools

    async def dispatch(self, ctx: ToolExecutionContext, parsed_args: dict[str, Any]) -> str:
        from omnigent.runner import tool_dispatch as td

        return await td._execute_agent_tool(
            ctx.tool_name,
            parsed_args,
            server_client=ctx.server_client,
            agent_spec=ctx.agent_spec,
            conversation_id=ctx.conversation_id,
            runner_workspace=ctx.runner_workspace,
        )


@dataclass(frozen=True)
class PolicyDispatcher:
    """Dispatch policy tools through the server client."""

    tools: Collection[str]
    name: str = "policy"

    def matches(self, ctx: ToolExecutionContext, parsed_args: dict[str, Any]) -> bool:
        del parsed_args
        return ctx.tool_name in self.tools

    async def dispatch(self, ctx: ToolExecutionContext, parsed_args: dict[str, Any]) -> str:
        del parsed_args
        from omnigent.runner import tool_dispatch as td

        return await td._execute_policy_tool(
            ctx.tool_name,
            ctx.arguments,
            conversation_id=ctx.conversation_id,
            server_client=ctx.server_client,
        )


@dataclass(frozen=True)
class SkillAcquisitionDispatcher:
    """Dispatch skill-acquisition tools through the server client."""

    tools: Collection[str]
    name: str = "skill_acq"

    def matches(self, ctx: ToolExecutionContext, parsed_args: dict[str, Any]) -> bool:
        del parsed_args
        return ctx.tool_name in self.tools

    async def dispatch(self, ctx: ToolExecutionContext, parsed_args: dict[str, Any]) -> str:
        from omnigent.runner import tool_dispatch as td

        return await td._execute_skill_acq_tool(
            ctx.tool_name,
            parsed_args,
            ctx.server_client,
        )


__all__ = [
    "AgentDispatcher",
    "FileDispatcher",
    "PolicyDispatcher",
    "RestDispatcher",
    "SkillAcquisitionDispatcher",
]
