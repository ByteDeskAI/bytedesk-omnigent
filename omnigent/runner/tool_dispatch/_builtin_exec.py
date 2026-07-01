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

async def _execute_spec_builtin_tool(
    tool_name: str,
    args: str,
    *,
    agent_spec: AgentSpecLike | None,
    conversation_id: str | None,
    task_id: str | None,
    agent_id: str | None,
    runner_workspace: Path | None,
    acting_identity: ActingIdentity | None = None,
) -> str:
    if agent_spec is None:
        return f"Error: {tool_name} not in local dispatch table (no agent spec)"
    try:
        manager = ToolManager(cast("AgentSpec", agent_spec), workdir=runner_workspace)
        workspace = None
        if runner_workspace is not None and conversation_id is not None:
            workspace = runner_workspace / conversation_id
            workspace.mkdir(parents=True, exist_ok=True)
        ctx = ToolContext(
            task_id=task_id or conversation_id or "runner-builtin-tool",
            agent_id=agent_id or getattr(agent_spec, "name", "runner-agent") or "runner-agent",
            workspace=workspace,
            conversation_id=conversation_id,
            acting_identity=acting_identity,
        )
        return await asyncio.to_thread(manager.call_tool, tool_name, args, ctx)
    except Exception as exc:
        _logger.exception("runner spec builtin dispatch failed for %s", tool_name)
        return f"Error: {type(exc).__name__}: {exc}"

async def _execute_local_python_tool(
    tool_name: str,
    args: str,
    *,
    agent_spec: AgentSpecLike | None,
    conversation_id: str | None,
    task_id: str | None,
    agent_id: str | None,
    runner_workspace: Path | None,
    acting_identity: ActingIdentity | None = None,
) -> str:
    if agent_spec is None:
        return f"Error: {tool_name} not in local dispatch table (no agent spec)"
    try:
        # ``ToolManager`` requires the nominal ``AgentSpec``; the carried value
        # is structurally ``AgentSpecLike`` but is always a real ``AgentSpec``
        # at runtime. Cast (string forward-ref → no runtime import) to bridge
        # the structural→nominal gap without re-pinning the carried type.
        manager = ToolManager(cast("AgentSpec", agent_spec), workdir=runner_workspace)
        workspace = None
        if runner_workspace is not None and conversation_id is not None:
            workspace = runner_workspace / conversation_id
            workspace.mkdir(parents=True, exist_ok=True)
        ctx = ToolContext(
            task_id=task_id or conversation_id or "runner-local-tool",
            agent_id=agent_id or getattr(agent_spec, "name", "runner-agent") or "runner-agent",
            workspace=workspace,
            conversation_id=conversation_id,
            acting_identity=acting_identity,
        )
        return await asyncio.to_thread(manager.call_tool, tool_name, args, ctx)
    except Exception as exc:
        _logger.exception("runner local Python tool dispatch failed for %s", tool_name)
        return f"Error: {type(exc).__name__}: {exc}"

def _resolve_spec_callable(
    tool_name: str,
    agent_spec: AgentSpecLike | None,
) -> Callable[..., Any] | str:
    """
    Look up a custom callable tool in the agent spec and resolve it.

    Returns the callable on success, or an error string on failure.
    Caches resolved callables in :data:`_callable_cache` so
    repeated invocations of the same tool skip the import.

    :param tool_name: Tool name from the LLM, e.g. ``"echo"``.
    :param agent_spec: The session's :class:`AgentSpec`. ``None``
        when no spec is available.
    :returns: The resolved callable, or an error string if the
        tool is not found or the import fails.
    """
    import importlib

    if agent_spec is None:
        return f"Error: {tool_name} not in local dispatch table (no agent spec)"
    local_tools = getattr(agent_spec, "local_tools", None) or []
    tool_info = next((lt for lt in local_tools if lt.name == tool_name), None)
    if tool_info is None or not tool_info.path:
        return f"Error: {tool_name} not in local dispatch table"
    dotted_path = tool_info.path
    cached = _callable_cache.get(dotted_path)
    if cached is not None:
        return cached
    module_name, _, attr_name = dotted_path.rpartition(".")
    if not module_name or not attr_name:
        return f"Error: {tool_name} has invalid callable path {dotted_path!r}"
    mod = importlib.import_module(module_name)
    fn = getattr(mod, attr_name, None)
    if fn is None:
        return f"Error: {tool_name}: module {module_name!r} has no attribute {attr_name!r}"
    _callable_cache[dotted_path] = fn
    return fn

async def _execute_spec_callable_tool(
    tool_name: str,
    args: dict[str, Any],
    *,
    agent_spec: AgentSpecLike | None = None,
) -> str:
    """
    Execute a custom callable tool defined in the agent spec YAML.

    Resolves the dotted Python path via :func:`_resolve_spec_callable`,
    then calls the function with the LLM's arguments as kwargs.
    Sync callables run in a worker thread via ``asyncio.to_thread``
    to avoid blocking the event loop.

    :param tool_name: Tool name from the LLM, e.g. ``"echo"``.
    :param args: Parsed argument dict from the LLM.
    :param agent_spec: The session's :class:`AgentSpec`. ``None``
        when no spec is available (returns an error string).
    :returns: Tool output as a string, or an error message.
    """
    resolved = _resolve_spec_callable(tool_name, agent_spec)
    if isinstance(resolved, str):
        return resolved
    if asyncio.iscoroutinefunction(resolved):
        result = await resolved(**args)
    else:
        result = await asyncio.to_thread(resolved, **args)
    return str(result) if result is not None else ""

def _resolve_uc_profile(agent_spec: AgentSpecLike | None) -> str | None:
    """
    Extract the Databricks profile from the agent spec's executor
    auth configuration.

    Checks ``executor.auth`` (preferred) then falls back to
    ``executor.profile`` (deprecated) and finally
    ``executor.config["profile"]`` (compat bridge).

    :param agent_spec: The session's :class:`AgentSpec`.
    :returns: The profile name, e.g. ``"oss"``, or ``None`` for
        SDK default resolution.
    """
    executor = getattr(agent_spec, "executor", None)
    if executor is None:
        return None
    # Preferred: executor.auth.profile (DatabricksAuth).
    auth = getattr(executor, "auth", None)
    if auth is not None and hasattr(auth, "profile"):
        return auth.profile
    # Deprecated: executor.profile.
    profile = getattr(executor, "profile", None)
    if profile:
        return profile
    # Compat bridge: executor.config["profile"].
    config = getattr(executor, "config", None) or {}
    return config.get("profile")

async def _execute_uc_function_tool(
    tool_name: str,
    args: dict[str, Any],
    *,
    agent_spec: AgentSpecLike | None = None,
) -> str:
    """
    Execute a Unity Catalog function tool and return the output
    string.

    Resolves the ``catalog_path`` from the spec's ``local_tools``,
    extracts the Databricks profile and warehouse ID from the
    executor config, then delegates to
    :func:`omnigent.runner.uc_function.execute_uc_function`.

    :param tool_name: Tool name from the LLM, e.g.
        ``"classify_text"``.
    :param args: Parsed argument dict from the LLM.
    :param agent_spec: The session's :class:`AgentSpec`. Must not
        be ``None`` (caller checks via :func:`_is_uc_function_tool`
        first).
    :returns: Tool output as a string, or an error message.
    """
    from omnigent.runner.uc_function import execute_uc_function

    local_tools = getattr(agent_spec, "local_tools", None) or []
    tool_info = next((lt for lt in local_tools if lt.name == tool_name), None)
    if tool_info is None or tool_info.catalog_path is None:
        return f"Error: {tool_name} is not a UC function tool"

    profile = _resolve_uc_profile(agent_spec)
    warehouse_id = getattr(tool_info, "warehouse_id", None)

    return await execute_uc_function(
        catalog_path=tool_info.catalog_path,
        args=args,
        profile=profile,
        warehouse_id=warehouse_id,
    )

