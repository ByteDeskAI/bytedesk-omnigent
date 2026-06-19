"""Tool execution context — typed bundle for runner-local tool dispatch.

Part of the omnigent core-refactor spine (BDP-2327, Phase 4). Today
:func:`omnigent.runner.tool_dispatch.dispatch_tool_locally` and
:func:`~omnigent.runner.tool_dispatch.execute_tool` each thread the same
15+ positional/keyword params (``server_client``, ``terminal_registry``,
``resource_registry``, ``agent_spec``, ``conversation_id``, ``task_id``,
``agent_id``, ``agent_name``, ``runner_workspace``, ``mcp_manager``,
``session_inbox``, ``session_async_tasks``, ``harness_client``,
``publish_event``, ``filesystem_registry``) through a long kwargs list.
Each new per-dispatch dependency widens both signatures.

:class:`ToolExecutionContext` is a frozen dataclass that bundles those
params into one value. It is introduced behind
``OMNIGENT_USE_TOOL_EXECUTION_CONTEXT`` (default OFF): when the flag is
off, ``execute_tool`` keeps its existing per-kwarg signature and behaves
byte-identically to today; when the flag is on, ``execute_tool`` builds a
context from those same args and dispatches through the context-consuming
path. The two paths thread the identical values to the same per-tool
``_execute_*`` helpers — the context is a carrier, not a behavior change.

**Reference semantics are the whole point.** The mutable coordination
objects — ``session_inbox`` (the per-session :class:`asyncio.Queue` that
``sys_call_async`` pushes onto and ``sys_read_inbox`` drains) and
``session_async_tasks`` (the handle→``(Task, Event)`` map
``sys_cancel_async`` signals through) — are held BY REFERENCE, never
copied. A background task that mutates ``context.session_inbox`` must be
visible to the caller that still holds the same queue. ``frozen=True``
freezes the *bindings* (you cannot rebind ``context.session_inbox`` to a
different queue), not the referents (the queue's contents stay mutable and
shared). That is exactly the invariant async tool dispatch depends on.

This module deliberately holds no omnigent service imports beyond the
filesystem-registry type hint (guarded under ``TYPE_CHECKING``), so it
stays a generic, upstream-friendly carrier with no runtime dependency on
the rest of the dispatch module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

    from omnigent.runtime.filesystem_registry import FilesystemRegistry


@dataclass(frozen=True)
class ToolExecutionContext:
    """Bundled, by-reference dependencies for one runner-local tool dispatch.

    Mirrors the keyword-only parameters of
    :func:`omnigent.runner.tool_dispatch.execute_tool` one-for-one. Every
    field defaults to ``None`` so the context can be built from a partial
    dispatch site (the async background path, in-process callers) without
    forcing callers to spell out absent dependencies.

    ``frozen=True`` makes the field *bindings* immutable — it does NOT
    deep-copy the referents. The mutable coordination objects
    (:attr:`session_inbox`, :attr:`session_async_tasks`) are stored by
    reference precisely so a background task's mutation is observed by the
    caller that shares the same object.

    :param tool_name: Tool to execute, e.g. ``"sys_os_shell"``.
    :param arguments: JSON-encoded arguments string.
    :param server_client: httpx client pointed at the Omnigent server.
    :param terminal_registry: Runner-local terminal registry, or ``None``.
    :param resource_registry: Session-resource registry, or ``None``.
    :param agent_spec: Agent spec resolved for the current turn, or ``None``.
    :param conversation_id: Conversation id, e.g. ``"conv_123"``.
    :param task_id: Current task id, or ``None``.
    :param agent_id: Current agent id, or ``None``.
    :param agent_name: Current agent name, or ``None``.
    :param runner_workspace: CLI launch workspace path, or ``None``.
    :param mcp_manager: Runner MCP manager for MCP-owned tools, or ``None``.
    :param session_inbox: Per-session inbox queue held BY REFERENCE — async
        tool completions push here and ``sys_read_inbox`` drains it.
    :param session_async_tasks: Per-session handle→``(Task, Event)`` map held
        BY REFERENCE — ``sys_cancel_async`` signals cancellation through it.
    :param harness_client: httpx client pointed at the harness, or ``None``.
    :param publish_event: Per-session SSE emit callback, or ``None``.
    :param filesystem_registry: Registry tracking agent file mods, or ``None``.
    """

    tool_name: str
    arguments: str
    server_client: httpx.AsyncClient | None = None
    terminal_registry: Any | None = None
    resource_registry: Any | None = None
    agent_spec: Any | None = None
    conversation_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    runner_workspace: Path | None = None
    mcp_manager: Any | None = None
    session_inbox: asyncio.Queue[dict[str, Any]] | None = None
    session_async_tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] | None = None
    harness_client: httpx.AsyncClient | None = None
    publish_event: Callable[[str, dict[str, Any]], None] | None = None
    filesystem_registry: FilesystemRegistry | None = None
