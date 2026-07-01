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

async def _execute_terminal_tool(
    tool_name: str,
    args: dict[str, Any],
    *,
    terminal_registry: TerminalRegistry | None,
    resource_registry: SessionResourceRegistry | None = None,
    agent_spec: AgentSpecLike | None,
    conversation_id: str | None,
    task_id: str | None,
    agent_id: str | None,
    runner_workspace: Path | None = None,
    session_inbox: asyncio.Queue[dict[str, Any]] | None = None,
    publish_event: Callable[[str, dict[str, Any]], None] | None = None,
    acting_identity: ActingIdentity | None = None,
) -> str:
    """Execute a terminal tool using the runner's TerminalRegistry.

    :param runner_workspace: Optional CLI launch workspace passed
        into ``ToolContext.workspace`` for terminal cwd resolution.
    :param session_inbox: Per-session queue drained by
        ``sys_read_inbox``. Accepted at the dispatcher boundary but
        no longer threaded into the launch tool — kept for callers
        that still pass it.
    :param publish_event: Per-session SSE emitter (the runner's
        ``_publish_event``). When set, a fresh ``sys_terminal_launch``
        emits ``session.resource.created`` and a successful
        ``sys_terminal_close`` emits ``session.resource.deleted`` so
        the web rail updates mid-turn instead of waiting for the
        response-end terminals-cache invalidation. ``None`` for
        in-process callers / tests that don't relay.
    :param resource_registry: Optional session-resource registry used to
        observe fresh launches as auxiliary terminal resources.
    """
    import asyncio

    if terminal_registry is None:
        return "Error: terminal_registry not available in runner"
    if agent_spec is None:
        return "Error: agent_spec not available for terminal dispatch"
    if conversation_id is None:
        return "Error: conversation_id required for terminal tools"

    from omnigent.tools.base import ToolContext

    ctx = ToolContext(
        task_id=task_id or "unknown",
        agent_id=agent_id or "unknown",
        workspace=runner_workspace,
        conversation_id=conversation_id,
        acting_identity=acting_identity,
    )

    del session_inbox
    if tool_name == SysTerminalLaunchTool.name():
        tool_instance: Any = SysTerminalLaunchTool(
            # Nominal AgentSpec required; carried value is structurally
            # AgentSpecLike but always a real AgentSpec at runtime.
            spec=cast("AgentSpec", agent_spec),
            registry=terminal_registry,
        )
    elif tool_name == SysTerminalSendTool.name():
        tool_instance = SysTerminalSendTool(registry=terminal_registry)
    elif tool_name == SysTerminalReadTool.name():
        tool_instance = SysTerminalReadTool(registry=terminal_registry)
    elif tool_name == SysTerminalListTool.name():
        tool_instance = SysTerminalListTool(registry=terminal_registry)
    elif tool_name == SysTerminalCloseTool.name():
        tool_instance = SysTerminalCloseTool(registry=terminal_registry)
    else:
        return f"Error: unknown terminal tool {tool_name}"

    arguments_str = json.dumps(args)

    # Terminal tools use blocking tmux APIs; bridge via to_thread.
    try:
        output = await asyncio.to_thread(tool_instance.invoke, arguments_str, ctx)
    except Exception as exc:  # noqa: BLE001
        return f"Error: {tool_name} failed: {type(exc).__name__}: {exc}"

    # Surface the resource lifecycle on the live SSE stream. The
    # tool ran in the runner process, where ``session_stream`` (the
    # AP-server pub-sub the web UI subscribes to) has no subscribers;
    # ``publish_event`` is the runner's own per-session queue, which
    # the Omnigent server's relay republishes onto ``session_stream``.
    if publish_event is not None and tool_name in (
        SysTerminalLaunchTool.name(),
        SysTerminalCloseTool.name(),
    ):
        await _emit_terminal_resource_event(
            tool_name=tool_name,
            output=output,
            args=args,
            conversation_id=conversation_id,
            terminal_registry=terminal_registry,
            resource_registry=resource_registry,
            publish_event=publish_event,
        )
    return output

async def _emit_terminal_resource_event(
    *,
    tool_name: str,
    output: str,
    args: dict[str, Any],
    conversation_id: str,
    terminal_registry: TerminalRegistry,
    resource_registry: SessionResourceRegistry | None,
    publish_event: Callable[[str, dict[str, Any]], None],
) -> None:
    """Emit a ``session.resource.{created,deleted}`` event for a terminal tool.

    Parses the terminal tool's JSON envelope and pushes a matching
    SSE event onto ``publish_event`` so live subscribers (the web
    rail) see tool-launched / tool-closed terminals immediately. The
    event shapes match the REST resource path
    (:func:`omnigent.server.routes.sessions._publish_and_persist_resource_event`)
    so the AP-server relay and the web UI handle both surfaces
    identically.

    Best-effort: a malformed / error envelope, an unexpected status,
    or a registry miss is a silent no-op — the snapshot endpoint
    (``GET /resources/terminals``) plus the response-end cache
    invalidation remain the source of truth for reconnecting clients.

    :param tool_name: The terminal tool name, e.g.
        ``"sys_terminal_launch"`` or ``"sys_terminal_close"``.
    :param output: The tool's JSON-encoded result envelope, e.g.
        ``{"terminal": "bash", "session": "s1", "status": "launched"}``.
    :param args: Parsed launch / close arguments — fallback source
        for ``terminal`` / ``session`` if the envelope omits them.
    :param conversation_id: Owning conversation id, e.g.
        ``"conv_abc123"``.
    :param terminal_registry: The runner's ``TerminalRegistry``,
        used to look up the live instance for a fresh launch.
    :param resource_registry: Optional session-resource registry used to
        observe fresh launches as auxiliary terminal resources.
    :param publish_event: The runner's per-session SSE emitter.
    """
    try:
        envelope = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(envelope, dict):
        return
    terminal_name = envelope.get("terminal") or args.get("terminal")
    session_key = envelope.get("session") or args.get("session")
    if not isinstance(terminal_name, str) or not isinstance(session_key, str):
        return

    status = envelope.get("status")
    if tool_name == SysTerminalLaunchTool.name() and status == "launched":
        await _publish_terminal_created_event(
            conversation_id=conversation_id,
            terminal_name=terminal_name,
            session_key=session_key,
            terminal_registry=terminal_registry,
            resource_registry=resource_registry,
            publish_event=publish_event,
        )
    elif tool_name == SysTerminalCloseTool.name() and status == "closed":
        _publish_terminal_deleted_event(
            conversation_id=conversation_id,
            terminal_name=terminal_name,
            session_key=session_key,
            publish_event=publish_event,
        )

async def _publish_terminal_created_event(
    *,
    conversation_id: str,
    terminal_name: str,
    session_key: str,
    terminal_registry: TerminalRegistry,
    resource_registry: SessionResourceRegistry | None,
    publish_event: Callable[[str, dict[str, Any]], None],
) -> None:
    """Build and publish ``session.resource.created`` for a fresh launch.

    Looks up the live :class:`TerminalInstance` from the registry and
    projects it through :func:`terminal_resource_view` so the wire
    shape exactly matches the REST resource path. A registry miss
    (the instance vanished between launch and lookup) is a silent
    no-op.

    :param conversation_id: Owning conversation id, e.g.
        ``"conv_abc123"``.
    :param terminal_name: Terminal spec name, e.g. ``"bash"``.
    :param session_key: Per-launch session key, e.g. ``"s1"``.
    :param terminal_registry: The runner's ``TerminalRegistry``.
    :param resource_registry: Optional session-resource registry used to
        observe the launched terminal as auxiliary.
    :param publish_event: The runner's per-session SSE emitter.
    """
    from omnigent.entities.session_resources import session_resource_view_to_dict

    instance = terminal_registry.get(conversation_id, terminal_name, session_key)
    if instance is None:
        return
    if resource_registry is not None:
        try:
            view = await resource_registry.observe_auxiliary_terminal(
                conversation_id,
                terminal_name,
                session_key,
                instance,
            )
        except Exception:
            _logger.exception(
                "Failed to observe tool-launched terminal: session=%s terminal=%s:%s",
                conversation_id,
                terminal_name,
                session_key,
            )
            return
        resource = session_resource_view_to_dict(view)
    else:
        from omnigent.entities.session_resources import terminal_resource_view
        from omnigent.terminals.registry import TerminalListEntry

        entry = TerminalListEntry(
            terminal_name=terminal_name,
            session_key=session_key,
            instance=instance,
        )
        resource = session_resource_view_to_dict(terminal_resource_view(conversation_id, entry))
    publish_event(
        conversation_id,
        {"type": "session.resource.created", "resource": resource},
    )

    # Legacy fallback for callers that do not have a SessionResourceRegistry:
    # start the runner-side pane-activity watcher here so the web "active"
    # badge still works. Normal runner dispatch uses observe_auxiliary_terminal
    # above, which owns the watcher and terminal-exit lifecycle semantics.
    if resource_registry is not None:
        return
    resource_id = resource["id"]
    if isinstance(resource_id, str) and resource_id:
        loop = asyncio.get_running_loop()

        def _on_activity() -> None:
            loop.call_soon_threadsafe(
                publish_event,
                conversation_id,
                {
                    "type": "session.terminal.activity",
                    "session_id": conversation_id,
                    "terminal_id": resource_id,
                },
            )

        instance.start_idle_watcher_thread(on_activity=_on_activity)

def _publish_terminal_deleted_event(
    *,
    conversation_id: str,
    terminal_name: str,
    session_key: str,
    publish_event: Callable[[str, dict[str, Any]], None],
) -> None:
    """Build and publish ``session.resource.deleted`` for a closed terminal.

    The delete event carries only the deterministic resource id (no
    instance lookup needed), matching the shape the REST resource
    path emits via ``_publish_and_persist_resource_event``.

    :param conversation_id: Owning conversation id, e.g.
        ``"conv_abc123"``.
    :param terminal_name: Terminal spec name, e.g. ``"bash"``.
    :param session_key: Per-launch session key, e.g. ``"s1"``.
    :param publish_event: The runner's per-session SSE emitter.
    """
    from omnigent.entities.session_resources import terminal_resource_id

    publish_event(
        conversation_id,
        {
            "type": "session.resource.deleted",
            "resource_id": terminal_resource_id(terminal_name, session_key),
            "resource_type": "terminal",
            "session_id": conversation_id,
        },
    )

def _format_terminal_idle_item(
    payload: dict[str, Any],
) -> str:
    """
    Render a terminal-idle inbox item for ``sys_read_inbox``.

    :param payload: Canonical terminal-idle inbox payload.
    :returns: Human-readable inbox line.
    :raises ValueError: If the payload is missing required fields or
        top-level and content identities disagree.
    """
    payload_type = payload.get("type")
    source = payload.get("source")
    session = payload.get("session")
    content = payload.get("content")
    if payload_type != "terminal_idle":
        raise ValueError("terminal-idle inbox payload must have type 'terminal_idle'")
    if not isinstance(source, str) or not source:
        raise ValueError("terminal-idle inbox payload requires non-empty string source")
    if not isinstance(session, str) or not session:
        raise ValueError("terminal-idle inbox payload requires non-empty string session")
    if not isinstance(content, dict):
        raise ValueError("terminal-idle inbox payload requires object content")
    if content.get("status") != "idle":
        raise ValueError("terminal-idle inbox payload content.status must be 'idle'")
    if content.get("terminal") != source or content.get("session") != session:
        raise ValueError(
            "terminal-idle inbox payload content terminal/session must match source/session"
        )
    return f"[System: inbox item terminal_idle — terminal {source}:{session} is idle]"

