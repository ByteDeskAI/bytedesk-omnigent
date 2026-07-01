"""Runner-local tool dispatch for intercepted action_required events.

Per designs/RUNNER_TOOL_DISPATCH.md, the runner dispatches most tools
locally and relays action_required events upstream UNCHANGED for
visibility (the executor emits ToolCallInProgress/ToolCallObserved for
the REPL but doesn't dispatch itself — it checks should_dispatch_locally
and skips).

Tool categories:
- _OS_ENV_TOOLS: execute through a runner-local OSEnvironment (sys_os_*)
- _REST_TOOLS: call server REST APIs (sys_call_async, sys_cancel_async)
- _FILE_TOOLS: call server file APIs (sys_upload/download/list_files)
- _TERMINAL_TOOLS: runner-local TerminalRegistry
- MCP tools: spec-defined; dispatched via RunnerMcpManager passed
  in by proxy_stream (designs/RUNNER_MCP.md). Not in the static
  allow-list because names vary per spec.
- Client-side tools: tunneled via REPL (deferred)
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, cast

if TYPE_CHECKING:
    from omnigent.identity.identity import ActingIdentity
    from omnigent.runner.mcp_manager import McpManager
    from omnigent.runner.resource_registry import SessionResourceRegistry
    from omnigent.runtime.filesystem_registry import FilesystemRegistry
    from omnigent.spec.types import AgentSpec
    from omnigent.terminals.registry import TerminalRegistry

import httpx

from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE,
    CODEX_NATIVE_WRAPPER_VALUE,
)
from omnigent.model_override import (
    harness_supports_model_override,
    model_family_mismatch,
    normalize_model_for_provider,
    validate_model_override,
)
from omnigent.runner.tool_dispatch._helpers import _CancelAsyncToolResult
from omnigent.runner.subagent_status import (
    _ACTIVE as _SUBAGENT_ACTIVE_STATUSES,
)
from omnigent.runner.subagent_status import (
    SubagentWorkStatus,
)
from omnigent.runner.tool_dispatch._subagent import (
    _cancel_subagent_task,
    _cleanup_drained_subagent_work,
    _evaluate_subagent_inbox_output,
)
from omnigent.runner.tool_dispatch._terminal import _format_terminal_idle_item
from omnigent.runner.tool_execution_context import ToolExecutionContext
from omnigent.runtime import pending_elicitations
from omnigent.session_lifecycle import (
    CLOSED_LABEL_KEY,
    CLOSED_LABEL_VALUE,
    is_session_closed,
    title_without_closed_marker,
)
from omnigent.tools import ToolManager
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.async_inbox import (
    SysCallAsyncTool,
    SysCancelAsyncTool,
    SysCancelTaskTool,
    SysReadInboxTool,
)
from omnigent.tools.builtins.download_file import DownloadFileTool
from omnigent.tools.builtins.list_comments import ListCommentsTool
from omnigent.tools.builtins.os_env import (
    SysOsEditTool,
    SysOsReadTool,
    SysOsShellTool,
    SysOsWriteTool,
)
from omnigent.tools.builtins.spawn import (
    # Shared contract values with the in-process sys_session_* tools. Imported
    # (not duplicated) so the runner's REST-backed peek clamps to the same
    # bounds the LLM-facing tool schema advertises and tombstones with the
    # same marker the in-process close writes.
    _ACTIVITY_MAX_CHARS,
    _CLOSED_TITLE_INFIX,
    _HISTORY_DEFAULT_TAIL,
    _clamp_tail_items,
)
from omnigent.tools.builtins.sys_terminal import (
    SysTerminalCloseTool,
    SysTerminalLaunchTool,
    SysTerminalListTool,
    SysTerminalReadTool,
    SysTerminalSendTool,
)
from omnigent.tools.builtins.update_comment import UpdateCommentTool
from omnigent.tools.builtins.upload_file import UploadFileTool, safe_resolve

_logger = logging.getLogger(__name__)
def _import_package_bindings() -> None:
    from . import _constants as _pkg_constants
    from . import _state as _pkg_state
    g = globals()
    for _mod in (_pkg_constants, _pkg_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_package_bindings()

async def _execute_async_inbox_tool(
    tool_name: str,
    args: dict[str, Any],
    *,
    session_inbox: asyncio.Queue[dict[str, Any]] | None,
    session_async_tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] | None,
    server_client: httpx.AsyncClient | None,
    terminal_registry: TerminalRegistry | None,
    resource_registry: SessionResourceRegistry | None,
    agent_spec: AgentSpecLike | None,
    conversation_id: str | None,
    task_id: str | None,
    agent_id: str | None,
    agent_name: str | None,
    runner_workspace: Path | None,
    mcp_manager: McpManager | None,
    filesystem_registry: FilesystemRegistry | None = None,
    harness_client: httpx.AsyncClient | None = None,
) -> str:
    """
    Runner-local dispatch for async inbox tools.

    Backed by per-session ``asyncio.Queue`` (SESSION_REARCHITECTURE
    Step 7).

    :param tool_name: Tool name, e.g. ``"sys_read_inbox"``.
    :param args: Parsed JSON arguments from the LLM.
    :param session_inbox: Per-session completion queue.
    :param session_async_tasks: Per-session handle_id →
        ``(Task, cancel_event)`` tuple map.
    :param filesystem_registry: Optional registry for tracking file
        changes made by tools spawned via ``sys_call_async``.
        Forwarded to ``_spawn_async_tool`` so that async OS-env tool
        calls record paths for the ``GET …/changes`` endpoint.
    :param resource_registry: Optional session-resource registry used by
        async terminal-tool launches.
    :param harness_client: Unused; kept for caller compatibility.
    :returns: Tool output string.
    """
    del harness_client
    if tool_name == SysReadInboxTool.name():
        return await _drain_inbox(
            session_inbox,
            server_client=server_client,
            conversation_id=conversation_id,
        )

    if tool_name == SysCallAsyncTool.name():
        return _spawn_async_tool(
            args,
            session_inbox=session_inbox,
            session_async_tasks=session_async_tasks,
            server_client=server_client,
            terminal_registry=terminal_registry,
            resource_registry=resource_registry,
            agent_spec=agent_spec,
            conversation_id=conversation_id,
            task_id=task_id,
            agent_id=agent_id,
            agent_name=agent_name,
            runner_workspace=runner_workspace,
            mcp_manager=mcp_manager,
            filesystem_registry=filesystem_registry,
        )

    if tool_name == SysCancelAsyncTool.name():
        return _cancel_async_tool(
            args,
            session_async_tasks=session_async_tasks,
        )

    return f"Error: {tool_name} not implemented in async inbox dispatch"

def _truncate_inbox_output(output: object) -> str:
    """
    Convert an inbox payload output to bounded text.

    :param output: Raw payload output, e.g. ``"done"`` or an error
        object converted by the caller.
    :returns: Text capped for LLM delivery.
    """
    text = output if isinstance(output, str) else str(output)
    if len(text) <= _INBOX_OUTPUT_MAX_CHARS:
        return text
    return (
        text[:_INBOX_OUTPUT_MAX_CHARS].rstrip()
        + f"\n...[truncated {len(text) - _INBOX_OUTPUT_MAX_CHARS} chars]"
    )

def _format_async_task_item(payload: dict[str, Any]) -> str:
    """
    Render a completed/failed/cancelled async-task inbox payload.

    :param payload: Async-task payload with ``handle_id``,
        ``tool_name``, ``status``, ``output`` keys.
    :returns: Human-readable inbox line.
    """
    handle_id = payload.get("handle_id", "unknown")
    tool = payload.get("tool_name", "unknown")
    status = payload.get("status", "unknown")
    output = _truncate_inbox_output(payload.get("output", ""))
    # An empty completion (e.g. a native child that idled with no assistant
    # text — the runner delivers "" rather than fabricating from stale
    # history) must read as "produced no output", not a dangling
    # "…returned: " that the parent LLM mistakes for a truncated handoff.
    has_output = bool(output and output.strip())
    if payload.get("type") == "sub_agent":
        agent = payload.get("agent") or payload.get("tool_name", "sub_agent")
        title = payload.get("title", "")
        target = f"{agent}:{title}" if title else str(agent)
        if status == "completed":
            if not has_output:
                return (
                    f"[System: sub-agent task {handle_id} completed — {target} produced no output]"
                )
            return f"[System: sub-agent task {handle_id} completed — {target} returned: {output}]"
        if status == "failed":
            return f"[System: sub-agent task {handle_id} failed — {target} error: {output}]"
        if status == "cancelled":
            return f"[System: sub-agent task {handle_id} cancelled — {target}]"
        return f"[System: sub-agent task {handle_id} {status} — {target}: {output}]"
    if status == "completed":
        if not has_output:
            return f"[System: task {handle_id} completed — {tool} produced no output]"
        return f"[System: task {handle_id} completed — {tool} returned: {output}]"
    if status == "failed":
        return f"[System: task {handle_id} failed — {tool} error: {output}]"
    if status == "cancelled":
        return f"[System: task {handle_id} cancelled]"
    return f"[System: task {handle_id} {status} — {tool}: {output}]"

async def _drain_inbox(
    inbox: asyncio.Queue[dict[str, Any]] | None,
    *,
    server_client: httpx.AsyncClient | None = None,
    conversation_id: str | None = None,
) -> str:
    """
    Non-blocking drain of the per-session inbox queue.

    Returns formatted completion payloads or "Inbox is empty."

    :param inbox: The session's asyncio.Queue, or ``None`` if
        no queue has been created yet.
    :param server_client: HTTP client pointed at Omnigent server.
    :param conversation_id: Parent session id, e.g.
        ``"conv_parent123"``.
    :returns: Formatted string of completed tasks.
    """
    if inbox is None or inbox.empty():
        return "Inbox is empty — no completed tasks."
    items: list[str] = []
    retry_payloads: list[dict[str, Any]] = []
    while not inbox.empty():
        try:
            payload = inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if payload.get("type") == "terminal_idle":
            try:
                items.append(_format_terminal_idle_item(payload))
            except ValueError as exc:
                _logger.warning(
                    "malformed terminal-idle inbox item ignored: %s",
                    exc,
                    exc_info=True,
                )
                items.append(f"[System: malformed terminal_idle inbox item ignored — {exc}]")
            continue
        evaluation = await _evaluate_subagent_inbox_output(
            payload,
            server_client=server_client,
            conversation_id=conversation_id,
        )
        items.append(_format_async_task_item(evaluation.payload))
        if evaluation.retry_original:
            retry_payloads.append(payload)
        else:
            _cleanup_drained_subagent_work(evaluation.payload)
    for payload in retry_payloads:
        inbox.put_nowait(payload)
    return "\n\n".join(items) if items else "Inbox is empty — no completed tasks."

def _spawn_async_tool(
    args: dict[str, Any],
    *,
    session_inbox: asyncio.Queue[dict[str, Any]] | None,
    session_async_tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] | None,
    server_client: httpx.AsyncClient | None,
    terminal_registry: TerminalRegistry | None,
    resource_registry: SessionResourceRegistry | None,
    agent_spec: AgentSpecLike | None,
    conversation_id: str | None,
    task_id: str | None,
    agent_id: str | None,
    agent_name: str | None,
    runner_workspace: Path | None,
    mcp_manager: McpManager | None,
    filesystem_registry: FilesystemRegistry | None = None,
) -> str:
    """
    Spawn a tool as a background asyncio.Task.

    Returns a handle immediately. On completion, the result is
    pushed to the session's inbox queue for ``sys_read_inbox``
    to drain.

    :param args: Must contain ``"tool"`` (target tool name) and
        ``"args"`` (JSON string of target tool arguments).
    :param filesystem_registry: Optional registry forwarded to
        ``execute_tool`` so that OS-env tools invoked via
        ``sys_call_async`` record file changes for the
        ``GET …/changes`` endpoint.
    :param resource_registry: Optional session-resource registry used by
        async terminal-tool launches.
    :returns: JSON handle string with ``handle_id``, ``tool_name``,
        ``status``.
    """
    target_tool = args.get("tool")
    target_args = args.get("args", "{}")
    if not target_tool:
        return 'Error: sys_call_async requires "tool" argument'
    if target_tool == SysCallAsyncTool.name():
        return "Error: sys_call_async cannot dispatch itself"
    if session_inbox is None or session_async_tasks is None:
        return "Error: async inbox not initialized for this session"

    handle_id = f"handle_{uuid.uuid4().hex[:12]}"
    cancel_event = asyncio.Event()

    async def _bg() -> str:
        """
        Background task: dispatch the tool and push result to inbox.

        Uses a cancel_event to bail out immediately when
        sys_cancel_async is called — asyncio.Task.cancel() alone
        can't interrupt asyncio.to_thread (the thread keeps running
        until the subprocess finishes).

        :returns: The tool output string.
        """
        try:
            # Race the tool execution against the cancel event.
            exec_coro = execute_tool(
                tool_name=target_tool,
                arguments=target_args,
                server_client=server_client,
                terminal_registry=terminal_registry,
                resource_registry=resource_registry,
                agent_spec=agent_spec,
                conversation_id=conversation_id,
                task_id=task_id,
                agent_id=agent_id,
                agent_name=agent_name,
                runner_workspace=runner_workspace,
                mcp_manager=mcp_manager,
                session_inbox=session_inbox if target_tool in _TERMINAL_TOOLS else None,
                filesystem_registry=filesystem_registry,
            )
            done, _pending = await asyncio.wait(
                [
                    asyncio.ensure_future(exec_coro),
                    asyncio.ensure_future(cancel_event.wait()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_event.is_set():
                session_inbox.put_nowait(
                    {
                        "handle_id": handle_id,
                        "tool_name": target_tool,
                        "status": "cancelled",
                        "output": "",
                    }
                )
                return ""
            result = next(iter(done)).result()
            session_inbox.put_nowait(
                {
                    "handle_id": handle_id,
                    "tool_name": target_tool,
                    "status": "completed",
                    "output": result,
                }
            )
            return result
        except asyncio.CancelledError:
            session_inbox.put_nowait(
                {
                    "handle_id": handle_id,
                    "tool_name": target_tool,
                    "status": "cancelled",
                    "output": "",
                }
            )
            raise
        except Exception as exc:  # noqa: BLE001
            session_inbox.put_nowait(
                {
                    "handle_id": handle_id,
                    "tool_name": target_tool,
                    "status": "failed",
                    "output": str(exc),
                }
            )
            return f"Error: {exc}"
        finally:
            session_async_tasks.pop(handle_id, None)

    bg_task = asyncio.create_task(_bg(), name=f"async-{handle_id}")
    session_async_tasks[handle_id] = (bg_task, cancel_event)

    return json.dumps(
        {
            "handle_id": handle_id,
            "tool_name": target_tool,
            "status": "in_progress",
            "message": (
                f"[System: {target_tool} dispatched as background "
                f"task {handle_id}. Result will appear in your "
                f"inbox — call sys_read_inbox to check.]"
            ),
        }
    )

def _cancel_async_tool_result(
    args: dict[str, Any],
    *,
    session_async_tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] | None,
) -> _CancelAsyncToolResult:
    """
    Cancel an in-flight local async tool by handle id.

    Signals the cancel_event so the background task's
    ``asyncio.wait`` returns immediately — the underlying
    thread may keep running but the task won't block on it.

    :param args: Must contain ``"handle_id"`` (``"task_id"`` is
        accepted as a legacy alias).
    :returns: Structured local-cancel result. ``try_subagent_cancel``
        is true only when no local async task matched.
    """
    handle_id = args.get("handle_id") or args.get("task_id")
    if not handle_id:
        return _CancelAsyncToolResult('Error: sys_cancel_async requires "handle_id"')
    if session_async_tasks is None:
        return _CancelAsyncToolResult("Error: async inbox not initialized for this session")
    entry = session_async_tasks.get(handle_id)
    if entry is None:
        return _CancelAsyncToolResult(
            f"Error: no in-flight task with handle_id {handle_id}",
            try_subagent_cancel=True,
        )
    _task, cancel_event = entry
    # Signal the event — _bg's asyncio.wait returns immediately.
    # Don't call task.cancel(): the CancelledError races with
    # the event check and can prevent the inbox push.
    cancel_event.set()
    return _CancelAsyncToolResult(json.dumps({"cancelled": True, "handle_id": handle_id}))

def _cancel_async_tool(
    args: dict[str, Any],
    *,
    session_async_tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] | None,
) -> str:
    """
    Cancel an in-flight async tool by handle_id.

    :param args: Must contain ``"handle_id"`` (``"task_id"`` is
        accepted as a legacy alias).
    :param session_async_tasks: Per-session async task map, or
        ``None`` when async inbox state is unavailable.
    :returns: Confirmation or error string.
    """
    return _cancel_async_tool_result(
        args,
        session_async_tasks=session_async_tasks,
    ).output

async def _execute_task_lifecycle_tool(
    args: dict[str, Any],
    *,
    session_async_tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] | None,
    conversation_id: str | None,
    server_client: httpx.AsyncClient | None,
) -> str:
    """
    Runner-local handler for ``sys_cancel_task``.

    The generic cancel path first tries the in-memory async dispatches
    tracked in ``session_async_tasks``. If no async tool handle matches,
    it falls through to the sub-agent work registry so handles returned
    by ``sys_session_send`` can be cancelled by task id.

    :param args: Parsed JSON arguments from the LLM.
    :param session_async_tasks: Per-session async task map
        from ``create_runner_app``.
    :param conversation_id: Parent session id, e.g.
        ``"conv_parent123"``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: JSON-encoded result string.
    """
    async_result = _cancel_async_tool_result(
        args,
        session_async_tasks=session_async_tasks,
    )
    if not async_result.try_subagent_cancel:
        return async_result.output
    return await _cancel_subagent_task(
        args,
        conversation_id=conversation_id,
        server_client=server_client,
    )
