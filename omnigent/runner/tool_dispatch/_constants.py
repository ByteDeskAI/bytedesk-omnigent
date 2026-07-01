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
_INBOX_OUTPUT_MAX_CHARS = 12000
_OS_ENV_SHELL_DEFAULT_TIMEOUT_S = 120.0
_RUNNER_EXECUTION_TIMEOUT_S = 7200.0
_SUBAGENT_POLICY_STATUSES = frozenset({"completed", "failed"})
_SUBAGENT_INBOX_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_SUBAGENT_POLICY_FAILURE_OUTPUT = "[Result suppressed by policy: policy evaluation failed]"
_SESSION_WRAPPER_LABEL_KEY = "omnigent.wrapper"
MCP_PROXY_FORWARD_TIMEOUT_S = _RUNNER_EXECUTION_TIMEOUT_S + 30.0
MCP_PROXY_CALL_TIMEOUT_S = _RUNNER_EXECUTION_TIMEOUT_S + 60.0
_OS_ENV_TOOLS = frozenset(
    {
        SysOsReadTool.name(),
        SysOsWriteTool.name(),
        SysOsEditTool.name(),
        SysOsShellTool.name(),
    }
)
_REST_TOOLS: frozenset[str] = frozenset()
_FILE_TOOLS = frozenset(
    {
        UploadFileTool.name(),
        DownloadFileTool.name(),
        "list_files",  # from builtins registry; no standalone class
    }
)
_TERMINAL_TOOLS = frozenset(
    {
        SysTerminalLaunchTool.name(),
        SysTerminalSendTool.name(),
        SysTerminalReadTool.name(),
        SysTerminalListTool.name(),
        SysTerminalCloseTool.name(),
    }
)
_ASYNC_INBOX_TOOLS = frozenset(
    {
        SysCallAsyncTool.name(),
        SysReadInboxTool.name(),
        SysCancelAsyncTool.name(),
    }
)
_SUBAGENT_TOOLS = frozenset({"sys_session_send"})
_CHILD_MESSAGE_RETRY_DELAYS_S = (0.5, 1.0)
_SESSION_CREATE_TOOLS = frozenset({"sys_session_create"})
_SESSION_QUERY_TOOLS = frozenset(
    {"sys_session_get_history", "sys_session_list", "sys_session_close", "sys_session_get_info"}
)
_WEB_FETCH_TOOLS = frozenset({"web_fetch"})
_LIST_MODELS_TOOLS = frozenset({"sys_list_models"})
_TIMER_TOOLS = frozenset({"sys_timer_set", "sys_timer_cancel"})
_TASK_LIFECYCLE_TOOLS = frozenset(
    {
        SysCancelTaskTool.name(),
    }
)
_SKILL_TOOLS = frozenset({"load_skill", "read_skill_file"})
_COMMENT_TOOLS = frozenset(
    {
        ListCommentsTool.name(),
        UpdateCommentTool.name(),
    }
)
_AGENT_TOOLS = frozenset({"sys_agent_get", "sys_agent_download", "sys_agent_list"})
_POLICY_TOOLS = frozenset({"sys_add_policy", "sys_policy_registry"})
_SKILL_ACQ_TOOLS = frozenset(
    {
        "sys_skill_search",
        "sys_skill_sources",
        "sys_skill_installed",
        "sys_skill_resolve_targets",
        "sys_skill_stage_preview",
        "sys_skill_apply",
        "sys_skill_remove",
    }
)
_NATIVE_RELAY_BUILTIN_TOOLS = (
    _COMMENT_TOOLS
    | _SESSION_QUERY_TOOLS
    | _ASYNC_INBOX_TOOLS
    | _SUBAGENT_TOOLS
    | _LIST_MODELS_TOOLS
    | _SESSION_CREATE_TOOLS
    | _TASK_LIFECYCLE_TOOLS
    | _AGENT_TOOLS
    | _POLICY_TOOLS
    | _TERMINAL_TOOLS
)
_AGENT_CONFIG_SUBDIR = ".omnigent/agent-configs"
_AGENT_LIST_PAGE_LIMIT = 1000
_ALL_LOCAL_TOOLS = (
    _OS_ENV_TOOLS
    | _REST_TOOLS
    | _FILE_TOOLS
    | _TERMINAL_TOOLS
    | _ASYNC_INBOX_TOOLS
    | _SUBAGENT_TOOLS
    | _LIST_MODELS_TOOLS
    | _SESSION_CREATE_TOOLS
    | _SESSION_QUERY_TOOLS
    | _WEB_FETCH_TOOLS
    | _TIMER_TOOLS
    | _TASK_LIFECYCLE_TOOLS
    | _SKILL_TOOLS
    | _COMMENT_TOOLS
    | _AGENT_TOOLS
    | _POLICY_TOOLS
    | _SKILL_ACQ_TOOLS
)
_PLACEHOLDER_CWDS = (None, "", ".", "./")
_MAX_TIMER_SECONDS = 1_000_000.0
_CHANGED_FILES_SIGNAL_THROTTLE_S = 0.75
_CHANGED_FILES_SIGNAL_MAX_TRACKED = 4096
_CHANGED_FILES_TOOLS = frozenset(
    {SysOsWriteTool.name(), SysOsEditTool.name(), SysOsShellTool.name()}
)

