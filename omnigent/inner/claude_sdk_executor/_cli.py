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

def _find_system_claude() -> str | None:
    """Find a system-installed ``claude`` CLI binary on PATH.

    Returns the absolute path, or None if not found.  Prefers the system
    install over the SDK's bundled CLI because the bundled version may be
    older and send beta flags the Databricks gateway doesn't support.
    """
    return shutil.which("claude")

def _resolve_gateway_env(
    profile: str | None = None,
    *,
    host_override: str | None = None,
    base_url_override: str | None = None,
    auth_command_override: str | None = None,
    auth_refresh_interval_ms: int | None = None,
) -> dict[str, str]:
    """Build Claude Code gateway env from the gateway transport values.

    The vendor-neutral gateway transport is a base URL + a bearer-token
    command + a refresh TTL. When the gateway base URL and auth command are
    supplied directly (the generic-provider producer, or ucode), they are
    used verbatim. When only a Databricks profile is supplied (no override
    values), the Databricks-specific fallback derives both from
    ``~/.databrickscfg``:
      1. ~/.databrickscfg profile credentials
      2. ~/.databrickscfg (explicit profile, DEFAULT, or first valid section)
    Returns an empty dict if no credentials are available.

    The bearer token itself is not returned. Claude Code receives an
    invocation-local ``apiKeyHelper`` setting and refresh TTL instead, so
    the CLI can periodically re-run the auth command during long sessions
    instead of inheriting a one-hour token snapshot.

    :param profile: Optional Databricks profile name from
        ``~/.databrickscfg`` (used only on the profile-derivation fallback).
    :param host_override: Gateway workspace host origin, e.g.
        ``"https://example.databricks.com"``. When set, skips
        ``~/.databrickscfg`` host lookup and requires the gateway base URL
        and auth command values.
    :param base_url_override: When set, use this as ``ANTHROPIC_BASE_URL``
        instead of deriving it from the profile host.  Populated from
        ``HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL``.
    :param auth_command_override: Shell command that prints a bearer token,
        e.g. ``"databricks auth token --host ..."`` or ``"printf %s sk-..."``.
    :param auth_refresh_interval_ms: Refresh TTL in milliseconds, e.g.
        ``900000``.
    :returns: Environment values plus an internal apiKeyHelper command
        consumed by :meth:`ClaudeSDKExecutor.run_turn`, or ``{}`` when
        no credentials are available.
    :raises OSError: If a gateway host is present but missing the
        corresponding base URL or auth command.
    """
    host = host_override.rstrip("/") if host_override else None
    if host is None:
        try:
            from ..databricks_executor import _read_databrickscfg

            creds = _read_databrickscfg(profile)
        except ImportError:
            creds = None

        if creds is None:
            return {}
        host = creds.host.rstrip("/")
        base_url = (
            base_url_override if base_url_override is not None else f"{host}/ai-gateway/anthropic"
        )
        auth_command = (
            auth_command_override
            if auth_command_override is not None
            else _databricks_claude_auth_command(host, profile)
        )
    else:
        if base_url_override is None:
            raise OSError(
                "ClaudeSDKExecutor(gateway=True) with a gateway workspace host "
                "requires HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL."
            )
        if auth_command_override is None:
            raise OSError(
                "ClaudeSDKExecutor(gateway=True) with a gateway workspace host "
                "requires HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND."
            )
        base_url = base_url_override
        auth_command = auth_command_override

    return {
        "ANTHROPIC_BASE_URL": base_url,
        "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": str(
            auth_refresh_interval_ms or _GATEWAY_AUTH_REFRESH_MS
        ),
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        _CLAUDE_API_KEY_HELPER_ENV_KEY: auth_command,
    }

def _databricks_claude_auth_command(host: str, profile: str | None = None) -> str:
    """Return the legacy Databricks CLI auth helper command for Claude.

    :param host: Databricks workspace host, e.g.
        ``"https://example.databricks.com"``.
    :param profile: Optional ``~/.databrickscfg`` profile name, e.g.
        ``"oss"``. Preferred over ``--host`` when known: two profiles can
        share one host, which makes ``databricks auth token --host`` fail
        ("Use --profile to specify which profile") → empty token → 401.
        ``--profile`` is always unambiguous.
    :returns: Shell command that prints a bearer token.
    """
    # --profile is unambiguous; --host fails when two profiles share a host.
    selector = f"--profile {json.dumps(profile)}" if profile else f"--host {json.dumps(host)}"
    # `--force-refresh` proactively refreshes a still-valid cached token
    # (guards against a mid-session 401 on long gateway connections) but
    # only exists in Databricks CLI >= v0.296.0. Probe `--help` and pass it
    # only when supported: older CLIs reject the unknown flag → empty token
    # → silent 401. Plain `auth token` still auto-refreshes expired tokens.
    return (
        'if [ -n "${DATABRICKS_BEARER:-}" ]; then '
        'printf "%s\\n" "$DATABRICKS_BEARER"; '
        "else force=''; "
        "if databricks auth token --help 2>&1 | grep -q force-refresh; "
        "then force=--force-refresh; fi; "
        "env -u DATABRICKS_CONFIG_PROFILE "
        f"databricks auth token {selector} "
        "$force --output json | jq -r '.access_token'; fi"
    )

def _parse_optional_int(value: str | None) -> int | None:
    """Parse an optional integer env-var value.

    :param value: Raw env-var value, e.g. ``"900000"``.
    :returns: Parsed integer, or ``None`` when unset or invalid.
    """
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        logger.warning("Ignoring invalid integer value %r", value)
        return None

def _claude_internal_write_roots() -> list[pathlib.Path]:
    """Writable roots the Claude CLI needs for its own local session state."""

    roots = [
        pathlib.Path.home() / ".claude" / "backups",
        pathlib.Path.home() / ".claude" / "plugins",
        pathlib.Path.home() / ".claude" / "session-env",
        pathlib.Path.home() / ".claude" / "sessions",
        pathlib.Path.home() / ".npm" / "_logs",
        pathlib.Path(tempfile.gettempdir()) / f"claude-{os.getuid()}",
    ]
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
    return roots

def _claude_internal_write_files() -> list[pathlib.Path]:
    """Exact files the Claude CLI updates outside its writable roots."""

    path = pathlib.Path.home() / ".claude.json"
    return [path] if path.exists() else []

def prepare_claude_cli_path(
    real_cli_path: str | None,
    spec: OSEnvSpec | None,
) -> PreparedClaudeCli:
    """Wrap the Claude CLI in the agent's configured sandbox when possible.

    :param real_cli_path: Absolute path to the system-installed Claude CLI
        binary, or ``None`` when no CLI is available.
    :param spec: The agent's ``os_env`` spec.  Only ``caller_process`` specs
        with a compatible sandbox are eligible for wrapping.
    :returns: A :class:`PreparedClaudeCli` naming the effective CLI path and
        whether native tools should be enabled.
    """

    if real_cli_path is None or spec is None or spec.type != "caller_process":
        return PreparedClaudeCli(cli_path=real_cli_path, enable_native_tools=False)

    if _sandbox_disabled_by_env():
        return PreparedClaudeCli(cli_path=real_cli_path, enable_native_tools=False)

    sandbox_spec = spec.sandbox or OSEnvSandboxSpec()
    if sandbox_spec.type == "none":
        return PreparedClaudeCli(cli_path=real_cli_path, enable_native_tools=True)

    cwd = pathlib.Path(spec.cwd or os.getcwd()).resolve(strict=False)
    sandbox = resolve_sandbox(spec, cwd)
    if not sandbox.active:
        return PreparedClaudeCli(cli_path=real_cli_path, enable_native_tools=False)
    if not sandbox.allow_network:
        # The Claude CLI itself must reach the provider, so we cannot run the
        # whole native-tool process tree inside a network-denying sandbox.
        return PreparedClaudeCli(cli_path=real_cli_path, enable_native_tools=False)

    sandbox = with_additional_read_roots(sandbox, _claude_internal_write_roots())
    sandbox = with_additional_write_roots(sandbox, _claude_internal_write_roots())
    sandbox = with_additional_write_files(sandbox, _claude_internal_write_files())
    return PreparedClaudeCli(
        cli_path=create_exec_launcher(real_cli_path, sandbox),
        enable_native_tools=True,
    )

def prepare_tight_cli_process_path(
    real_cli_path: str | None,
    *,
    cwd: str | None = None,
) -> str | None:
    """Wrap the Claude CLI in a tight default sandbox without enabling tools."""

    if real_cli_path is None:
        return None

    if _sandbox_disabled_by_env():
        return real_cli_path

    # Skip silently on non-Linux: the implicit default sandbox here is
    # ``linux_bwrap`` and ``resolve_sandbox`` would raise
    # NotImplementedError / OSError on every macOS / Windows run. The
    # operator's only recourse is to either accept the no-op or
    # set ``os_env.sandbox.type='none'`` explicitly — both
    # already produce the same behavior we land on here, so
    # logging a warning every run is just noise (and breaks
    # tests that assert ``stderr_is_clean``).
    if sys.platform != "linux":
        return real_cli_path

    spec = OSEnvSpec(
        type="caller_process",
        cwd=cwd,
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            write_paths=[],
            allow_network=True,
        ),
    )
    try:
        resolved_cwd = pathlib.Path(cwd or os.getcwd()).resolve(strict=False)
        sandbox = resolve_sandbox(spec, resolved_cwd)
    except (OSError, NotImplementedError) as exc:
        logger.warning(
            "Could not apply default local CLI sandbox; continuing without it: %s",
            exc,
        )
        return real_cli_path

    if not sandbox.active:
        return real_cli_path
    sandbox = with_additional_write_roots(sandbox, _claude_internal_write_roots())
    sandbox = with_additional_write_files(sandbox, _claude_internal_write_files())
    return create_exec_launcher(real_cli_path, sandbox)


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _content as _sib_content
    from . import _executor as _sib_executor
    from . import _mcp as _sib_mcp
    from . import _process as _sib_process
    from . import _protocols as _sib_protocols
    from . import _types as _sib_types
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
    for _key, _value in _sib_types.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
