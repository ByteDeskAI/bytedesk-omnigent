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

def _parse_data_uri(uri: str) -> tuple[str, str]:
    """
    Parse a ``data:`` URI into ``(media_type, base64_data)``.

    :param uri: A data URI, e.g.
        ``"data:image/png;base64,iVBOR..."``.
    :returns: Tuple of ``(media_type, base64_payload)``.
    :raises ValueError: If the URI is not a valid ``data:`` URI.
    """
    if not uri.startswith("data:"):
        raise ValueError(f"Not a data URI: {uri[:40]!r}")
    header, _, payload = uri[5:].partition(",")
    media_type = header.replace(";base64", "")
    return media_type, payload

def _to_anthropic_content_blocks(
    blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Convert Responses API content blocks to Anthropic Messages
    API content block format.

    Mapping:

    - ``input_text`` / ``output_text`` → ``{"type": "text", ...}``
    - ``input_image`` (with ``image_url`` data URI) →
      ``{"type": "image", "source": {"type": "base64", ...}}``
    - ``input_file`` (with ``file_data`` data URI) →
      ``{"type": "document", "source": {"type": "base64", ...}}``

    :param blocks: Responses API content block dicts.
    :returns: Anthropic API content block dicts.
    """
    result: list[dict[str, Any]] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type in ("input_text", "output_text", "text"):
            result.append({"type": "text", "text": block["text"]})
        elif block_type == "input_image":
            image_url = block.get("image_url")
            if not isinstance(image_url, str) or not image_url:
                raise ValueError(
                    "input_image block is missing the 'image_url' field. "
                    "Upload the image via the session files API and reference "
                    "it by file_id so the content resolver can inline it."
                )
            if not image_url.startswith("data:"):
                raise ValueError(
                    "input_image block has a URL instead of a data URI. "
                    "Upload the image via the session files API and reference "
                    "it by file_id instead."
                )
            media_type, data = _parse_data_uri(image_url)
            result.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": data,
                    },
                }
            )
        elif block_type == "input_file":
            file_data = block.get("file_data")
            if not isinstance(file_data, str) or not file_data:
                raise ValueError(
                    "input_file block is missing the 'file_data' field. "
                    "Upload the file via the session files API and reference "
                    "it by file_id so the content resolver can inline it."
                )
            if not file_data.startswith("data:"):
                raise ValueError(
                    "input_file block has a URL instead of a data URI. "
                    "Upload the file via the session files API and reference "
                    "it by file_id instead."
                )
            media_type, data = _parse_data_uri(file_data)
            if media_type == "application/pdf":
                # Anthropic's base64 document source only accepts PDF.
                result.append(
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": data,
                        },
                    }
                )
            else:
                # All other text files (markdown, plain text, code, etc.)
                # must use Anthropic's "text" source type with decoded content.
                text_content = base64.b64decode(data).decode("utf-8", errors="replace")
                result.append(
                    {
                        "type": "document",
                        "source": {
                            "type": "text",
                            "media_type": "text/plain",
                            "data": text_content,
                        },
                    }
                )
    return result

async def _multimodal_message_iter(
    content_blocks: list[dict[str, Any]],
    *,
    session_id: str,
) -> AsyncIterator[dict[str, Any]]:
    """
    Yield a single structured user message dict for the Claude
    SDK's ``AsyncIterable[dict]`` query path.

    The SDK transport writes each yielded dict as a JSONL line to
    the CLI's stdin. The CLI forwards the content blocks to the
    Anthropic Messages API, which supports multimodal input.

    :param content_blocks: Anthropic API content block dicts
        (output of :func:`_to_anthropic_content_blocks`).
    :param session_id: The SDK session identifier.
    :yields: A single message dict.
    """
    yield {
        "type": "user",
        "message": {"role": "user", "content": content_blocks},
        "parent_tool_use_id": None,
        "session_id": session_id,
    }


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _cli as _sib_cli
    from . import _executor as _sib_executor
    from . import _mcp as _sib_mcp
    from . import _process as _sib_process
    from . import _protocols as _sib_protocols
    from . import _types as _sib_types
    for _key, _value in _sib_cli.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_executor.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_mcp.__dict__.items():
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
