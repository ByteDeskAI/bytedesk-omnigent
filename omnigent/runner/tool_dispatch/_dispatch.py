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
from omnigent.runner.subagent_status import (
    _ACTIVE as _SUBAGENT_ACTIVE_STATUSES,
)
from omnigent.runner.subagent_status import (
    SubagentWorkStatus,
)
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

def _build_tool_execution_context(
    *,
    tool_name: str,
    arguments: str,
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
    session_inbox: asyncio.Queue[dict[str, Any]] | None,
    session_async_tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] | None,
    harness_client: httpx.AsyncClient | None,
    publish_event: Callable[[str, dict[str, Any]], None] | None,
    filesystem_registry: FilesystemRegistry | None,
    acting_identity: ActingIdentity | None,
) -> ToolExecutionContext:
    """Bundle ``execute_tool``'s per-dispatch args into a context.

    The mutable coordination objects (``session_inbox``,
    ``session_async_tasks``) are passed straight through, so the context
    holds the SAME queue/map the caller still holds — never a copy. This
    is the only place the spine Phase 4 carrier is constructed.

    :returns: A :class:`ToolExecutionContext` mirroring the args.
    """
    return ToolExecutionContext(
        tool_name=tool_name,
        arguments=arguments,
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
        session_inbox=session_inbox,
        session_async_tasks=session_async_tasks,
        harness_client=harness_client,
        publish_event=publish_event,
        filesystem_registry=filesystem_registry,
        acting_identity=acting_identity,
    )

async def execute_tool(
    *,
    tool_name: str,
    arguments: str,
    server_client: httpx.AsyncClient | None = None,
    terminal_registry: TerminalRegistry | None = None,
    resource_registry: SessionResourceRegistry | None = None,
    agent_spec: AgentSpecLike | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    agent_name: str | None = None,
    runner_workspace: Path | None = None,
    mcp_manager: McpManager | None = None,
    session_inbox: asyncio.Queue[dict[str, Any]] | None = None,
    session_async_tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] | None = None,
    harness_client: httpx.AsyncClient | None = None,
    publish_event: Callable[[str, dict[str, Any]], None] | None = None,
    filesystem_registry: FilesystemRegistry | None = None,
    acting_identity: ActingIdentity | None = None,
) -> str:
    """
    Execute a tool and return the output string.

    Pure execution — does NOT post the result to the harness.
    Used by ``dispatch_tool_locally`` (which adds the harness
    POST) and by ``_spawn_async_tool`` background tasks (which
    push to the inbox queue instead).

    The args are bundled into a :class:`ToolExecutionContext` and dispatched
    through the canonical dispatcher registry. The mutable ``session_inbox`` /
    ``session_async_tasks`` are carried by reference.

    :param tool_name: Tool to execute, e.g. ``"sys_os_shell"``.
    :param arguments: JSON-encoded arguments string.
    :param publish_event: Callback that puts an SSE event on the
        runner's per-session outbound queue. ``None`` from
        dispatch sites that don't need event emission (e.g.
        async background tools).
    :param resource_registry: Optional session-resource registry used to
        observe tool-launched terminals through the same lifecycle path as
        runner-launched terminals.
    :param filesystem_registry: Optional registry for tracking agent
        file modifications. Forwarded to ``_execute_os_env_tool``
        so that ``sys_os_write`` and ``sys_os_edit`` calls record changed
        paths for the ``GET …/changes`` endpoint. ``sys_os_shell`` is
        not tracked — shell side-effects cannot be attributed to a session.
    :param acting_identity: Optional caller identity propagated to local,
        builtin, terminal, and skill tool contexts.
    :returns: Tool output string.
    """
    from omnigent.runner.tool_dispatcher_registry import dispatch_via_registry

    return await dispatch_via_registry(
        _build_tool_execution_context(
            tool_name=tool_name,
            arguments=arguments,
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
            session_inbox=session_inbox,
            session_async_tasks=session_async_tasks,
            harness_client=harness_client,
            publish_event=publish_event,
            filesystem_registry=filesystem_registry,
            acting_identity=acting_identity,
        )
    )

def _maybe_signal_changed_files(
    conversation_id: str | None,
    publish_event: Callable[[str, dict[str, Any]], None] | None,
    *,
    now: float,
) -> None:
    """Publish a throttled ``session.changed_files.invalidated`` event.

    Tells the web to refetch the changed-files list (a coarse "something
    changed" signal — per-file events aren't available for git-mode
    workspaces). Leading-edge throttle keyed by session collapses a
    multi-file turn to roughly one refetch trigger.

    :param conversation_id: Session id, or ``None`` (no-op).
    :param publish_event: Per-session SSE emitter, or ``None`` (no-op).
    :param now: Monotonic timestamp, e.g. ``loop.time()``.
    """
    if conversation_id is None or publish_event is None:
        return
    last = _changed_files_last_signal.get(conversation_id, 0.0)
    if now - last < _CHANGED_FILES_SIGNAL_THROTTLE_S:
        return
    if len(_changed_files_last_signal) > _CHANGED_FILES_SIGNAL_MAX_TRACKED:
        _changed_files_last_signal.clear()
    _changed_files_last_signal[conversation_id] = now
    publish_event(
        conversation_id,
        {
            "type": "session.changed_files.invalidated",
            "session_id": conversation_id,
            "environment_id": "default",
        },
    )

async def dispatch_tool_locally(
    *,
    tool_name: str,
    call_id: str,
    arguments: str,
    response_id: str,
    harness_client: httpx.AsyncClient,
    server_client: httpx.AsyncClient | None = None,
    terminal_registry: TerminalRegistry | None = None,
    resource_registry: SessionResourceRegistry | None = None,
    agent_spec: AgentSpecLike | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    agent_name: str | None = None,
    runner_workspace: Path | None = None,
    mcp_manager: McpManager | None = None,
    session_inbox: asyncio.Queue[dict[str, Any]] | None = None,
    session_async_tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] | None = None,
    publish_event: Callable[[str, dict[str, Any]], None] | None = None,
    filesystem_registry: FilesystemRegistry | None = None,
) -> str:
    """Execute a tool locally and PATCH the result to the harness.

    :param runner_workspace: Optional CLI launch workspace used to
        resolve placeholder cwd values for runner-owned filesystem
        tools.
    :param mcp_manager: When set, dispatch via
        :meth:`RunnerMcpManager.call_tool`. Caller (proxy_stream)
        passes this only for MCP-owned tools.
    :param session_inbox: Per-session asyncio queue for async tool
        completions. ``sys_call_async`` pushes results here;
        ``sys_read_inbox`` drains it.
    :param session_async_tasks: Per-session dict of handle_id →
        ``(Task, cancel_event)`` tuple. Used by ``sys_cancel_async``
        to signal cancellation via the event.
    :param filesystem_registry: Optional registry for tracking agent
        file modifications. Forwarded to ``execute_tool`` so that
        ``sys_os_write`` and ``sys_os_edit`` calls record changed paths
        for the ``GET …/changes`` endpoint.
    :param resource_registry: Optional session-resource registry used to
        observe tool-launched terminals.
    :returns: The tool output string.
    """
    output = await execute_tool(
        tool_name=tool_name,
        arguments=arguments,
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
        session_inbox=session_inbox,
        session_async_tasks=session_async_tasks,
        harness_client=harness_client,
        filesystem_registry=filesystem_registry,
        publish_event=publish_event,
    )

    # A file-mutating tool just ran — nudge the web to refetch the
    # changed-files list (throttled, coalesced client-side).
    if tool_name in _CHANGED_FILES_TOOLS:
        _maybe_signal_changed_files(
            conversation_id,
            publish_event,
            now=asyncio.get_running_loop().time(),
        )

    # POST the result back to the harness as a ``tool_result``
    # event on the session-keyed events endpoint. ``conversation_id``
    # is required: the harness validates the URL segment against
    # its own runner-stamped value and fails 404 on mismatch —
    # without an id we'd be unable to form a valid URL. Fail loud
    # per ``designs/DESIGN_PRINCIPLES.md`` rather than substituting
    # a synthetic default. ``response_id`` is unused at the URL /
    # body level (the harness has at most one in-flight turn so the
    # ``call_id`` alone keys the parked Future) — kept on the
    # function signature for symmetry with callers that track it.
    del response_id  # see comment above — intentionally unused
    if not conversation_id:
        raise ValueError(
            "dispatch_tool_locally requires conversation_id to POST the "
            "harness session-keyed URL; got None/empty"
        )
    try:
        resp = await harness_client.post(
            f"/v1/sessions/{conversation_id}/events",
            json={"type": "tool_result", "call_id": call_id, "output": output},
            timeout=30.0,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "Runner local dispatch tool_result event failed for %s (call_id=%s): %s",
            tool_name,
            call_id,
            exc,
        )

    return output

