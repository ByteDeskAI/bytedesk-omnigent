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

async def _execute_comment_tool(
    tool_name: str,
    arguments: str,
    *,
    conversation_id: str | None,
    server_client: httpx.AsyncClient | None,
) -> str:
    """
    Runner-local handler for ``list_comments`` and ``update_comment``.

    The runner is a separate subprocess from the Omnigent server and has no
    in-process ``CommentStore``. This handler uses ``server_client`` to
    call the Omnigent server's REST API (``GET/PATCH
    /v1/sessions/{id}/comments``), following the same pattern as the
    file tools.

    :param tool_name: ``"list_comments"`` or ``"update_comment"``.
    :param arguments: JSON-encoded arguments string from the LLM.
    :param conversation_id: Current session id, e.g.
        ``"conv_abc123"``. Required for per-session comment scoping.
    :param server_client: HTTP client pointed at the Omnigent server.
        ``None`` if unavailable (returns an error string).
    :returns: Tool output JSON string.
    """
    if server_client is None:
        return json.dumps({"error": f"{tool_name} requires server access"})
    if conversation_id is None:
        return json.dumps({"error": f"{tool_name} requires a session id"})

    try:
        args: dict[str, Any] = json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError:
        return json.dumps({"error": f"{tool_name}: malformed JSON arguments"})
    base = f"/v1/sessions/{conversation_id}/comments"

    if tool_name == ListCommentsTool.name():
        params: dict[str, str] = {}
        if args.get("path"):
            params["path"] = args["path"]
        try:
            resp = await server_client.get(base, params=params, timeout=30.0)
            if resp.status_code != 200:
                return json.dumps({"error": f"list_comments returned {resp.status_code}"})
            all_comments: list[dict[str, Any]] = resp.json()
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": f"list_comments failed: {exc}"})
        # The server's GET endpoint only supports ?path= filtering;
        # apply status filter client-side.
        status_filter: str | None = args.get("status")
        if status_filter is not None:
            all_comments = [c for c in all_comments if c.get("status") == status_filter]
        return json.dumps({"comments": all_comments})

    # update_comment
    comment_id: str | None = args.get("comment_id")
    status: str | None = args.get("status")
    if not comment_id:
        return json.dumps({"error": "missing required argument: comment_id"})
    if not status:
        return json.dumps({"error": "missing required argument: status"})
    _valid_statuses = {"draft", "addressed"}
    if status not in _valid_statuses:
        return json.dumps(
            {"error": f"invalid status {status!r}; must be one of {sorted(_valid_statuses)}"}
        )
    try:
        resp = await server_client.patch(
            f"{base}/{comment_id}",
            json={"status": status},
            timeout=30.0,
        )
        if resp.status_code == 404:
            return json.dumps({"error": f"comment not found: {comment_id}"})
        if resp.status_code != 200:
            return json.dumps({"error": f"update_comment returned {resp.status_code}"})
        return json.dumps({"comment": resp.json()})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"update_comment failed: {exc}"})

async def _execute_policy_tool(
    tool_name: str,
    arguments: str,
    *,
    conversation_id: str | None,
    server_client: httpx.AsyncClient | None,
) -> str:
    """
    Runner-local handler for ``sys_add_policy`` and ``sys_policy_registry``.

    ``sys_policy_registry`` proxies ``GET /v1/policy-registry`` so the
    agent can browse available builtin policies before picking one.

    ``sys_add_policy`` proxies ``POST /v1/sessions/{id}/policies``.
    Two modes: (1) CEL expression — ``expression`` + ``reason`` are
    translated into the ``cel_policy`` builtin factory; (2) builtin —
    ``handler`` + ``factory_params`` are forwarded as-is.

    :param tool_name: ``"sys_add_policy"`` or ``"sys_policy_registry"``.
    :param arguments: JSON-encoded arguments string from the LLM.
    :param conversation_id: Current session id, e.g.
        ``"conv_abc123"``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: Tool output JSON string.
    """
    if server_client is None:
        return json.dumps({"error": f"{tool_name} requires server access"})

    if tool_name == "sys_policy_registry":
        return await _execute_list_policies(server_client)

    if conversation_id is None:
        return json.dumps({"error": f"{tool_name} requires a session id"})

    try:
        args: dict[str, Any] = json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError:
        return json.dumps({"error": f"{tool_name}: malformed JSON arguments"})

    return await _execute_add_policy(args, conversation_id, server_client)

async def _execute_list_policies(
    server_client: httpx.AsyncClient,
) -> str:
    """
    Proxy ``GET /v1/policy-registry`` and return the list.

    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: JSON string with the policy registry entries.
    """
    try:
        resp = await server_client.get("/v1/policy-registry", timeout=30.0)
        if resp.status_code != 200:
            return json.dumps({"error": f"server returned {resp.status_code}"})
        data = resp.json().get("data", [])
        return json.dumps({"policies": data})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_policy_registry failed: {exc}"})

async def _execute_add_policy(
    args: dict[str, Any],
    conversation_id: str,
    server_client: httpx.AsyncClient,
) -> str:
    """
    Proxy ``POST /v1/sessions/{id}/policies`` to create a policy.

    Forwards ``handler`` and ``factory_params`` from the tool
    arguments directly to the session policy API as
    ``type="python"``.

    :param args: Parsed tool arguments from the LLM.
    :param conversation_id: Current session id.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: JSON string — created policy or error.
    """
    handler = args.get("handler")
    if not handler:
        return json.dumps(
            {"error": "sys_add_policy requires 'handler' (dotted path from sys_policy_registry)"}
        )
    payload: dict[str, Any] = {
        "name": args.get("name", ""),
        "type": "python",
        "handler": handler,
    }
    fp = args.get("factory_params")
    if fp is not None:
        payload["factory_params"] = fp

    try:
        resp = await server_client.post(
            f"/v1/sessions/{conversation_id}/policies",
            json=payload,
            timeout=30.0,
        )
        if resp.status_code not in (200, 201):
            body = resp.text[:500]
            return json.dumps(
                {
                    "error": f"server returned {resp.status_code}",
                    "details": body,
                }
            )
        result = resp.json()
        return json.dumps(
            {
                "policy_id": result.get("id"),
                "name": result.get("name"),
                "type": result.get("type"),
                "handler": result.get("handler"),
                "enabled": result.get("enabled"),
                "message": f"Policy '{result.get('name')}' created successfully.",
            }
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_add_policy failed: {exc}"})

