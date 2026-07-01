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

def is_action_required(event: ActionRequiredEvent) -> bool:
    """Check if an SSE event is an action_required tool call."""
    if event.get("type") != "response.output_item.done":
        return False
    item = event.get("item") or {}
    return item.get("type") == "function_call" and item.get("status") == "action_required"

def get_tool_name(event: ActionRequiredEvent) -> str:
    """Extract the tool name from an action_required event."""
    return (event.get("item") or {}).get("name", "")

def should_dispatch_locally(tool_name: str) -> bool:
    """Return True if this tool should be dispatched by the runner locally.

    Used by BOTH the runner's proxy_stream (to decide whether to
    dispatch) AND the server-side executor (to skip its own dispatch
    for tools the runner already handled). The executor imports this
    function directly — Phase 5 of RUNNER_TOOL_DISPATCH.md.
    """
    return tool_name in _ALL_LOCAL_TOOLS

def _is_spec_local_python_tool(tool_name: str, agent_spec: AgentSpecLike | None) -> bool:
    local_tools = getattr(agent_spec, "local_tools", None) or []
    return any(
        getattr(info, "name", None) == tool_name
        and getattr(info, "language", None) == "python"
        and getattr(info, "path", None)
        for info in local_tools
    )

def _is_spec_builtin_tool(tool_name: str, agent_spec: AgentSpecLike | None) -> bool:
    """
    Return whether *tool_name* is explicitly declared in ``tools.builtins``.

    Generic builtins (including extension-contributed tools such as
    ``bytedesk_jira``) are model-visible through ``ToolManager`` schemas, but
    most do not belong to one of the runner's hard-coded tool families. This
    predicate lets the dispatch tail route those declared builtins back through
    ``ToolManager`` instead of falling through to spec-callable resolution.

    :param tool_name: Tool name emitted by the model.
    :param agent_spec: Current session agent spec.
    :returns: ``True`` when the spec declares the builtin.
    """
    tools = getattr(agent_spec, "tools", None)
    builtins = getattr(tools, "builtins", None) or []
    return any(getattr(entry, "name", None) == tool_name for entry in builtins)

def should_relay_tool_to_native(tool_name: str, agent_spec: AgentSpecLike | None) -> bool:
    """
    Return whether a ToolManager schema should be added to the native CLI relay.

    Claude-native / Codex-native ignore the harness ``tools`` list and only see
    the MCP relay. Always relay the runner/server-proxied builtin families, and
    additionally relay spec-declared generic builtins so extension tools exposed
    via ``tools.builtins`` are reachable from native agents too. OS tools stay
    excluded here because the native bridge installs the policy-enforced OS
    relay separately.

    :param tool_name: Tool schema name from ``ToolManager``.
    :param agent_spec: Current session agent spec.
    :returns: ``True`` if the relay should advertise the schema.
    """
    if tool_name in _OS_ENV_TOOLS:
        return False
    return tool_name in _NATIVE_RELAY_BUILTIN_TOOLS or _is_spec_builtin_tool(
        tool_name,
        agent_spec,
    )

def _is_uc_function_tool(
    tool_name: str,
    agent_spec: AgentSpecLike | None,
) -> bool:
    """
    Check whether *tool_name* is a UC function tool in the spec.

    :param tool_name: Tool name from the LLM, e.g.
        ``"classify_text"``.
    :param agent_spec: The session's :class:`AgentSpec`. ``None``
        when no spec is available.
    :returns: ``True`` if the tool is a
        :attr:`ToolRuntime.UC_FUNCTION` tool.
    """
    if agent_spec is None:
        return False
    local_tools = getattr(agent_spec, "local_tools", None) or []
    from omnigent.spec.types import ToolRuntime

    return any(
        lt.name == tool_name and lt.runtime == ToolRuntime.UC_FUNCTION for lt in local_tools
    )

