"""Tool dispatcher registry — Strategy + Registry over tool dispatch.

Part of the omnigent core-refactor spine (BDP-2327, Phase 5). This module
provides a Strategy + Registry over runner-local tool dispatch: each tool
*category* (MCP, OS env, REST, file, terminal, async inbox, sub-agent, …)
is a dispatcher :class:`Strategy` held in a :class:`DispatcherRegistry`.
A dispatcher pairs a ``matches(ctx)`` predicate with a ``dispatch(ctx)``
coroutine; the registry walks the dispatchers in precedence order and
routes a tool call to the first match. The dispatch bodies delegate to
the per-tool ``_execute_*`` helpers in ``tool_dispatch``, so there is one
dispatch implementation behind the strategies, not two.

Precedence is fixed and load-bearing: **MCP first** (an unconditional
override whenever an ``mcp_manager`` is present), then the static
name-set categories in declaration order, then the predicate-based tail
(``_is_spec_builtin_tool`` → ``_is_spec_local_python_tool`` →
``_is_uc_function_tool`` → catch-all ``_execute_spec_callable_tool``).
``_NATIVE_RELAY_BUILTIN_TOOLS`` and the per-category tool sets are imported
from ``tool_dispatch`` (never re-declared), so the registry's category
membership tracks ``tool_dispatch`` by construction;
``register_default_dispatchers`` builds the strategies in that precedence
order.

``execute_tool`` builds a :class:`ToolExecutionContext` and dispatches
through this registry. The registry is now the canonical runner-local
dispatch path; there is no feature-flagged inline fallback.

**Reference semantics carry through.** The mutable coordination objects
(``session_inbox``, ``session_async_tasks``) ride inside the
:class:`ToolExecutionContext` by reference, never copied — the same
invariant Phase 4 documents.

This module deliberately holds no omnigent service imports at module
load beyond the carrier type; the per-tool helpers are imported lazily
inside :func:`register_default_dispatchers` (mirroring ``tool_dispatch``'s
own lazy-import discipline) so the registry stays an upstream-friendly,
import-cycle-free seam.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from omnigent.runner.tool_execution_context import ToolExecutionContext

_logger = logging.getLogger(__name__)


# A dispatcher's match predicate and async body both receive the bundled
# per-dispatch context. ``parsed_args`` (the ``json.loads`` of
# ``ctx.arguments``) is threaded alongside so dispatchers that need the
# dict don't each re-parse — mirroring the single ``args = json.loads(...)``
# the elif chain does once at the top.
_MatchFn = Callable[["ToolExecutionContext", dict[str, Any]], bool]
_DispatchFn = Callable[["ToolExecutionContext", dict[str, Any]], Awaitable[str]]


@runtime_checkable
class ToolDispatcher(Protocol):
    """Strategy contract for dispatching one category of runner-local tools.

    Each dispatcher owns one branch of the historical elif chain: a
    ``matches`` predicate (the branch condition) and a ``dispatch``
    coroutine (the branch body). The :class:`DispatcherRegistry` holds an
    ordered list of these and routes a call to the first whose ``matches``
    returns ``True`` — reproducing the elif chain's first-match-wins
    precedence exactly.
    """

    name: str

    def matches(self, ctx: ToolExecutionContext, parsed_args: dict[str, Any]) -> bool:
        """Return ``True`` if this dispatcher owns ``ctx.tool_name``."""
        ...

    async def dispatch(self, ctx: ToolExecutionContext, parsed_args: dict[str, Any]) -> str:
        """Execute the tool and return its output string."""
        ...


@dataclass(frozen=True)
class _FunctionalDispatcher:
    """Concrete :class:`ToolDispatcher` built from a predicate + a coroutine.

    A thin, frozen adapter so each elif branch becomes one strategy value
    without a bespoke class per category. ``name`` is for logging/debug
    only; ``match`` and ``run`` are the branch condition and body.

    :param name: Human-facing category label, e.g. ``"os_env"``.
    :param match: Predicate mirroring the elif branch's condition.
    :param run: Coroutine mirroring the elif branch's body.
    """

    name: str
    match: _MatchFn
    run: _DispatchFn

    def matches(self, ctx: ToolExecutionContext, parsed_args: dict[str, Any]) -> bool:
        return self.match(ctx, parsed_args)

    async def dispatch(self, ctx: ToolExecutionContext, parsed_args: dict[str, Any]) -> str:
        return await self.run(ctx, parsed_args)


class DispatcherRegistry:
    """Ordered registry of :class:`ToolDispatcher` strategies (first match wins).

    The registry is the structural twin of ``execute_tool``'s elif chain:
    its dispatcher list is in the SAME precedence (MCP first, then the
    static name-set branches in declaration order, then the predicate
    tail), and :meth:`dispatch` walks the list and routes to the first
    matching strategy — the registry equivalent of falling through the
    ``elif`` ladder. The trailing catch-all dispatcher always matches, so
    the registry is total (every tool name resolves to exactly one
    dispatch), just like the elif chain's ``else`` arm.
    """

    def __init__(self) -> None:
        self._dispatchers: list[ToolDispatcher] = []

    def register(self, dispatcher: ToolDispatcher) -> None:
        """Append a dispatcher; order is precedence, so register MCP first.

        :param dispatcher: A strategy to consult after all already-registered
            ones (lower precedence).
        """
        self._dispatchers.append(dispatcher)

    @property
    def dispatchers(self) -> tuple[ToolDispatcher, ...]:
        """The registered dispatchers in precedence order (read-only view)."""
        return tuple(self._dispatchers)

    async def dispatch(self, ctx: ToolExecutionContext) -> str:
        """Route ``ctx`` to the first matching dispatcher and run it.

        Parses ``ctx.arguments`` once (matching the elif chain's single
        ``args = json.loads(...)``), then walks the dispatchers in
        precedence order. The first whose ``matches`` returns ``True``
        runs. Exceptions are caught and rendered to the SAME
        ``"Error: {type}: {msg}"`` string the elif chain produces, so the
        flag-on path's failure shape is identical to today.

        :param ctx: The bundled per-dispatch dependencies.
        :returns: Tool output string (or an ``"Error: ..."`` string).
        """
        try:
            parsed_args = json.loads(ctx.arguments)
        except json.JSONDecodeError:
            parsed_args = {}

        try:
            for dispatcher in self._dispatchers:
                if dispatcher.matches(ctx, parsed_args):
                    return await dispatcher.dispatch(ctx, parsed_args)
        except Exception as exc:  # noqa: BLE001
            return f"Error: {type(exc).__name__}: {exc}"

        # Unreachable: the catch-all dispatcher always matches. Kept as a
        # defensive net so a mis-registered registry fails loud, not silently.
        return f"Error: no dispatcher matched tool {ctx.tool_name!r}"


def register_default_dispatchers(registry: DispatcherRegistry) -> None:
    """Register the dispatchers in the SAME precedence as the elif chain.

    Each ``registry.register(...)`` below corresponds one-for-one to a
    branch of ``execute_tool``'s ``if/elif`` ladder, in declaration order:

    1. MCP (``mcp_manager is not None``) — unconditional override.
    2. ``_OS_ENV_TOOLS`` → ``_execute_os_env_tool``
    3. ``_REST_TOOLS`` → ``_execute_rest_tool``
    4. ``_FILE_TOOLS`` → ``_execute_file_tool``
    5. ``_TERMINAL_TOOLS`` → ``_execute_terminal_tool``
    6. ``_ASYNC_INBOX_TOOLS`` → ``_execute_async_inbox_tool``
    7. ``_SUBAGENT_TOOLS`` → ``_execute_subagent_tool``
    8. ``_LIST_MODELS_TOOLS`` → ``_execute_list_models_tool``
    9. ``_SESSION_CREATE_TOOLS`` → ``_execute_session_create``
    10. ``_SESSION_QUERY_TOOLS`` → ``_execute_session_query_tool``
    11. ``_WEB_FETCH_TOOLS`` → ``_execute_web_fetch_tool``
    12. ``_TIMER_TOOLS`` → ``_execute_timer_set`` / ``_execute_timer_cancel``
    13. ``_TASK_LIFECYCLE_TOOLS`` → ``_execute_task_lifecycle_tool``
    14. ``_SKILL_TOOLS`` → ``_execute_skill_tool``
    15. ``_COMMENT_TOOLS`` → ``_execute_comment_tool``
    16. ``_AGENT_TOOLS`` → ``_execute_agent_tool``
    17. ``_POLICY_TOOLS`` → ``_execute_policy_tool``
    18. ``_SKILL_ACQ_TOOLS`` → ``_execute_skill_acq_tool``
    19. ``_is_spec_builtin_tool`` → ``_execute_spec_builtin_tool``
    20. ``_is_spec_local_python_tool`` → ``_execute_local_python_tool``
    21. ``_is_uc_function_tool`` → ``_execute_uc_function_tool``
    22. catch-all → ``_execute_spec_callable_tool``

    Helpers and tool sets are imported lazily from ``tool_dispatch`` (the
    sets are imported, never re-declared, so they cannot drift). The
    dispatch bodies thread the identical per-branch kwargs the elif chain
    threads, so the helper called and the values passed are unchanged.

    :param registry: The registry to populate. Callers pass a fresh one.
    """
    from omnigent.communications.runner_tools import CommunicationServiceProvider
    from omnigent.runner import tool_dispatch as td
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

    communication_provider = CommunicationServiceProvider.for_runner_tools(
        inbox_handler=td._execute_async_inbox_tool,
        session_send_handler=td._execute_subagent_tool,
        session_create_handler=td._execute_session_create,
        session_query_handler=td._execute_session_query_tool,
    )

    # 1. MCP first — an unconditional override whenever an mcp_manager is
    # present, regardless of tool name (the elif chain's leading
    # ``if mcp_manager is not None``).
    async def _run_mcp(ctx: ToolExecutionContext, args: dict[str, Any]) -> str:
        return await ctx.mcp_manager.call_tool(ctx.agent_spec, ctx.tool_name, args)

    registry.register(
        _FunctionalDispatcher(
            name="mcp",
            match=lambda ctx, _args: ctx.mcp_manager is not None,
            run=_run_mcp,
        )
    )

    # 2. OS env tools.
    async def _run_os_env(ctx: ToolExecutionContext, args: dict[str, Any]) -> str:
        return await td._execute_os_env_tool(
            ctx.tool_name,
            args,
            agent_spec=ctx.agent_spec,
            conversation_id=ctx.conversation_id,
            runner_workspace=ctx.runner_workspace,
            filesystem_registry=ctx.filesystem_registry,
        )

    registry.register(
        _FunctionalDispatcher(
            name="os_env",
            match=lambda ctx, _args: ctx.tool_name in td._OS_ENV_TOOLS,
            run=_run_os_env,
        )
    )

    # 3-4. REST and file tools use class-backed server-client dispatchers.
    registry.register(RestDispatcher(td._REST_TOOLS))
    registry.register(FileDispatcher(td._FILE_TOOLS))

    # 5. Terminal tools.
    async def _run_terminal(ctx: ToolExecutionContext, args: dict[str, Any]) -> str:
        return await td._execute_terminal_tool(
            ctx.tool_name,
            args,
            acting_identity=ctx.acting_identity,
            terminal_registry=ctx.terminal_registry,
            resource_registry=ctx.resource_registry,
            agent_spec=ctx.agent_spec,
            conversation_id=ctx.conversation_id,
            task_id=ctx.task_id,
            agent_id=ctx.agent_id,
            runner_workspace=ctx.runner_workspace,
            session_inbox=ctx.session_inbox,
            publish_event=ctx.publish_event,
        )

    registry.register(
        _FunctionalDispatcher(
            name="terminal",
            match=lambda ctx, _args: ctx.tool_name in td._TERMINAL_TOOLS,
            run=_run_terminal,
        )
    )

    # 6-7. Communication tools use class-backed, DI-composed dispatchers.
    registry.register(InboxDispatcher(td._ASYNC_INBOX_TOOLS, communication_provider))
    registry.register(SessionSendDispatcher(td._SUBAGENT_TOOLS, communication_provider))

    # 8. List-models tool.
    async def _run_list_models(ctx: ToolExecutionContext, _args: dict[str, Any]) -> str:
        return await td._execute_list_models_tool(agent_spec=ctx.agent_spec)

    registry.register(
        _FunctionalDispatcher(
            name="list_models",
            match=lambda ctx, _args: ctx.tool_name in td._LIST_MODELS_TOOLS,
            run=_run_list_models,
        )
    )

    # 9-10. Child session create and query use the same communication provider.
    registry.register(SessionCreateDispatcher(td._SESSION_CREATE_TOOLS, communication_provider))
    registry.register(SessionQueryDispatcher(td._SESSION_QUERY_TOOLS, communication_provider))

    # 11. web_fetch tool.
    async def _run_web_fetch(ctx: ToolExecutionContext, args: dict[str, Any]) -> str:
        return await td._execute_web_fetch_tool(
            args,
            server_client=ctx.server_client,
            conversation_id=ctx.conversation_id,
            agent_spec=ctx.agent_spec,
            task_id=ctx.task_id,
            publish_event=ctx.publish_event,
            session_inbox=ctx.session_inbox,
        )

    registry.register(
        _FunctionalDispatcher(
            name="web_fetch",
            match=lambda ctx, _args: ctx.tool_name in td._WEB_FETCH_TOOLS,
            run=_run_web_fetch,
        )
    )

    # 12. Timer tools — set vs cancel keyed on the tool name, matching the
    # elif branch's inner ``if tool_name == "sys_timer_set"``.
    async def _run_timer(ctx: ToolExecutionContext, args: dict[str, Any]) -> str:
        if ctx.tool_name == "sys_timer_set":
            return await td._execute_timer_set(
                args,
                server_client=ctx.server_client,
                conversation_id=ctx.conversation_id,
            )
        return await td._execute_timer_cancel(
            args,
            conversation_id=ctx.conversation_id,
        )

    registry.register(
        _FunctionalDispatcher(
            name="timer",
            match=lambda ctx, _args: ctx.tool_name in td._TIMER_TOOLS,
            run=_run_timer,
        )
    )

    # 13. Task-lifecycle tools.
    async def _run_task_lifecycle(ctx: ToolExecutionContext, args: dict[str, Any]) -> str:
        return await td._execute_task_lifecycle_tool(
            args,
            session_async_tasks=ctx.session_async_tasks,
            conversation_id=ctx.conversation_id,
            server_client=ctx.server_client,
        )

    registry.register(
        _FunctionalDispatcher(
            name="task_lifecycle",
            match=lambda ctx, _args: ctx.tool_name in td._TASK_LIFECYCLE_TOOLS,
            run=_run_task_lifecycle,
        )
    )

    # 14. Skill tools. ``_execute_skill_tool`` is synchronous; await of its
    # plain value would fail, so the body returns it directly — matching the
    # elif branch's non-awaited ``output = _execute_skill_tool(...)``.
    async def _run_skill(ctx: ToolExecutionContext, args: dict[str, Any]) -> str:
        return td._execute_skill_tool(
            ctx.tool_name,
            args,
            acting_identity=ctx.acting_identity,
            agent_spec=ctx.agent_spec,
            runner_workspace=ctx.runner_workspace,
        )

    registry.register(
        _FunctionalDispatcher(
            name="skill",
            match=lambda ctx, _args: ctx.tool_name in td._SKILL_TOOLS,
            run=_run_skill,
        )
    )

    # 15. Comment tools. (Passes the raw ``ctx.arguments`` string.)
    async def _run_comment(ctx: ToolExecutionContext, _args: dict[str, Any]) -> str:
        return await td._execute_comment_tool(
            ctx.tool_name,
            ctx.arguments,
            conversation_id=ctx.conversation_id,
            server_client=ctx.server_client,
        )

    registry.register(
        _FunctionalDispatcher(
            name="comment",
            match=lambda ctx, _args: ctx.tool_name in td._COMMENT_TOOLS,
            run=_run_comment,
        )
    )

    # 16-18. Server-backed agent, policy, and skill-acquisition tools.
    registry.register(AgentDispatcher(td._AGENT_TOOLS))
    registry.register(PolicyDispatcher(td._POLICY_TOOLS))
    registry.register(SkillAcquisitionDispatcher(td._SKILL_ACQ_TOOLS))

    # 19. Spec-declared builtin tool (predicate branch). ``ctx.arguments`` is
    # the raw JSON string, matching the elif branch.
    async def _run_spec_builtin(ctx: ToolExecutionContext, _args: dict[str, Any]) -> str:
        return await td._execute_spec_builtin_tool(
            ctx.tool_name,
            ctx.arguments,
            acting_identity=ctx.acting_identity,
            agent_spec=ctx.agent_spec,
            conversation_id=ctx.conversation_id,
            task_id=ctx.task_id,
            agent_id=ctx.agent_id,
            runner_workspace=ctx.runner_workspace,
        )

    registry.register(
        _FunctionalDispatcher(
            name="spec_builtin",
            match=lambda ctx, _args: td._is_spec_builtin_tool(ctx.tool_name, ctx.agent_spec),
            run=_run_spec_builtin,
        )
    )

    # 20. Spec-defined local Python tool (predicate branch). ``ctx.arguments``
    # is the raw JSON string here, matching the elif branch.
    async def _run_local_python(ctx: ToolExecutionContext, _args: dict[str, Any]) -> str:
        return await td._execute_local_python_tool(
            ctx.tool_name,
            ctx.arguments,
            acting_identity=ctx.acting_identity,
            agent_spec=ctx.agent_spec,
            conversation_id=ctx.conversation_id,
            task_id=ctx.task_id,
            agent_id=ctx.agent_id,
            runner_workspace=ctx.runner_workspace,
        )

    registry.register(
        _FunctionalDispatcher(
            name="local_python",
            match=lambda ctx, _args: td._is_spec_local_python_tool(ctx.tool_name, ctx.agent_spec),
            run=_run_local_python,
        )
    )

    # 21. Unity Catalog function tool (predicate branch).
    async def _run_uc_function(ctx: ToolExecutionContext, args: dict[str, Any]) -> str:
        return await td._execute_uc_function_tool(ctx.tool_name, args, agent_spec=ctx.agent_spec)

    registry.register(
        _FunctionalDispatcher(
            name="uc_function",
            match=lambda ctx, _args: td._is_uc_function_tool(ctx.tool_name, ctx.agent_spec),
            run=_run_uc_function,
        )
    )

    # 22. Catch-all (the elif chain's ``else``) — spec callable tool. Always
    # matches, so the registry is total: every tool name resolves to exactly
    # one dispatch, mirroring the elif chain's final ``else`` arm.
    async def _run_spec_callable(ctx: ToolExecutionContext, args: dict[str, Any]) -> str:
        return await td._execute_spec_callable_tool(ctx.tool_name, args, agent_spec=ctx.agent_spec)

    registry.register(
        _FunctionalDispatcher(
            name="spec_callable",
            match=lambda _ctx, _args: True,
            run=_run_spec_callable,
        )
    )


def build_default_registry() -> DispatcherRegistry:
    """Construct a registry pre-loaded with the default dispatchers.

    Convenience factory: a fresh :class:`DispatcherRegistry` with
    :func:`register_default_dispatchers` already applied, in the elif
    chain's precedence (MCP first).

    :returns: A ready-to-use registry.
    """
    registry = DispatcherRegistry()
    register_default_dispatchers(registry)
    return registry


async def dispatch_via_registry(ctx: ToolExecutionContext) -> str:
    """Dispatch a tool through a fresh default :class:`DispatcherRegistry`.

    ``execute_tool`` calls this canonical registry entry point. A fresh
    registry per call keeps the seam stateless (dispatchers close over
    nothing mutable, so there is no per-call cost beyond list construction)
    and avoids any module-level singleton that would couple test isolation to
    import order.

    :param ctx: The bundled per-dispatch dependencies.
    :returns: Tool output string.
    """
    return await build_default_registry().dispatch(ctx)
