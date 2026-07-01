"""ClaudeSDKExecutor: run agents using the Claude Agent SDK.

Uses the ``claude-agent-sdk`` Python package to run Claude Code as the
underlying agent harness.  Omnigent tools are bridged into the SDK session
as MCP tools so Claude can call them alongside its built-in capabilities.

The SDK manages its own internal agent loop (tool calls, retries, context).
This executor translates the SDK message stream into Omnigent ExecutorEvents
and builds up the session History from observed tool-use blocks.

Requirements:
    pip install claude-agent-sdk          # optional dependency

Environment (direct Anthropic):
    ANTHROPIC_API_KEY – API key for Claude

Environment (Databricks-hosted Claude via native Anthropic Messages API):
    DATABRICKS_CONFIG_PROFILE – optional Databricks profile selector
    ~/.databrickscfg          – host + token profile for workspace access
    (or ~/.databrickscfg with a profile containing host + token)

    The executor builds ANTHROPIC_BASE_URL plus an invocation-local
    apiKeyHelper setting from Databricks credentials so Claude Code can
    refresh auth through ``databricks auth token`` while routing through
    the Databricks gateway.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import pathlib
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Protocol, TypeAlias, cast

from omnigent.claude_native_bridge import (
    build_mcp_config,
    post_tools_changed,
    prepare_bridge_dir,
    start_tool_relay,
)
from omnigent.inner.bundle_skills import ensure_bundle_plugin_manifest
from omnigent.llms._usage_observer import notify_from_dict as _notify_usage_from_dict
from omnigent.onboarding.databricks_config import DATABRICKS_CLAUDE_DEFAULT_MODEL
from omnigent.reasoning_effort import CLAUDE_EFFORTS, validate_effort
from omnigent.spec.types import RetryPolicy

from .._subprocess_lifecycle import close_anyio_subprocess_transport
from ..claude_gateway_shim import DATABRICKS_CLAUDE_ADAPTIVE_THINKING_PREFIXES, ClaudeGatewayShim
from ..datamodel import OSEnvSandboxSpec, OSEnvSpec
from ..executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    ToolSpec,
    TurnComplete,
    classify_tool_result,
)
from ..sandbox import (
    create_exec_launcher,
    resolve_sandbox,
    with_additional_read_roots,
    with_additional_write_files,
    with_additional_write_roots,
)

logger = logging.getLogger(__name__)

# Default auth-token refresh cadence (ms) for the vendor-neutral gateway
# transport when ``HARNESS_CLAUDE_SDK_GATEWAY_AUTH_REFRESH_INTERVAL_MS`` is
# unset. Not Databricks-specific: the same fallback applies to any gateway
# producer (Databricks AI gateway or a generic key/gateway provider).
_GATEWAY_AUTH_REFRESH_MS = 900_000

# ---------------------------------------------------------------------------
# TypeAliases for Omnigent JSON-shaped boundary values. The SDK exchanges
# heterogeneous dicts at the transport and tool boundaries — named aliases
# here keep the executor ``object``-free while isolating the justified
# ``explicit-any`` boundary to a single place, mirroring the peer
# ``openai_agents_sdk_executor`` / ``databricks_executor`` conventions.
# ---------------------------------------------------------------------------

# Parsed tool arguments / tool result dict — JSON-shaped bags exchanged
# with the Omnigent tool executor and the SDK's MCP bridge.
ToolArgs: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]
ToolResult: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# MCP response payload (``content`` + optional ``isError``) returned to the
# Claude SDK from each MCP tool handler.
McpResponse: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# Tool executor callable wired in by ``omnigent.Session``.
ToolExecutor: TypeAlias = Callable[[str, ToolArgs], Awaitable[ToolResult]]

# Elicitation handler wired in by :class:`ExecutorAdapter`. Kept SDK-agnostic
# so the adapter does not import ``claude_agent_sdk`` types.
ElicitationHandler: TypeAlias = Callable[  # type: ignore[explicit-any]
    [str, ToolArgs],
    Awaitable[bool],
]

# Opaque SDK artifacts whose concrete shape we don't touch directly:
# - ``SdkMcpTool``: returned by ``sdk.tool(...)`` decorator and passed back
#   to ``sdk.create_sdk_mcp_server(tools=...)`` without field access.
# - ``ClaudeAgentOptions``: the SDK's dataclass; fields set via attribute
#   assignment after construction rather than typed kwargs.
SdkMcpTool: TypeAlias = Any  # type: ignore[explicit-any]
SdkOptions: TypeAlias = Any  # type: ignore[explicit-any]


# ---------------------------------------------------------------------------
# SDK-private reach Protocols.
#
# ``claude_agent_sdk.*`` is listed as ``ignore_missing_imports`` in mypy
# config, so every SDK-typed value mypy sees is ``Any``. We recover types
# locally with Protocols for the handful of public and private attributes
# this executor touches.
#
# The private reaches (``_query``, ``_transport``, ``_process``,
# ``_stderr_task`` / ``_stderr_task_group``, etc.) are necessary to tear
# down the CLI subprocess tree when the SDK's own ``disconnect()`` path is unsafe
# (different event loop / task) or hangs. The SDK does not expose a
# supported equivalent, so we treat the private attributes as part of
# our integration contract and document them here.
# ---------------------------------------------------------------------------


def _import_package_bindings() -> None:
    from . import _constants as _pkg_constants
    from . import _state as _pkg_state
    g = globals()
    for _mod in (_pkg_constants, _pkg_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_package_bindings()

def _build_mcp_tools(
    tool_schemas: list[ToolSpec],
    tool_executor: ToolExecutor | None,
    sdk: _ClaudeSDK | None = None,
) -> list[SdkMcpTool]:
    """Build SdkMcpTool objects from Omnigent tool schemas.

    Each tool is backed by a handler that calls the Omnigent tool_executor
    callback, which routes through the Session's tool registry (and thus
    respects policies, history recording, etc.).
    """
    sdk = sdk or cast(_ClaudeSDK, _ensure_sdk())

    mcp_tools: list[SdkMcpTool] = []
    for schema in tool_schemas:
        raw_name = schema.get("name")
        raw_desc = schema.get("description")
        # ``sdk.tool()`` requires ``str`` for name/description — the SDK
        # itself does not accept ``None``. Omnigent tool schemas always
        # carry a ``name`` (see ``Tool.tool_schema``); fall back to ``""``
        # only for the description, which is legitimately optional.
        tname: str = raw_name if isinstance(raw_name, str) else ""
        sdk_tname = _claude_sdk_visible_tool_name(tname)
        tdesc: str = raw_desc if isinstance(raw_desc, str) else ""
        tparams = schema.get("parameters", {"type": "object", "properties": {}})

        def _make_handler(tool_name: str) -> Callable[[ToolArgs], Awaitable[McpResponse]]:
            async def handler(args: ToolArgs) -> McpResponse:
                if tool_executor is None:
                    return {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {"error": f"No tool executor for '{tool_name}'"}
                                ),
                            }
                        ],
                    }
                try:
                    # ``tool_executor`` is declared as ``Awaitable[ToolResult]``
                    # so we always await. The isinstance guard below preserves
                    # the pre-refactor safety net for unexpected non-dict
                    # runtime returns without confusing the type checker.
                    raw = await tool_executor(tool_name, args)
                    result: ToolResult = raw if isinstance(raw, dict) else {"result": raw}
                    response: McpResponse = {
                        "content": [{"type": "text", "text": json.dumps(result)}],
                    }
                    if result.get("blocked") is True or (
                        "error" in result and result.get("error")
                    ):
                        response["isError"] = True
                    return response
                except Exception as exc:  # noqa: BLE001 — tool handler converts any error to MCP error response
                    return {
                        "content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
                        "isError": True,
                    }

            return handler

        decorated = sdk.tool(sdk_tname, tdesc, tparams)(_make_handler(tname))
        mcp_tools.append(decorated)
    return mcp_tools

def _omnigent_mcp_server_name(index: int) -> str:
    """Return the deterministic SDK MCP server name for a chunk index."""
    if index == 0:
        return _OMNIGENT_MCP_SERVER_NAME
    return f"{_OMNIGENT_MCP_SERVER_NAME}{index + 1}"

def _generated_sdk_mcp_tool_name(server_name: str, raw_name: str) -> str:
    """Return the Claude Code callable name for a raw Omnigent tool."""
    return f"mcp__{server_name}__{_claude_sdk_visible_tool_name(raw_name)}"

def _build_sdk_mcp_servers(
    sdk: _ClaudeSDK,
    tool_schemas: list[ToolSpec],
    tool_executor: ToolExecutor | None,
) -> tuple[dict[str, Any], list[str], dict[str, str]]:
    """Build chunked SDK MCP servers and the generated tool-name map.

    Claude Code 2.1.x hides all tools from an in-process SDK MCP server once
    that server exposes six or more tools. Keep each generated server at five
    tools so larger Omnigent/ByteDesk MCP surfaces remain discoverable through
    the normal MCP path without duplicating connector implementations.
    """
    mcp_servers: dict[str, Any] = {}  # type: ignore[explicit-any]
    generated_tool_names: list[str] = []
    generated_by_raw_name: dict[str, str] = {}
    seen_generated: set[str] = set()

    for offset in range(0, len(tool_schemas), _OMNIGENT_MCP_SERVER_CHUNK_SIZE):
        chunk = tool_schemas[offset : offset + _OMNIGENT_MCP_SERVER_CHUNK_SIZE]
        if not chunk:
            continue
        chunk_index = offset // _OMNIGENT_MCP_SERVER_CHUNK_SIZE
        server_name = _omnigent_mcp_server_name(chunk_index)
        mcp_tools = _build_mcp_tools(chunk, tool_executor, sdk=sdk)
        if not mcp_tools:
            continue
        mcp_servers[server_name] = sdk.create_sdk_mcp_server(
            name=server_name,
            version="1.0.0",
            tools=mcp_tools,
        )
        for schema in chunk:
            raw_name = schema.get("name")
            if not isinstance(raw_name, str) or not raw_name:
                continue
            generated_name = _generated_sdk_mcp_tool_name(server_name, raw_name)
            generated_by_raw_name[raw_name] = generated_name
            if generated_name in seen_generated:
                continue
            seen_generated.add(generated_name)
            generated_tool_names.append(generated_name)

    return mcp_servers, generated_tool_names, generated_by_raw_name

def _sanitize_claude_mcp_schema(value: Any) -> Any:  # type: ignore[explicit-any]
    """Return a Claude Code-safe copy of an MCP JSON Schema node.

    MCP permits boolean JSON Schema nodes. Claude Code's tool manifest accepts
    boolean values for keywords such as ``additionalProperties: false`` but
    rejects boolean property schemas like ``"event": true``. Convert ``true``
    schema nodes to ``{}``, which is the equivalent permissive schema.
    """
    if value is True:
        return {}
    if value is False:
        return False
    if isinstance(value, list):
        return [_sanitize_claude_mcp_schema(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _sanitize_claude_mcp_schema(item) for key, item in value.items()}
    return value

def _claude_sdk_relay_tool_schema(schema: ToolSpec) -> ToolSpec | None:
    """Convert one Omnigent tool schema to the stdio relay's visible schema."""
    raw_name = schema.get("name")
    if not isinstance(raw_name, str) or not raw_name:
        return None
    raw_desc = schema.get("description")
    parameters = schema.get("parameters")
    relay_schema: ToolSpec = {
        "name": _claude_sdk_visible_tool_name(raw_name),
        "description": raw_desc if isinstance(raw_desc, str) else "",
        "parameters": (
            _sanitize_claude_mcp_schema(parameters)
            if isinstance(parameters, Mapping)
            else {"type": "object", "properties": {}}
        ),
    }
    return relay_schema

def _build_stdio_bridge_mcp_tools(
    tool_schemas: list[ToolSpec],
) -> tuple[list[ToolSpec], list[str], dict[str, str], dict[str, str]]:
    """Build relay schemas and name maps for the existing stdio MCP bridge."""
    relay_tools: list[ToolSpec] = []
    generated_tool_names: list[str] = []
    generated_by_raw_name: dict[str, str] = {}
    raw_by_visible_name: dict[str, str] = {}
    seen_generated: set[str] = set()

    for schema in tool_schemas:
        raw_name = schema.get("name")
        if not isinstance(raw_name, str) or not raw_name:
            continue
        relay_schema = _claude_sdk_relay_tool_schema(schema)
        if relay_schema is None:
            continue
        visible_name = cast(str, relay_schema["name"])
        generated_name = _generated_sdk_mcp_tool_name(_OMNIGENT_MCP_SERVER_NAME, raw_name)
        raw_by_visible_name[visible_name] = raw_name
        generated_by_raw_name[raw_name] = generated_name
        relay_tools.append(relay_schema)
        if generated_name in seen_generated:
            continue
        seen_generated.add(generated_name)
        generated_tool_names.append(generated_name)

    return relay_tools, generated_tool_names, generated_by_raw_name, raw_by_visible_name

def _claude_sdk_visible_tool_name(raw_name: str) -> str:
    """Return the tool name registered with Claude's SDK MCP server.

    Omnigent namespaces external MCP tools as ``server__tool`` so dispatch can
    route calls back to the owning server. The Claude Code MCP wrapper also
    uses ``mcp__server__tool``. Passing a raw name that itself contains ``__``
    creates an ambiguous nested name such as
    ``mcp__omnigent__bytedesk-platform__googleworkspace_drive_search``; live
    Claude Code sessions then omit that tool from the active manifest. Register
    a single-segment alias for those external tools and map the handler back to
    the original raw name.
    """
    if "__" not in raw_name:
        return raw_name
    return raw_name.replace("-", "_").replace("__", "_")

def _omnigent_tool_naming_note(
    tool_names: list[str],
    generated_by_raw_name: dict[str, str] | None = None,
) -> str:
    """Build a system-prompt note bridging bare Omnigent built-in tool names
    to the generated MCP names the claude-agent-sdk advertises.

    Claude Code exposes MCP tools as ``mcp__<server>__<tool>``.
    Native agent prompts (and the openai-agents harness, which registers bare
    names directly) refer to the built-ins by their bare ``sys_*`` names.
    Without this bridge the model on the claude-sdk harness treats a bare
    ``sys_agent_list`` as a *skill* and the Claude Code CLI returns
    "Unknown skill: sys_agent_list" (BDP-2204).

    Only the ``sys_*`` built-ins are documented — those are the tools prompts
    reference by bare name. MCP-server tools (e.g. ``bytedesk-platform``) carry
    descriptive names the model invokes directly and are intentionally omitted
    to keep the note short. Returns "" when there are no built-ins to document.
    """
    builtins = sorted({n for n in tool_names if n and n.startswith("sys_")})
    if not builtins:
        return ""
    generated = generated_by_raw_name or {}
    listed = "\n".join(
        f"- bare `{n}` → call `{generated.get(n, _OMNIGENT_MCP_PREFIX + n)}`"
        for n in builtins
    )
    return (
        "# Omnigent built-in tool names\n"
        "Your Omnigent orchestration/OS built-in tools are exposed to you as "
        "generated MCP tool names. When any instruction names one of these by "
        "its bare name, invoke the callable form below. These are regular "
        "tools, NOT skills — never use the `Skill` tool to call them:\n"
        f"{listed}"
    )

def _omnigent_tool_alias_note(
    tool_names: list[str],
    generated_by_raw_name: dict[str, str] | None = None,
) -> str:
    """Document Claude-safe aliases for namespaced external MCP tools."""
    generated = generated_by_raw_name or {}
    aliases = [
        (
            raw,
            generated.get(raw, _OMNIGENT_MCP_PREFIX + _claude_sdk_visible_tool_name(raw)),
        )
        for raw in sorted({n for n in tool_names if n})
        if _claude_sdk_visible_tool_name(raw) != raw
    ]
    if not aliases:
        return ""
    listed = "\n".join(
        f"- raw `{raw}` → call `{generated_name}`" for raw, generated_name in aliases
    )
    return (
        "# Omnigent external MCP tool aliases\n"
        "Some Omnigent MCP tools come from external servers and have raw "
        "`server__tool` names. Claude Code exposes those through safe aliases "
        "as generated MCP tool names. When an instruction names one of these "
        "raw tools, invoke the callable name below. If a callable is deferred "
        "or is not immediately callable, first call `ToolSearch` with "
        "`select:<callable-name>` to load its schema, then call the callable:\n"
        f"{listed}"
    )


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _cli as _sib_cli
    from . import _content as _sib_content
    from . import _executor as _sib_executor
    from . import _process as _sib_process
    from . import _protocols as _sib_protocols
    from . import _types as _sib_types
    for _key, _value in _sib_cli.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_content.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_executor.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_process.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_protocols.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_types.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
