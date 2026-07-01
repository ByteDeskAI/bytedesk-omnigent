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

from ._os_env import _effective_runner_os_env_spec

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

async def _execute_rest_tool(
    tool_name: str,
    args: dict[str, Any],
    server_client: httpx.AsyncClient | None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
) -> str:
    """Execute a REST-backed tool by calling server APIs.

    Uses the ``/v1/sessions`` API: creates a child session,
    posts a message event to kick off the turn, and returns the
    session_id as the handle. Cancellation sends an interrupt
    event to the child session.

    :param tool_name: The tool to execute, e.g.
        ``"sys_call_async"``.
    :param args: Tool arguments from the LLM.
    :param server_client: httpx client pointed at the Omnigent server.
    :param agent_id: Durable agent id, e.g. ``"ag_abc123"``.
        Required from the session context.
    :param conversation_id: Parent conversation id, e.g.
        ``"conv_abc123"``. Used to look up the runner binding
        on the parent session so the child session can be bound
        to the same runner.
    :returns: JSON result string for the LLM.
    """
    if server_client is None:
        return f"Error: {tool_name} requires server access"

    if tool_name == SysCallAsyncTool.name():
        # agent_id must be provided by the session context.
        resolved_agent_id = agent_id
        if resolved_agent_id is None:
            return "Error: sys_call_async requires agent_id from the session context"

        input_items = args.get("input") or [{"role": "user", "content": args.get("prompt", "")}]
        try:
            # Create a child session bound to the same agent.
            create_resp = await server_client.post(
                "/v1/sessions",
                json={"agent_id": resolved_agent_id},
                timeout=30.0,
            )
            if create_resp.status_code not in (200, 201):
                return (
                    f"Error: sys_call_async session create returned "
                    f"{create_resp.status_code}: {create_resp.text[:200]}"
                )
            session_id = create_resp.json()["id"]

            # Bind to the parent's runner so event forwarding works.
            if conversation_id is not None:
                try:
                    parent_resp = await server_client.get(
                        f"/v1/sessions/{conversation_id}",
                        timeout=10.0,
                    )
                    if parent_resp.status_code == 200:
                        parent_runner = parent_resp.json().get("runner_id")
                        if parent_runner:
                            await server_client.patch(
                                f"/v1/sessions/{session_id}",
                                json={"runner_id": parent_runner},
                                timeout=10.0,
                            )
                except httpx.HTTPError:
                    _logger.debug(
                        "sys_call_async: failed to bind runner for child session %s",
                        session_id,
                        exc_info=True,
                    )

            # Post the message event to start the turn.
            content = input_items
            if isinstance(content, str):
                content = [{"type": "input_text", "text": content}]
            event_body: dict[str, Any] = {
                "type": "message",
                "data": {
                    "role": "user",
                    "content": content,
                },
            }
            event_resp = await server_client.post(
                f"/v1/sessions/{session_id}/events",
                json=event_body,
                timeout=30.0,
            )
            if event_resp.status_code >= 400:
                return (
                    f"Error: sys_call_async event post returned "
                    f"{event_resp.status_code}: {event_resp.text[:200]}"
                )
            # Return session_id as the handle (replaces task_id).
            return json.dumps({"task_id": session_id, "status": "running"})
        except Exception as exc:  # noqa: BLE001
            return f"Error: sys_call_async failed: {exc}"

    if tool_name == SysCancelAsyncTool.name():
        # task_id from sys_call_async is now a session_id.
        task_id = args.get("task_id", "")
        try:
            resp = await server_client.post(
                f"/v1/sessions/{task_id}/events",
                json={"type": "interrupt", "data": {}},
                timeout=30.0,
            )
            if resp.status_code in (200, 201, 202):
                return f"Cancelled task {task_id}"
            return f"Error: sys_cancel_async returned {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return f"Error: sys_cancel_async failed: {exc}"

    return f"Error: {tool_name} not implemented in REST dispatch"

async def _execute_file_tool(
    tool_name: str,
    args: dict[str, Any],
    server_client: httpx.AsyncClient | None,
    *,
    conversation_id: str | None,
    agent_spec: AgentSpecLike | None = None,
    runner_workspace: Path | None = None,
) -> str:
    """
    Execute a file tool by calling session-scoped server file APIs.

    :param tool_name: File tool name, e.g. ``"upload_file"``.
    :param args: Parsed tool arguments.
    :param server_client: HTTP client for the Omnigent server.
    :param conversation_id: Owning session/conversation id,
        e.g. ``"conv_abc123"``.
    :param agent_spec: Agent spec resolved for the current turn, used
        (with ``runner_workspace``) to derive the workspace root that
        an ``upload_file`` path is resolved against. ``None`` falls back
        to the per-conversation default workspace.
    :param runner_workspace: Authoritative runtime cwd for the runner,
        sourced from ``OMNIGENT_RUNNER_WORKSPACE``. Combined with
        ``agent_spec`` to compute the workspace containment boundary
        for ``upload_file``.
    :returns: Tool result string.
    """
    if server_client is None:
        return f"Error: {tool_name} requires server access"
    if conversation_id is None:
        return f"Error: {tool_name} requires a session id"
    files_path = f"/v1/sessions/{conversation_id}/resources/files"

    if tool_name == UploadFileTool.name():
        path = args.get("path")
        if not path:
            return "Error: sys_upload_file failed: empty path"
        # Resolve the agent-supplied path against the session workspace
        # (the same cwd the sys_os_* tools operate in) and reject any
        # path that escapes it. The read happens in the un-sandboxed
        # runner process, so without this containment an agent could
        # exfiltrate arbitrary host files. Mirrors the
        # builtin UploadFileTool's safe_resolve / sys_agent_download
        # containment checks.
        workspace = Path(
            _effective_runner_os_env_spec(agent_spec, conversation_id, runner_workspace).cwd
        )
        try:
            resolved = safe_resolve(path, workspace)
        except ValueError as exc:
            return f"Error: sys_upload_file failed: {exc}"
        filename = resolved.name
        try:
            with open(resolved, "rb") as f:
                content = f.read()
            resp = await server_client.post(
                files_path,
                files={"file": (filename, content)},
                timeout=60.0,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                return json.dumps({"file_id": data.get("id"), "filename": filename})
            return f"Error: upload returned {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return f"Error: sys_upload_file failed: {exc}"

    if tool_name == DownloadFileTool.name():
        file_id = args.get("file_id", "")
        try:
            resp = await server_client.get(
                f"{files_path}/{file_id}/content",
                timeout=30.0,
            )
            if resp.status_code == 200:
                return resp.text
            return f"Error: download returned {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return f"Error: {DownloadFileTool.name()} failed: {exc}"

    if tool_name == "list_files":
        try:
            resp = await server_client.get(files_path, timeout=30.0)
            if resp.status_code == 200:
                return json.dumps(resp.json())
            return f"Error: list_files returned {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return f"Error: list_files failed: {exc}"

    return f"Error: {tool_name} not implemented in file dispatch"
