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

def _sandbox_disabled_by_env() -> bool:
    """``True`` when the diagnostic bypass env var is set to a truthy
    value. Emits a WARNING on activation so CI output unambiguously
    confirms the bypass was in effect for a given run.
    """
    if os.environ.get(_NO_SANDBOX_ENV):
        logger.warning(
            "Sandbox bypass active (%s is set); skipping create_exec_launcher.",
            _NO_SANDBOX_ENV,
        )
        return True
    return False

def _terminate_process_tree(process: _Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    pid = process.pid
    if pid is not None:
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pid, signal.SIGTERM)
            return
    with suppress(ProcessLookupError, Exception):
        process.terminate()

def _kill_process_tree(process: _Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    pid = process.pid
    if pid is not None:
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pid, signal.SIGKILL)
            return
    with suppress(ProcessLookupError, Exception):
        process.kill()

@contextmanager
def _unset_env_var(name: str) -> Iterator[None]:
    """
    Temporarily remove an env var from ``os.environ`` for the duration of
    the ``with`` block, then restore it (or leave it absent if it was not
    set before).

    Used around the claude-cli subprocess spawn to strip ``CLAUDECODE``
    when our own Python process is itself running under Claude Code — the
    child cli otherwise reports a "nested session" error. The SDK builds
    the child env as ``{**os.environ, **options.env, ...}``, so the only
    way to *remove* (not just override with ``""``) a key is to unset it
    in ``os.environ`` during the spawn.

    :param name: Env var name to remove for the block, e.g. ``"CLAUDECODE"``.
    :yields: Nothing; restores ``os.environ[name]`` on exit if it was set.
    """
    previous = os.environ.pop(name, None)
    try:
        yield
    finally:
        if previous is not None:
            os.environ[name] = previous

def _call_optional_method(obj: Any, name: str) -> None:  # type: ignore[explicit-any]
    """Call ``obj.<name>()`` if it exists and is callable, swallowing errors.

    Uses a runtime attribute name so this stays out of the
    ``getattr(..., "<literal>", ...)`` lint's crosshairs while still giving
    mypy a known shape (``Any`` at the boundary — the caller's concrete
    types don't declare the sync ``close`` hook we probe here).
    """
    method = getattr(obj, name, None)
    if callable(method):
        with suppress(Exception):
            method()

def _best_effort_close(resource: _Stream | _Process) -> None:
    """Invoke a best-effort synchronous close on an SDK-internal handle.

    The current SDK exposes ``aclose`` (async) on streams and a no-``close``
    anyio ``Process``; older revs and test doubles may still ship a sync
    ``close`` method. We probe for it via ``hasattr``-style helpers and
    swallow any failures — this runs only on the force-close teardown path
    where the alternative is leaking the handle.
    """
    _call_optional_method(resource, _CLOSE_ATTR)
    transport_obj = getattr(resource, _TRANSPORT_ATTR, None)
    if transport_obj is not None:
        _call_optional_method(transport_obj, _CLOSE_ATTR)

def _ensure_sdk() -> ModuleType:
    """Import and return the claude_agent_sdk module, raising a clear error if missing."""
    try:
        import claude_agent_sdk

        return claude_agent_sdk
    except ImportError as exc:
        raise ImportError(
            "ClaudeSDKExecutor requires the 'claude-agent-sdk' package. "
            "Install it with: pip install claude-agent-sdk"
        ) from exc

def _resolve_skills_option(
    skills_filter: str | list[str],
) -> _ResolvedSkills | None:
    """
    Translate the spec's ``skills_filter`` into the pair of SDK
    options ``ClaudeAgentOptions.skills`` and
    ``ClaudeAgentOptions.setting_sources``.

    Three meaningful filter values produce three distinct SDK
    configurations:

    - ``"all"`` → ``skills="all"``, ``setting_sources=None`` (SDK
      auto-defaults to ``["user", "project"]``). All host skills
      from ``~/.claude/skills/`` and ``<cwd>/.claude/skills/``
      (walking up the cwd tree) appear in the model's listing.
    - ``"none"`` → ``skills=[]``, ``setting_sources=[]``. Both
      the ``Skill`` tool listing AND the scope-based discovery
      are suppressed: no host skills appear in the system
      prompt or as invokable. Bundled skills (loaded via
      ``--plugin-dir``) are unaffected by ``setting_sources``
      and remain visible.
    - ``list[str]`` → ``skills=[names]``, ``setting_sources=None``.
      Only the named subset is in the model's listing; the SDK's
      auto-default still loads user and project sources for
      CLAUDE.md and other settings.

    :param skills_filter: ``"all"`` / ``"none"`` / list of skill
        names from :class:`AgentSpec.skills_filter`.
    :returns: The :class:`_ResolvedSkills` pair, or ``None`` when
        *skills_filter* is malformed — the caller falls back to
        ``"all"`` semantics.
    """
    if skills_filter == "all":
        return _ResolvedSkills(skills="all", setting_sources=None)
    if skills_filter == "none":
        # Empty ``skills`` suppresses the listing AND empty
        # ``setting_sources`` skips the SDK's auto-default that
        # would otherwise load ``~/.claude/skills/`` for the
        # system prompt anyway.
        return _ResolvedSkills(skills=[], setting_sources=[])
    if isinstance(skills_filter, list):
        return _ResolvedSkills(skills=list(skills_filter), setting_sources=None)
    return None


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _cli as _sib_cli
    from . import _content as _sib_content
    from . import _executor as _sib_executor
    from . import _mcp as _sib_mcp
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
    for _key, _value in _sib_mcp.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_protocols.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_types.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
