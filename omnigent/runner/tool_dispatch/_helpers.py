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

@dataclass(frozen=True)
class _CancelAsyncToolResult:
    """
    Internal result for local async-task cancellation.

    :param output: Tool output string to return to the LLM.
    :param try_subagent_cancel: Whether no local async task matched,
        so ``sys_cancel_task`` should try the sub-agent work registry
        next.
    """

    output: str
    try_subagent_cancel: bool = False

def get_call_id(event: ActionRequiredEvent) -> str:
    """Extract the call_id from an action_required event."""
    return (event.get("item") or {}).get("call_id", "")

def get_arguments(event: ActionRequiredEvent) -> str:
    """Extract the arguments JSON string from an action_required event."""
    return (event.get("item") or {}).get("arguments", "{}")

async def _collect_sub_agents(
    conversation_id: str,
    server_client: httpx.AsyncClient,
) -> list[dict[str, str | None]]:
    """
    Collect the caller's named-sub-agent view via ``GET .../child_sessions``.

    Returns ``[{"agent", "title", "conversation_id"}, ...]``, skipping
    closed and titleless/colonless rows so they never re-surface to the
    LLM. Includes the caller's own children and, when the caller is
    itself a child (e.g. a user-added agent), its parent (surfaced as
    ``agent="main"``) and its siblings — so an added agent can still
    discover ``main`` and its session-mates. Best-effort: a failed
    lookup yields ``[]`` (or own-children-only) rather than raising.

    :param conversation_id: The caller session id.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: The sub-agent entries.
    """
    try:
        resp = await server_client.get(
            f"/v1/sessions/{conversation_id}/child_sessions",
            params={"limit": 100},
            timeout=30.0,
        )
    except Exception:  # noqa: BLE001
        return []
    if resp.status_code != 200:
        return []
    result = _child_rows_to_entries(resp.json().get("data", []))

    # If the caller is itself a child, surface main + siblings too.
    parent_id = await _session_parent_id(conversation_id, server_client)
    if parent_id is not None:
        result.append({"agent": "main", "title": None, "conversation_id": parent_id})
        try:
            sib_resp = await server_client.get(
                f"/v1/sessions/{parent_id}/child_sessions",
                params={"limit": 100},
                timeout=30.0,
            )
            if sib_resp.status_code == 200:
                for entry in _child_rows_to_entries(sib_resp.json().get("data", [])):
                    # Exclude the caller itself from its own sibling list.
                    if entry["conversation_id"] != conversation_id:
                        result.append(entry)
        except Exception:  # noqa: BLE001
            _logger.debug(
                "sys_session_list sibling enrichment failed for parent %s",
                parent_id,
                exc_info=True,
            )
    return result

async def _collect_global_sessions(
    server_client: httpx.AsyncClient,
    agent_name: Any,
) -> list[dict[str, Any]]:
    """
    Fetch the global session list via ``GET /v1/sessions``, with connectivity.

    Projects each accessible session to ``{session_id, agent_name, title,
    status, runner_id, runner_online, parent_session_id}``.
    ``runner_online`` is resolved once per unique bound runner (see
    :func:`_resolve_runner_online_map`). An optional ``agent_name``
    filters the list server-side. Permission-bounded by the server (the
    runner's request carries the owning user's identity). Best-effort:
    returns ``[]`` on a fetch failure.

    :param server_client: HTTP client pointed at the Omnigent server.
    :param agent_name: Optional agent-name filter; applied only when a
        non-empty string.
    :returns: The projected global session entries.
    """
    params: dict[str, Any] = {"limit": _AGENT_LIST_PAGE_LIMIT, "order": "desc"}
    if isinstance(agent_name, str) and agent_name:
        params["agent_name"] = agent_name
    try:
        resp = await server_client.get("/v1/sessions", params=params, timeout=30.0)
    except Exception:  # noqa: BLE001
        return []
    if resp.status_code != 200:
        return []
    rows = resp.json().get("data", [])
    if not isinstance(rows, list):
        return []
    online = await _resolve_runner_online_map(rows, server_client)
    return [
        {
            "session_id": r.get("id"),
            "agent_name": r.get("agent_name"),
            "title": r.get("title"),
            "status": r.get("status"),
            "runner_id": r.get("runner_id"),
            "runner_online": online.get(r.get("runner_id")),
            "parent_session_id": r.get("parent_session_id"),
        }
        for r in rows
    ]
