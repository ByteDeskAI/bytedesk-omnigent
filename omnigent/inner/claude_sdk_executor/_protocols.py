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

class _Process(Protocol):
    """Subset of ``anyio.abc.Process`` / ``asyncio.subprocess.Process``.

    These fields are standard on both process abstractions — the SDK's
    transport uses an anyio process but the shape matches the asyncio one
    for the attributes we touch.
    """

    pid: int | None
    returncode: int | None

    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    async def wait(self) -> int: ...

class _CancelScope(Protocol):
    def cancel(self) -> None: ...

class _TaskGroup(Protocol):
    cancel_scope: _CancelScope

class _TaskHandle(Protocol):
    """Private view of the SDK's detached stderr-reader task.

    Current ``claude-agent-sdk`` (>=0.2.x) runs the stderr reader as a
    single task exposed as ``_stderr_task`` with a ``cancel()`` method;
    older revs used an anyio task group (``_stderr_task_group``). The
    executor probes both shapes during force-close, so both are typed
    optional on ``_ClaudeTransport`` below.
    """

    def cancel(self) -> None: ...

class _ClaudeQuery(Protocol):
    """Private view of ``claude_agent_sdk._internal.query.Query``.

    ``_closed`` is the SDK's "stop accepting messages" flag. ``_tg`` was a
    per-query task group in older SDK revs — absent in current revs but
    still probed so this executor handles both shapes.
    """

    _closed: bool
    _tg: _TaskGroup | None

class _Stream(Protocol):
    """Structural view of an anyio text stream. Only ``aclose`` is actually
    available on the real ``TextReceiveStream`` / ``TextSendStream``; the
    ``close`` / ``transport`` attributes probed during teardown are
    historical belt-and-suspenders cleanup and no-op on the current SDK.
    """

    async def aclose(self) -> None: ...

class _ClaudeTransport(Protocol):
    """Private view of ``SubprocessCLITransport`` internals we tear down.

    Kept minimal — only the attributes ``_force_close_client`` touches.
    """

    _process: _Process | None
    _stdout_stream: _Stream | None
    _stdin_stream: _Stream | None
    _stderr_stream: _Stream | None
    _stderr_task: _TaskHandle | None
    _stderr_task_group: _TaskGroup | None
    _ready: bool

class _ClaudeClient(Protocol):
    """Structural view of ``claude_agent_sdk.ClaudeSDKClient``.

    Covers the public methods the executor calls plus the two private
    attributes it clears during a force-close. Test doubles (see
    ``tests/test_claude_sdk_executor.py``) satisfy this Protocol
    structurally via ``SimpleNamespace`` / custom classes.
    """

    _query: _ClaudeQuery | None
    _transport: _ClaudeTransport | None

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def query(self, prompt: str, session_id: str = ...) -> None: ...
    async def set_model(self, model: str | None) -> None: ...
    async def interrupt(self) -> None: ...

    # receive_response yields heterogeneous SDK message objects — kept
    # as ``Any`` so the caller can ``isinstance``-narrow onto the SDK's
    # real types via ``_ClaudeSDK`` below without needing a union here.
    def receive_response(self) -> AsyncIterator[Any]: ...  # type: ignore[explicit-any]

class _StreamEventObj(Protocol):
    """Structural view of ``claude_agent_sdk.StreamEvent``."""

    event: dict[str, Any]  # type: ignore[explicit-any]  # SDK declares this as dict[str, Any]

class _AssistantMessageObj(Protocol):
    """Structural view of ``claude_agent_sdk.AssistantMessage``."""

    # Each content block is one of the SDK block classes; isinstance-narrowed
    # at the read sites below.
    content: list[Any]  # type: ignore[explicit-any]
    # The model the SDK actually used for this message, e.g.
    # ``"claude-opus-4-8"``. The only place the executor learns the concrete
    # model when the spec pins none and the gateway resolves it internally.
    model: str | None

class _UserMessageObj(Protocol):
    """Structural view of ``claude_agent_sdk.UserMessage``."""

    content: str | list[Any]  # type: ignore[explicit-any]

class _ResultMessageObj(Protocol):
    """Structural view of ``claude_agent_sdk.ResultMessage``."""

    result: str | None
    usage: dict[str, Any] | None  # type: ignore[explicit-any]

class _SystemMessageObj(Protocol):
    """Structural view of ``claude_agent_sdk.SystemMessage``."""

    subtype: str
    data: dict[str, Any]  # type: ignore[explicit-any]

class _TextBlockObj(Protocol):
    text: str

class _ToolUseBlockObj(Protocol):
    id: str
    name: str
    input: ToolArgs

class _ToolResultBlockObj(Protocol):
    tool_use_id: str
    content: str | list[dict[str, Any]] | None  # type: ignore[explicit-any]
    is_error: bool | None

class _ClaudeSDK(Protocol):
    """Structural view of the ``claude_agent_sdk`` module.

    Tests swap in a fake with matching attributes, so we mirror what the
    executor actually pulls off the module. The ``*Message`` / ``*Block``
    attributes are declared as ``type`` so they can be used both as
    ``isinstance`` second args and as Protocol-implementing factories.
    """

    # Factories / callables the executor invokes. ``Callable[..., X]``
    # expands to an implicit ``Any`` arg spec under
    # ``disallow_any_explicit`` — the SDK's construction kwargs are opaque
    # at our boundary so that's the right abstraction level here.
    ClaudeSDKClient: Callable[..., _ClaudeClient]  # type: ignore[explicit-any]
    ClaudeAgentOptions: Callable[..., Any]  # type: ignore[explicit-any]
    tool: Callable[..., Any]  # type: ignore[explicit-any]
    create_sdk_mcp_server: Callable[..., Any]  # type: ignore[explicit-any]

    # Classes used as isinstance second args. Declared as ``type`` so the
    # checker accepts them in isinstance() while the real attributes are
    # the SDK's concrete classes. Test doubles assign plain ``type``
    # objects which satisfy this shape.
    AssistantMessage: type
    UserMessage: type
    SystemMessage: type
    ResultMessage: type
    TextBlock: type
    ToolUseBlock: type
    ToolResultBlock: type


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _cli as _sib_cli
    from . import _content as _sib_content
    from . import _executor as _sib_executor
    from . import _mcp as _sib_mcp
    from . import _process as _sib_process
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
    for _key, _value in _sib_mcp.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_process.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_types.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
