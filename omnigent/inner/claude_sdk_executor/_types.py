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

@dataclass
class _ClaudeClientState:
    client: _ClaudeClient
    model: str | None
    loop: asyncio.AbstractEventLoop | None = None
    task: asyncio.Task[None] | None = None

@dataclass(frozen=True)
class PreparedClaudeCli:
    """Result of wrapping the Claude CLI in an Omnigent sandbox.

    :param cli_path: Path the SDK should exec for the Claude CLI.  May be the
        original system CLI or a generated wrapper script that applies the
        sandbox before exec-ing the real binary.  ``None`` when no CLI is
        available (no system ``claude`` on PATH).
    :param enable_native_tools: ``True`` when the sandbox is active and it is
        safe for the SDK to enable the built-in native OS tools (Bash, Read,
        Edit, …).  ``False`` when the sandbox cannot be applied (e.g. network
        denied, unsupported platform) — the caller should then leave native
        tools disabled.
    """

    cli_path: str | None
    enable_native_tools: bool

@dataclass(frozen=True)
class _ResolvedSkills:
    """
    Pair of SDK options derived from a single ``skills_filter``
    value: ``ClaudeAgentOptions.skills`` and
    ``ClaudeAgentOptions.setting_sources``.

    Both are needed because the SDK's ``_apply_skills_defaults``
    auto-defaults ``setting_sources`` to ``["user", "project"]``
    whenever ``skills`` is non-None — including when ``skills=[]``.
    That auto-default loads ``~/.claude/skills/`` and the cwd's
    ancestor ``.claude/skills/`` chain into the system prompt
    listing even when the ``Skill`` tool itself is suppressed.
    Hermetic agents need to explicitly override
    ``setting_sources=[]`` to actually hide host skills from the
    model's view of its own skill listing.

    :param skills: Value for ``ClaudeAgentOptions.skills``:
        ``"all"`` / list of names / empty list for hermetic mode.
    :param setting_sources: Value for
        ``ClaudeAgentOptions.setting_sources``: ``None`` to let
        the SDK pick its default (``["user", "project"]``), or
        an explicit list (e.g. ``[]`` for hermetic mode where we
        don't want any scope-based discovery).
    """

    skills: str | list[str]
    setting_sources: list[str] | None


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _cli as _sib_cli
    from . import _content as _sib_content
    from . import _executor as _sib_executor
    from . import _mcp as _sib_mcp
    from . import _process as _sib_process
    from . import _protocols as _sib_protocols
    for _key, _value in _sib_cli.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_content.__dict__.items():
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

_wire_sibling_modules()
