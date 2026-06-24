"""MCP server connections with tool discovery and caching.

``McpServerConnection`` wraps a single MCP server (stdio or HTTP)
and exposes ``connect()`` / ``call_tool()`` / ``close()``. Each
connection runs a long-lived lifecycle task that owns the
transport + ``ClientSession`` for the connection's full lifespan
so resource teardown happens on the same task that opened them
(anyio cancel-scope identity).

The runner's :class:`omnigent.runner.mcp_manager.RunnerMcpManager`
is the only production consumer; see designs/RUNNER_MCP.md.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol, TypeVar, runtime_checkable

from anyio.streams.memory import (
    MemoryObjectReceiveStream,
    MemoryObjectSendStream,
)
from cachetools import TTLCache
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.exceptions import McpError
from mcp.shared.message import SessionMessage
from mcp.types import (
    CONNECTION_CLOSED,
    CallToolRequest,
    CallToolRequestParams,
    CallToolResult,
    ClientRequest,
    ContentBlock,
    ElicitRequestParams,
    ElicitResult,
)
from mcp.types import Tool as McpToolDef

from omnigent.runner.identity import strip_runner_auth_secrets
from omnigent.spec.types import MCPOAuthConfig, MCPServerConfig, RetryPolicy
from omnigent.tools.mcp_auth import DEFAULT_MCP_AUTH_REGISTRY
from omnigent.tools.result_formatter import DEFAULT_TOOL_RESULT_FORMATTER

_T = TypeVar("_T")

# Type aliases for the (read, write) stream pair returned by MCP
# transports. Uses anyio's concrete stream types parameterized
# over the MCP session message type.
_ReadStream = MemoryObjectReceiveStream[SessionMessage | Exception]
_WriteStream = MemoryObjectSendStream[SessionMessage]

_logger = logging.getLogger(__name__)


# Default retry policy for MCP connection-level reconnection
# (transport died, server crashed). Separate from the tool-level
# retry in workflow.py, which handles call timeouts. These
# defaults give a flaky server ~6s to restart (1s + 2s + 4s
# backoff with jitter across 3 reconnect attempts).
_MCP_RECONNECT_DEFAULTS = RetryPolicy(
    max_retries=2,
    backoff_base_s=1.0,
    backoff_max_s=10.0,
)

# Circuit breaker: trips after this many consecutive exhausted
# call_tool invocations (each of which already retried
# max_retries reconnections). 5 failures × 3 reconnects each
# = 15 total reconnect attempts before the breaker trips.
_CIRCUIT_BREAKER_THRESHOLD = 5


def _resolve_databricks_token(profile: str) -> str:
    """
    Resolve an OAuth bearer token from a Databricks config profile.

    Uses the Databricks SDK's ``WorkspaceClient`` to read
    ``~/.databrickscfg`` and obtain a fresh token. The client is
    NOT cached here — token resolution happens once per
    ``connect()`` call and the token is short-lived (typically 1h).

    :param profile: Databricks config profile name, e.g.
        ``"<your-profile>"``.
    :returns: A bearer token string.
    :raises ImportError: If ``databricks-sdk`` is not installed.
    :raises RuntimeError: If the profile cannot resolve a token
        (bad profile, expired credentials, network error).
    """
    try:
        from databricks.sdk import WorkspaceClient
    except ImportError:
        raise ImportError(
            "databricks-sdk is required for MCP Databricks auth (pip install databricks-sdk)"
        ) from None

    try:
        client = WorkspaceClient(profile=profile)
        result = client.config.authenticate()
        # SDK returns either a dict (newer versions) or a callable
        # that produces headers (older versions).
        headers: dict[str, str] = result if isinstance(result, dict) else result(None)  # type: ignore[assignment]
        auth_header = headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[len("Bearer ") :]
        # Some auth flows return the token directly.
        return auth_header
    except Exception as exc:
        raise RuntimeError(
            f"Failed to resolve Databricks token from profile {profile!r}: {exc}"
        ) from exc


# Cache of minted OAuth client-credentials tokens, keyed by the
# distinguishing parts of the request: (token_url, client_id, resource,
# scopes). Value is (access_token, expiry_epoch). Module-level so reconnects
# and sibling connections to the same MCP reuse a still-valid token instead of
# minting one per connect. Refreshed when within _OAUTH_REFRESH_SKEW of expiry.
_oauth_token_cache: dict[tuple[str, str, str | None, tuple[str, ...]], tuple[str, float]] = {}

# Refresh a cached token this many seconds before it actually expires, so an
# in-flight connection never presents a token that lapses mid-handshake.
_OAUTH_REFRESH_SKEW = 60.0


def _resolve_oauth_token(oauth: "MCPOAuthConfig") -> str:
    """
    Mint (or reuse a cached) OAuth 2.0 bearer token via ``client_credentials``.

    Lets a headless agent authenticate to an OAuth-protected MCP server
    (e.g. an OpenIddict resource server like ByteDesk.Mcp) without a human
    login. POSTs the ``client_credentials`` grant to ``oauth.token_url``,
    caches the token until shortly before its ``expires_in``, and refreshes it
    on the next call after that. Mirrors the ``_resolve_databricks_token`` path
    but is provider-agnostic.

    :param oauth: The OAuth client-credentials config.
    :returns: A bearer token string.
    :raises RuntimeError: If the token endpoint errors or returns no token.
    """
    key = (oauth.token_url, oauth.client_id, oauth.resource, tuple(oauth.scopes))
    cached = _oauth_token_cache.get(key)
    now = time.time()
    if cached is not None and cached[1] - now > _OAUTH_REFRESH_SKEW:
        return cached[0]

    form: dict[str, str] = {
        "grant_type": "client_credentials",
        "client_id": oauth.client_id,
    }
    if oauth.client_secret:
        form["client_secret"] = oauth.client_secret
    if oauth.scopes:
        form["scope"] = " ".join(oauth.scopes)
    if oauth.resource:
        form["resource"] = oauth.resource

    try:
        import httpx

        resp = httpx.post(
            oauth.token_url,
            data=form,
            headers={"Accept": "application/json"},
            timeout=30.0,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 — surface a clear connection-time error
        raise RuntimeError(
            f"Failed to mint OAuth client-credentials token from "
            f"{oauth.token_url!r} (client {oauth.client_id!r}): {exc}"
        ) from exc

    token = payload.get("access_token")
    if not token:
        raise RuntimeError(
            f"OAuth token endpoint {oauth.token_url!r} returned no access_token "
            f"(keys: {sorted(payload)})"
        )
    # expires_in is seconds-from-now; default to a conservative 5min when the
    # server omits it so we still refresh rather than caching forever.
    try:
        expires_in = float(payload.get("expires_in", 300))
    except (TypeError, ValueError):
        expires_in = 300.0
    _oauth_token_cache[key] = (token, now + expires_in)
    return token


# RFC 8693 grant + subject-token-type URNs for the token-exchange (OBO) flow.
_TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
_ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"

# Cache of minted on-behalf-of (token-exchange) tokens. Keyed like the
# client-credentials cache but with the subject_token folded in, since the OBO
# token's subject is the exchanged user's token: (token_url, client_id,
# subject_token, resource, scopes). Value is (access_token, expiry_epoch).
_token_exchange_cache: dict[
    tuple[str, str, str, str | None, tuple[str, ...]], tuple[str, float]
] = {}


def _resolve_token_exchange_token(oauth: MCPOAuthConfig, subject_token: str) -> str:
    """
    Mint (or reuse a cached) on-behalf-of bearer via RFC 8693 token-exchange.

    Exchanges the user's *subject_token* (their OpenIddict MCP access token) for
    an OBO access token at ``oauth.token_url`` while authenticating as the agent
    client (``oauth.client_id`` / ``oauth.client_secret``). The returned token's
    ``sub`` is the user and ``act_sub`` is the agent — the actor is conveyed by
    the authenticated client, so *subject_token* is the load-bearing input.
    Caches the token until shortly before its ``expires_in``; mirrors
    :func:`_resolve_oauth_token` but with the token-exchange grant.

    :param oauth: The OAuth config (token endpoint + agent client credentials).
    :param subject_token: The user's access token to exchange.
    :returns: An on-behalf-of bearer token string.
    :raises RuntimeError: If the token endpoint errors or returns no token.
    """
    key = (oauth.token_url, oauth.client_id, subject_token, oauth.resource, tuple(oauth.scopes))
    cached = _token_exchange_cache.get(key)
    now = time.time()
    if cached is not None and cached[1] - now > _OAUTH_REFRESH_SKEW:
        return cached[0]

    form: dict[str, str] = {
        "grant_type": _TOKEN_EXCHANGE_GRANT,
        "client_id": oauth.client_id,
        "subject_token": subject_token,
        "subject_token_type": _ACCESS_TOKEN_TYPE,
    }
    if oauth.client_secret:
        form["client_secret"] = oauth.client_secret
    if oauth.scopes:
        form["scope"] = " ".join(oauth.scopes)
    if oauth.resource:
        form["resource"] = oauth.resource

    try:
        import httpx

        resp = httpx.post(
            oauth.token_url,
            data=form,
            headers={"Accept": "application/json"},
            timeout=30.0,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        raise RuntimeError(
            f"Failed to mint OAuth token-exchange token from "
            f"{oauth.token_url!r} (client {oauth.client_id!r}): {exc}"
        ) from exc

    token = payload.get("access_token")
    if not token:
        raise RuntimeError(
            f"OAuth token endpoint {oauth.token_url!r} returned no access_token "
            f"(keys: {sorted(payload)})"
        )
    try:
        expires_in = float(payload.get("expires_in", 300))
    except (TypeError, ValueError):
        expires_in = 300.0
    _token_exchange_cache[key] = (token, now + expires_in)
    return token


# Seconds to wait after tripping before allowing a single
# half-open probe. Long enough that a restarting server has
# time to come back; short enough that recovery isn't delayed
# excessively.
_CIRCUIT_BREAKER_COOLDOWN_SECONDS = 30.0


class McpServerDisabledError(Exception):
    """
    Raised when the circuit breaker has tripped for an MCP server.

    Indicates that the server has failed too many consecutive times
    and is temporarily disabled. The caller should not retry
    immediately — the breaker will automatically allow a probe
    after the cooldown period elapses.

    :param server_name: The MCP server name, e.g. ``"github"``.
    :param consecutive_failures: How many consecutive call_tool
        invocations have failed, e.g. ``5``.
    :param cooldown_remaining: Seconds until the next probe is
        allowed, e.g. ``22.5``.
    """

    def __init__(
        self,
        server_name: str,
        consecutive_failures: int,
        cooldown_remaining: float,
    ) -> None:
        """
        :param server_name: The MCP server name, e.g. ``"github"``.
        :param consecutive_failures: Number of consecutive failures
            that triggered the breaker.
        :param cooldown_remaining: Seconds remaining in the cooldown
            period before a probe is allowed.
        """
        self.server_name = server_name
        self.consecutive_failures = consecutive_failures
        self.cooldown_remaining = cooldown_remaining
        super().__init__(
            f"MCP server {server_name!r} is temporarily disabled "
            f"after {consecutive_failures} consecutive failures. "
            f"Will allow a probe in {cooldown_remaining:.0f}s."
        )


class McpElicitationRequired(Exception):
    """
    Raised when an MCP server returns ``InputRequiredResult`` (MRTR).

    The server needs user input (e.g. approval, form data) before it
    can execute the tool. The caller should surface the elicitation
    to the user, gather their response, and retry ``call_tool`` with
    the user's ``inputResponses`` and the opaque ``requestState``.

    :param input_requests: The ``inputRequests`` map from the
        ``InputRequiredResult``, keyed by server-assigned id.
        Values are request objects (e.g. ``elicitation/create``
        params).
    :param request_state: The opaque ``requestState`` string from
        the server. Must be echoed back verbatim on retry.
    :param tool_name: The tool that was called, e.g. ``"get_me"``.
    :param arguments: The original tool arguments dict.
    """

    def __init__(
        self,
        input_requests: dict[str, Any],
        request_state: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None:
        """
        :param input_requests: Server's ``inputRequests`` map.
        :param request_state: Opaque state to echo on retry.
        :param tool_name: The tool name, e.g. ``"get_me"``.
        :param arguments: The original tool arguments.
        """
        self.input_requests = input_requests
        self.request_state = request_state
        self.tool_name = tool_name
        self.arguments = arguments
        super().__init__(f"MCP server requires user input before executing tool {tool_name!r}")


@dataclass
class _CircuitBreaker:
    """
    Per-server circuit breaker that trips after repeated failures.

    Tracks consecutive ``call_tool`` failures (where each call has
    already exhausted its reconnect retries). After
    ``failure_threshold`` consecutive failures, the breaker trips
    and rejects calls immediately for ``cooldown_seconds``. After
    the cooldown, one probe call is allowed (half-open state): if
    it succeeds, the breaker resets; if it fails, it re-trips.

    Three states:

    - **CLOSED**: Normal operation — calls proceed.
    - **OPEN**: Tripped — calls fail immediately with
      :class:`McpServerDisabledError`.
    - **HALF-OPEN**: Cooldown elapsed — one probe call allowed.

    :param failure_threshold: Number of consecutive failures before
        tripping, e.g. ``5``.
    :param cooldown_seconds: Seconds to stay open before allowing
        a half-open probe, e.g. ``30.0``.
    """

    failure_threshold: int
    cooldown_seconds: float
    _consecutive_failures: int = field(default=0, init=False, repr=False)
    _tripped_at: float | None = field(default=None, init=False, repr=False)

    def pre_call(self, server_name: str) -> None:
        """
        Check whether a call is allowed.

        In CLOSED state, always allows. In OPEN state, raises
        :class:`McpServerDisabledError`. In HALF-OPEN state
        (cooldown elapsed), allows one probe call.

        :param server_name: The MCP server name for error messages,
            e.g. ``"github"``.
        :raises McpServerDisabledError: If the breaker is OPEN.
        """
        if self._tripped_at is None:
            return
        elapsed = time.monotonic() - self._tripped_at
        if elapsed < self.cooldown_seconds:
            raise McpServerDisabledError(
                server_name=server_name,
                consecutive_failures=self._consecutive_failures,
                cooldown_remaining=self.cooldown_seconds - elapsed,
            )
        # Half-open: cooldown elapsed, allow exactly one probe.
        # Clear _tripped_at so concurrent callers see CLOSED and
        # don't also enter the probe path. If the probe fails,
        # record_failure() will re-trip the breaker.
        self._tripped_at = None

    def record_success(self) -> None:
        """
        Reset the breaker after a successful call.

        Clears the failure counter and un-trips the breaker,
        returning to CLOSED state.
        """
        self._consecutive_failures = 0
        self._tripped_at = None

    def record_failure(self, server_name: str) -> None:
        """
        Record a failed call and trip if threshold reached.

        Increments the consecutive failure counter. If the counter
        reaches ``failure_threshold``, trips the breaker by
        recording the current monotonic time.

        :param server_name: The MCP server name for log messages,
            e.g. ``"github"``.
        """
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._tripped_at = time.monotonic()
            _logger.warning(
                "Circuit breaker tripped for MCP server %r after %d consecutive "
                "failures — disabling for %.0fs",
                server_name,
                self._consecutive_failures,
                self.cooldown_seconds,
            )

    @property
    def consecutive_failures(self) -> int:
        """
        Current consecutive failure count.

        :returns: Number of consecutive failures since the last
            success or reset.
        """
        return self._consecutive_failures

    @property
    def is_tripped(self) -> bool:
        """
        Whether the breaker is currently in OPEN state.

        Returns ``True`` only if tripped AND cooldown has not
        elapsed (i.e. not yet half-open).

        :returns: ``True`` if the breaker is open and blocking
            calls.
        """
        if self._tripped_at is None:
            return False
        return (time.monotonic() - self._tripped_at) < self.cooldown_seconds


# Default discovery cache TTL in seconds (5 minutes).
_DEFAULT_CACHE_TTL_SECONDS = 300

# Maximum number of MCP server discovery results to cache.
# Each entry is lightweight (a list of tool definitions), so 64
# is generous for any realistic deployment.
_DEFAULT_CACHE_MAX_SIZE = 64

# Module-level discovery cache: bounded LRU with TTL expiration
# (via cachetools.TTLCache). Keyed by a stable server identity
# string (see _cache_key). Survives across ToolManager instances
# so sequential workflow executions against the same agent avoid
# redundant tools/list round-trips.
_discovery_cache: TTLCache[str, list[McpToolDef]] = TTLCache(
    maxsize=_DEFAULT_CACHE_MAX_SIZE,
    ttl=_DEFAULT_CACHE_TTL_SECONDS,
)


def _cache_key(config: MCPServerConfig, cwd: Path | None = None) -> str:
    """
    Build a stable cache key for an MCP server config + stdio cwd.

    Keys include the transport + identifying fields so that two
    configs pointing at the same live server share the cache
    entry and two configs pointing at different servers (same
    name, different url / command / cwd) don't collide.

    :param config: The MCP server configuration.
    :param cwd: Optional stdio subprocess working directory;
        relative ``command`` paths resolve against it.
    :returns: A string suitable as a dict key.
    """
    if config.transport == "stdio":
        args_part = shlex.join(config.args) if config.args else ""
        cwd_part = "" if cwd is None else str(cwd)
        return f"stdio:{config.name}:{config.command}:{args_part}:{cwd_part}"
    return f"http:{config.name}:{config.url}"


def clear_discovery_cache() -> None:
    """
    Clear all cached MCP tool discovery results.

    Useful in tests to ensure a clean state.
    """
    _discovery_cache.clear()


@dataclass
class McpServerConnection:
    """
    Manages the lifecycle of a single MCP server connection.

    Uses the HTTP (SSE) transport. On ``connect()``, establishes
    the transport, initializes the MCP session, and discovers
    tools (from cache if fresh, otherwise via ``tools/list``).
    On ``close()``, tears down the session and transport.

    :param config: The MCP server configuration from the agent
        spec, e.g. ``MCPServerConfig(name="github",
        transport="http", url="https://mcp.example.com/sse")``.
    :param cwd: Optional working directory for stdio MCP
        subprocesses. Ignored for HTTP transport.
    """

    config: MCPServerConfig
    cwd: Path | None = None
    # BDP-2434: the originating user's outbound access token. When set AND the
    # server config carries an ``oauth`` block, this connection presents an
    # on-behalf-of (RFC 8693 token-exchange) bearer instead of the
    # ``client_credentials`` bearer — so a ByteDesk.Mcp call acts *as* the user.
    # ``None`` (the default) ⇒ today's identity-blind egress, byte-identical.
    subject_token: str | None = None
    # Elicitation callback invoked when the MCP server sends
    # ``elicitation/create`` inline during a ``tools/call``.
    # Receives ``(session_id, params)`` and returns an
    # ``ElicitResult``. When ``None``, inline elicitations are
    # declined by the SDK's default handler.
    elicitation_callback: Callable[[str, ElicitRequestParams], Awaitable[ElicitResult]] | None = (
        field(default=None, repr=False)
    )
    # Guards concurrent tool calls so ``_active_session_id`` is
    # safe to read in the elicitation handler (which runs on the
    # SDK's receive-loop task, not the caller's task).
    _call_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    # Session id for the in-flight tool call, set under
    # ``_call_lock`` so only one call is active at a time.
    _active_session_id: str | None = field(default=None, init=False, repr=False)
    _session: ClientSession | None = field(default=None, init=False, repr=False)
    # Long-lived task that owns the transport + session + their
    # AsyncExitStack. Must be the SAME task that runs the stack's
    # ``__aexit__`` — anyio's stdio_client / sse_client cancel
    # scopes (and ClientSession's internal task group) raise
    # "Attempted to exit cancel scope in a different task than it
    # was entered in" otherwise, which is exactly what happens
    # when ``connect()`` and ``close()`` arrive on different
    # ``asyncio.run_coroutine_threadsafe`` invocations of the
    # ``EventLoopThread``. The lifecycle task signals ready via
    # ``_ready_future`` once the session is initialized and tools
    # are discovered, then awaits ``_close_event`` so resources
    # stay alive across ``call_tool`` invocations from sibling
    # tasks.
    _lifecycle_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _close_event: asyncio.Event | None = field(default=None, init=False, repr=False)
    _ready_future: asyncio.Future[list[McpToolDef]] | None = field(
        default=None, init=False, repr=False
    )
    _discovered_tools: list[McpToolDef] = field(default_factory=list, init=False, repr=False)
    _breaker: _CircuitBreaker = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """
        Initialize the circuit breaker with module-level defaults.
        """
        self._breaker = _CircuitBreaker(
            failure_threshold=_CIRCUIT_BREAKER_THRESHOLD,
            cooldown_seconds=_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
        )

    async def connect(self) -> list[McpToolDef]:
        """
        Establish the MCP connection and discover tools.

        Schedules :meth:`_run_lifecycle` as a long-lived task on
        the running event loop. The lifecycle task opens the
        transport + ClientSession in a single ``async with``
        block, signals ready, then blocks on the close event so
        that resource teardown happens on the same task that
        opened them — required by anyio's cancel-scope identity
        check.

        :returns: List of MCP tool definitions exposed by this
            server (cached or freshly discovered).
        :raises Exception: Any failure during transport open,
            session initialize, or tool discovery is propagated
            here via the ready future.
        """
        loop = asyncio.get_running_loop()
        self._ready_future = loop.create_future()
        self._close_event = asyncio.Event()
        self._lifecycle_task = asyncio.create_task(self._run_lifecycle())
        return await self._ready_future

    async def call_tool(
        self,
        name: str,
        # Values are Any because MCP tool arguments are JSON
        # objects with heterogeneous value types (str, int,
        # bool, nested dicts, etc.). Matches the MCP SDK's
        # own ClientSession.call_tool() signature.
        arguments: dict[str, Any],
        session_id: str | None = None,
    ) -> str:
        """
        Invoke a tool on this MCP server.

        Checks the circuit breaker before attempting the call.
        If the breaker is tripped (too many consecutive failures),
        raises :class:`McpServerDisabledError` immediately. On
        success, resets the breaker. On failure (after exhausting
        reconnect retries), records the failure — tripping the
        breaker if the threshold is reached.

        :param name: The tool name as returned by discovery.
        :param arguments: The tool arguments dict (already parsed
            from the LLM's JSON string).
        :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
            Forwarded to ``_invoke_tool`` for inline elicitation
            context. ``None`` when no session is available.
        :returns: The tool result as a string. For multi-content
            results, text blocks are joined with newlines.
        :raises RuntimeError: If ``connect()`` has not been called.
        :raises McpServerDisabledError: If the circuit breaker is
            tripped.
        """
        if self._session is None:
            raise RuntimeError(
                f"MCP server {self.config.name!r} has no live "
                f"session — call connect() before call_tool()"
            )
        self._breaker.pre_call(self.config.name)
        retry = self.config.retry or _MCP_RECONNECT_DEFAULTS
        try:
            result = await _call_tool_with_reconnect(
                conn=self,
                name=name,
                arguments=arguments,
                retry=retry,
                session_id=session_id,
            )
        except Exception:
            self._breaker.record_failure(self.config.name)
            raise
        self._breaker.record_success()
        return result

    async def _invoke_tool(
        self,
        name: str,
        arguments: dict[str, Any],  # JSON values — see call_tool
        session_id: str | None = None,
    ) -> str:
        """
        Send a single ``tools/call`` request to the MCP session.

        Acquires :attr:`_call_lock` and sets
        :attr:`_active_session_id` for the duration of the call so
        the inline elicitation handler knows which session to
        surface the approval on. The lock serializes concurrent
        calls from different sessions on the same shared connection.

        When the MCP server returns an ``InputRequiredResult``
        (MRTR pattern, ``resultType == "input_required"``), raises
        :class:`McpElicitationRequired` so the caller (runner
        ``/mcp/execute`` → Omnigent server) can surface the elicitation
        to the user and retry with ``inputResponses``.

        :param name: The tool name.
        :param arguments: The tool arguments dict.
        :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
            Set on the connection for the inline elicitation handler.
        :returns: The formatted tool result string.
        :raises McpElicitationRequired: When the MCP server returns
            an ``InputRequiredResult`` requiring user input before
            the tool can execute.
        """
        if self._session is None:
            raise RuntimeError("MCP session not initialized — call connect() first")
        async with self._call_lock:
            self._active_session_id = session_id
            try:
                result = await self._session.call_tool(name=name, arguments=arguments)
            finally:
                self._active_session_id = None

        # ── MRTR: detect InputRequiredResult ─────────────────────────
        extras = getattr(result, "model_extra", {}) or {}
        if extras.get("resultType") == "input_required":
            raise McpElicitationRequired(
                input_requests=extras.get("inputRequests") or {},
                request_state=extras.get("requestState", ""),
                tool_name=name,
                arguments=arguments,
            )

        return _format_call_result(result)

    async def call_tool_with_elicitation(
        self,
        name: str,
        arguments: dict[str, Any],
        input_responses: dict[str, Any] | None = None,
        request_state: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """
        Retry a ``tools/call`` with MRTR ``inputResponses``.

        Called by the Omnigent server after the user has responded to an
        ``InputRequiredResult`` elicitation. Sends a new
        ``tools/call`` with the user's ``inputResponses`` and the
        opaque ``requestState`` per the MCP MRTR spec.

        :param name: The tool name, e.g. ``"get_me"``.
        :param arguments: The original tool arguments dict.
        :param input_responses: The user's responses keyed by
            elicitation id, e.g.
            ``{"eid_1": {"action": "accept",
            "content": {"decision": "allow"}}}``.
        :param request_state: The opaque ``requestState`` from the
            ``InputRequiredResult``. Echoed back verbatim.
        :returns: The formatted tool result string.
        :raises RuntimeError: If ``connect()`` has not been called.
        """
        if self._session is None:
            raise RuntimeError("MCP session not initialized — call connect() first")

        retry_params: dict[str, Any] = {
            "name": name,
            "arguments": arguments,
        }
        if input_responses is not None:
            retry_params["inputResponses"] = input_responses
        if request_state:
            retry_params["requestState"] = request_state

        async with self._call_lock:
            self._active_session_id = session_id
            try:
                result = await self._session.send_request(
                    ClientRequest(
                        CallToolRequest(
                            params=CallToolRequestParams(**retry_params),
                        )
                    ),
                    CallToolResult,
                )
            finally:
                self._active_session_id = None

        # Multi-round MRTR: the server may return another
        # InputRequiredResult on the retry. Raise so the Omnigent server
        # can surface the next elicitation round.
        extras = getattr(result, "model_extra", {}) or {}
        if extras.get("resultType") == "input_required":
            raise McpElicitationRequired(
                input_requests=extras.get("inputRequests") or {},
                request_state=extras.get("requestState", ""),
                tool_name=name,
                arguments=arguments,
            )
        return _format_call_result(result)

    async def _reconnect(self) -> None:
        """
        Tear down the dead session and open a fresh one.

        Called by ``call_tool()`` after detecting a connection
        error. Does not re-discover tools — the tool list from
        the original ``connect()`` is still valid, but the
        session needs to be live for the next ``call_tool``.

        Same task-identity rule as :meth:`connect` applies: the
        new lifecycle task owns the new transport + session
        end to end.
        """
        await self.close()
        loop = asyncio.get_running_loop()
        self._ready_future = loop.create_future()
        self._close_event = asyncio.Event()
        self._lifecycle_task = asyncio.create_task(self._run_lifecycle())
        await self._ready_future

    async def _run_lifecycle(self) -> None:
        """
        Long-lived task that owns the MCP connection's resources.

        Runs the entire ``open transport → initialize session →
        await close`` sequence inside a single task. The
        ``async with AsyncExitStack()`` block enters anyio's
        stdio_client / sse_client + ClientSession on this task;
        when ``_close_event`` is set, the block exits — and
        anyio's cancel scopes are torn down on the SAME task
        that entered them. That's the invariant we need to
        avoid the "Attempted to exit cancel scope in a
        different task" RuntimeError that fires when the AP
        ToolManager's ``EventLoopThread.run`` schedules
        ``connect()`` and ``close()`` as separate tasks.

        Failure modes are routed through ``_ready_future``:

        - If teardown fails *before* ready is set, the
          exception is propagated to the caller of
          :meth:`connect` (or :meth:`_reconnect`) so they see
          a real error rather than a silently-wedged
          connection.
        - If a steady-state failure occurs *after* ready, it
          is logged here — :meth:`close` already has the
          ``await lifecycle_task`` it needs to surface a
          terminal exception, but a mid-flight teardown error
          shouldn't crash the workflow.
        """
        ready = self._ready_future
        close_event = self._close_event
        # Both invariants are set by the connect / reconnect site
        # immediately before scheduling this task; assert rather
        # than branch so a regression there fails loud here.
        assert ready is not None, "ready future not initialized before lifecycle start"
        assert close_event is not None, "close event not initialized before lifecycle start"
        try:
            async with AsyncExitStack() as stack:
                read_stream, write_stream = await self._open_transport(stack)
                # Session-level read timeout applies to initialize(),
                # list_tools(), and any call_tool() that doesn't pass
                # its own per-call timeout. Falls back to the MCP SDK
                # default (no timeout) when config.timeout is None.
                session_timeout = (
                    timedelta(seconds=self.config.timeout)
                    if self.config.timeout is not None
                    else None
                )
                session = ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=session_timeout,
                    elicitation_callback=(
                        self._elicitation_handler
                        if self.elicitation_callback is not None
                        else None
                    ),
                )
                await stack.enter_async_context(session)
                await session.initialize()
                self._session = session
                discovered = await self._discover_or_use_cache()
                ready.set_result(discovered)
                # Hold transport + session open until close() signals.
                # All call_tool() invocations during this window run
                # on sibling tasks, but they only send/receive on
                # already-open anyio streams — that does not touch
                # the cancel scopes opened above.
                await close_event.wait()
            # ``async with`` exits HERE on this task → cancel scopes
            # opened by stdio_client / sse_client / ClientSession are
            # torn down by the same task that entered them. ✓
        # Lifecycle task: any failure routes to the ready future on
        # startup, or to the logger on steady state. Letting an
        # exception bubble out of the task would leave the ready
        # future never resolved and connect() would hang forever.
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
                return
            _logger.exception(
                "MCP server %r lifecycle task failed during steady state",
                self.config.name,
            )
        finally:
            self._session = None

    async def _discover_or_use_cache(
        self,
    ) -> list[McpToolDef]:
        """
        Return tool definitions from cache or live discovery.

        If the cache is fresh, returns cached definitions without
        calling ``tools/list``. Otherwise performs a live
        ``tools/list`` call and updates the cache.

        Must be called after ``_open_session()`` so that
        ``self._session`` is live.

        :returns: List of MCP tool definitions.
        """
        cached = self._check_cache()
        if cached is not None:
            self._discovered_tools = cached
            _logger.debug(
                "MCP server %r: using cached discovery (%d tools)",
                self.config.name,
                len(cached),
            )
            return cached

        if self._session is None:
            raise RuntimeError("MCP session not initialized — call connect() first")
        tools_result = await self._session.list_tools()
        self._discovered_tools = tools_result.tools
        self._update_cache(tools_result.tools)
        _logger.info(
            "MCP server %r: discovered %d tool(s)",
            self.config.name,
            len(tools_result.tools),
        )
        return tools_result.tools

    async def close(self) -> None:
        """
        Tear down the MCP session and transport.

        Signals the lifecycle task to exit its
        ``async with AsyncExitStack`` block, then awaits the
        task's completion so resource teardown is observable
        from the caller. Safe to call multiple times or if
        :meth:`connect` was never called.
        """
        if self._close_event is not None:
            self._close_event.set()
        task = self._lifecycle_task
        if task is not None and not task.done():
            # Logged inside ``_run_lifecycle``; caller (RunnerMcpManager.shutdown)
            # only cares that close() ran to completion.
            with suppress(Exception):
                await task
        self._lifecycle_task = None
        self._close_event = None
        self._ready_future = None
        self._session = None

    def _check_cache(self) -> list[McpToolDef] | None:
        """
        Return cached discovery if still fresh, else ``None``.

        TTLCache handles expiry internally — ``get()`` returns
        ``None`` for expired or absent entries.

        :returns: Cached tool list or ``None`` if expired or
            absent.
        """
        key = _cache_key(self.config, self.cwd)
        # TTLCache lacks type stubs, so .get() returns Any.
        result: list[McpToolDef] | None = _discovery_cache.get(key)
        return result

    def _update_cache(self, tools: list[McpToolDef]) -> None:
        """
        Store discovery results in the module-level cache.

        TTLCache tracks insertion time internally and evicts
        entries after the configured TTL.

        :param tools: The freshly discovered tool definitions.
        """
        key = _cache_key(self.config, self.cwd)
        _discovery_cache[key] = tools

    async def _open_transport(
        self,
        stack: AsyncExitStack,
    ) -> tuple[_ReadStream, _WriteStream]:
        """
        Open the MCP transport — HTTP (SSE) or stdio subprocess.

        The transport's async context is registered on the
        caller's exit stack so it gets torn down by the same
        task that opened it. Dispatches on
        :attr:`MCPServerConfig.transport`.

        :param stack: The lifecycle task's exit stack —
            transports register here so their cancel scopes
            unwind on the lifecycle task's exit, not on
            :meth:`close`'s caller task.
        :returns: A ``(read_stream, write_stream)`` tuple of
            anyio memory object streams parameterized over
            ``SessionMessage``.
        """
        if self.config.transport == "stdio":
            return await self._open_stdio_transport(stack)
        return await self._open_http_transport(stack)

    async def _open_http_transport(
        self,
        stack: AsyncExitStack,
    ) -> tuple[_ReadStream, _WriteStream]:
        """
        Open an HTTP MCP transport — Streamable HTTP or legacy SSE.

        Tries the Streamable HTTP transport first (the current MCP
        spec default, used by Databricks MCP gateways and newer
        servers). Falls back to legacy SSE if the Streamable HTTP
        handshake fails, so older servers still work.

        :param stack: The lifecycle task's exit stack.
        :returns: A ``(read_stream, write_stream)`` tuple of
            anyio memory object streams parameterized over
            ``SessionMessage``.
        """
        if self.config.url is None:
            # Validator prevents this at spec-load time; the assert
            # is a belt-and-suspenders check for programmatic
            # MCPServerConfig construction paths that skip the
            # validator.
            raise RuntimeError(
                f"MCP server {self.config.name!r} transport='http' but url is None — "
                "validator should have caught this"
            )
        timeout = self.config.timeout
        headers = self._resolve_http_headers()
        try:
            return await self._open_streamable_http_transport(stack, timeout, headers)
        except Exception as exc:
            _logger.debug(
                "Streamable HTTP failed for %s (%s), falling back to SSE",
                self.config.name,
                exc,
            )
            return await self._open_sse_transport(stack, timeout, headers)

    def _resolve_http_headers(self) -> dict[str, str] | None:
        """
        Build the HTTP headers for the MCP connection.

        Delegates to the ordered :data:`DEFAULT_MCP_AUTH_REGISTRY`
        (:mod:`omnigent.tools.mcp_auth`), which seeds the explicit
        config ``headers`` and applies each registered auth scheme in
        precedence order (explicit header > databricks-profile > oauth).
        Tokens are resolved fresh on each call so reconnects pick up
        rotated credentials. Register new schemes (SigV4, mTLS, API key)
        against that registry without touching this call site.

        BDP-2434 (OBO egress): when this connection carries a
        ``subject_token`` AND the server config has an ``oauth`` block, the
        Authorization header is an on-behalf-of (RFC 8693 token-exchange) bearer
        instead of the ``client_credentials`` one. An explicit ``Authorization``
        config header still wins (seeded first, OBO via ``setdefault``); the
        ``client_credentials`` mint is skipped entirely (no wasted token). No
        ``subject_token`` (or no ``oauth``) ⇒ the registry alone runs,
        byte-identical to today.

        :returns: Merged headers dict, or ``None`` if no headers
            are needed (empty config headers and no scheme applied).
        """
        if self.subject_token is not None and self.config.oauth is not None:
            # Explicit config headers keep highest precedence; the OBO bearer
            # fills an absent Authorization. The registry's oauth scheme is
            # intentionally NOT run here — it would mint a discarded
            # client-credentials token (the OBO bearer is the credential).
            headers = dict(self.config.headers) if self.config.headers else {}
            obo = _resolve_token_exchange_token(self.config.oauth, self.subject_token)
            headers.setdefault("Authorization", f"Bearer {obo}")
            return headers or None
        return DEFAULT_MCP_AUTH_REGISTRY.resolve_headers(self.config)

    async def _open_streamable_http_transport(
        self,
        stack: AsyncExitStack,
        timeout: int | None,
        headers: dict[str, str] | None,
    ) -> tuple[_ReadStream, _WriteStream]:
        """
        Open a Streamable HTTP MCP transport.

        Uses the ``streamablehttp_client`` from the MCP SDK, which
        sends JSON-RPC over HTTP POST with optional SSE streaming
        for server-initiated messages.

        :param stack: The lifecycle task's exit stack.
        :param timeout: Per-server timeout in seconds, or ``None``
            for SDK defaults.
        :param headers: Resolved HTTP headers (may include a
            Databricks bearer token), or ``None``.
        :returns: A ``(read_stream, write_stream)`` tuple.
        """
        assert self.config.url is not None
        read_stream, write_stream, _get_session_id = await stack.enter_async_context(
            streamablehttp_client(
                url=self.config.url,
                headers=headers,
                timeout=float(timeout) if timeout is not None else 30,
                sse_read_timeout=float(timeout) if timeout is not None else 300,
            )
        )
        return read_stream, write_stream

    async def _open_sse_transport(
        self,
        stack: AsyncExitStack,
        timeout: int | None,
        headers: dict[str, str] | None,
    ) -> tuple[_ReadStream, _WriteStream]:
        """
        Open a legacy SSE MCP transport.

        Falls back here when the server does not support Streamable
        HTTP (e.g. older MCP servers that only speak SSE).

        :param stack: The lifecycle task's exit stack.
        :param timeout: Per-server timeout in seconds, or ``None``
            for SDK defaults.
        :param headers: Resolved HTTP headers (may include a
            Databricks bearer token), or ``None``.
        :returns: A ``(read_stream, write_stream)`` tuple.
        """
        assert self.config.url is not None
        read_stream, write_stream = await stack.enter_async_context(
            sse_client(
                url=self.config.url,
                headers=headers,
                # MCP SDK default: 5s for initial HTTP connection handshake.
                timeout=float(timeout) if timeout is not None else 5,
                # MCP SDK default: 300s (5 min) for SSE event read.
                sse_read_timeout=float(timeout) if timeout is not None else 300,
            )
        )
        return read_stream, write_stream

    async def _open_stdio_transport(
        self,
        stack: AsyncExitStack,
    ) -> tuple[_ReadStream, _WriteStream]:
        """
        Open a stdio MCP transport — spawns the MCP server as a
        subprocess directly.

        The command runs unwrapped. Step 7 of the harness contract
        migration removed the ``srt`` sandbox wrap that previously
        gated this spawn: srt's default policy blocks outbound
        network (which every useful MCP server needs to reach its
        backend), so ``sandbox: true`` consistently produced silent
        hangs on the first ``tool/call``. Inner-stack stdio MCPs
        have always spawned without sandboxing
        (``omnigent/inner/mcp_tools.py``); Omnigent now matches that
        baseline. Per-MCP sandboxing — if reintroduced — should
        flow through the ``omnigent/environments/`` primitive
        with explicit outbound-host allowlists, not srt-defaults.

        :param stack: The lifecycle task's exit stack.
        :returns: A ``(read_stream, write_stream)`` tuple of
            anyio memory object streams parameterized over
            ``SessionMessage``.
        :raises RuntimeError: If ``command`` is None (programmatic
            construction path that bypassed the validator).
        """
        if self.config.command is None:
            # Validator prevents this at spec-load time; the assert
            # is a belt-and-suspenders check for programmatic
            # MCPServerConfig construction paths that skip the
            # validator.
            raise RuntimeError(
                f"MCP server {self.config.name!r} transport='stdio' but command is "
                "None — validator should have caught this"
            )
        params = StdioServerParameters(
            command=self.config.command,
            args=list(self.config.args),
            cwd=self.cwd,
            # ``env=None`` inherits the SDK's ``get_default_environment``
            # allowlist (no runner-auth secret). The ``config.env`` branch
            # overlays author-declared vars (e.g. ``GITHUB_TOKEN``) on the
            # full parent env, so strip the runner tunnel binding token
            # first: an MCP server command is spec-author code.
            env=(strip_runner_auth_secrets(os.environ) | self.config.env)
            if self.config.env
            else None,
        )
        read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
        return read_stream, write_stream

    async def _elicitation_handler(
        self,
        context: Any,
        params: ElicitRequestParams,
    ) -> ElicitResult:
        """
        MCP SDK elicitation callback for inline ``elicitation/create``.

        Called by the SDK's receive-loop task when the MCP server
        sends ``elicitation/create`` during a ``tools/call``.
        Delegates to :attr:`elicitation_callback` with the session
        id from :attr:`_active_session_id` (set under
        :attr:`_call_lock` by ``_invoke_tool``).

        :param context: MCP SDK ``RequestContext`` (unused).
        :param params: Elicitation params from the server.
        :returns: User verdict as an :class:`ElicitResult`.
        """
        del context
        session_id = self._active_session_id
        if session_id is None or self.elicitation_callback is None:
            _logger.warning(
                "MCP server %r: elicitation/create received but no "
                "session context or callback — declining",
                self.config.name,
            )
            return ElicitResult(action="decline")
        return await self.elicitation_callback(session_id, params)


# JSON Schema keywords that LLM providers either reject outright
# or handle inconsistently. Presence of these in an MCP tool's
# inputSchema does not block registration, but operators should
# know the tool may produce API errors at call time.
_PROBLEMATIC_SCHEMA_KEYWORDS = frozenset(
    {
        # $ref with sibling properties — OpenAI ignores siblings,
        # Anthropic rejects the combination.
        "$ref",
        # oneOf — OpenAI rejects in nested contexts (must use anyOf).
        "oneOf",
        # allOf with $ref — Anthropic rejects the combination.
        "allOf",
    }
)


@runtime_checkable
class SchemaNormalizer(Protocol):
    """
    Strategy for normalizing an MCP ``inputSchema`` for a specific
    LLM provider.

    The default (OpenAI) strategy injects a ``properties`` key and
    warns on problematic keywords without transforming them. Future
    provider strategies (e.g. an Anthropic normalizer that rewrites
    ``oneOf`` → ``anyOf`` and inlines ``$ref``) can be registered via
    :func:`register_schema_normalizer` and selected by provider.
    """

    def normalize(
        self,
        schema: dict[str, Any] | None,
        tool_name: str,
    ) -> dict[str, Any]:
        """
        Normalize *schema* for this provider.

        :param schema: The raw ``inputSchema`` dict, or ``None``.
        :param tool_name: Tool name for log messages.
        :returns: A normalized schema dict.
        """
        ...


class _OpenAISchemaNormalizer:
    """
    Default :class:`SchemaNormalizer` reproducing the historical
    OpenAI-locked behavior exactly.

    MCP allows schemas that LLM providers reject. This strategy
    applies the minimum transformations needed to avoid the most
    common real-world failures, following the approach used by
    the OpenAI Agents Python SDK:

    1. **Missing or None schema** → default to
       ``{"type": "object", "properties": {}}``. Many MCP tools
       (especially no-arg tools) omit ``inputSchema`` entirely.
    2. **Missing ``properties`` key** → inject ``"properties": {}``.
       MCP spec allows ``{"type": "object"}`` without
       ``properties``, but OpenAI rejects it (see
       openai/openai-agents-python#449).
    3. **Problematic keywords** (``$ref``, ``oneOf``, ``allOf``) →
       log a warning. This strategy does not transform these because
       inlining ``$ref`` and converting ``oneOf`` → ``anyOf`` is
       complex and lossy. Operators see the warning and can fix
       the MCP server's schema.
    """

    def normalize(
        self,
        schema: dict[str, Any] | None,
        tool_name: str,
    ) -> dict[str, Any]:
        if schema is None:
            return {"type": "object", "properties": {}}

        # MCP allows {"type": "object"} with no properties key.
        # OpenAI requires "properties" to be present, even if empty.
        if schema.get("type") == "object" and "properties" not in schema:
            schema = {**schema, "properties": {}}

        _warn_problematic_keywords(schema, tool_name)
        return schema


# Schema normalizer registry keyed by LLM provider. The "openai"
# entry is the default and must remain byte-identical to the
# historical OpenAI-locked behavior (including the warn-only handling
# of problematic keywords).
_DEFAULT_SCHEMA_NORMALIZER = "openai"
_SCHEMA_NORMALIZERS: dict[str, SchemaNormalizer] = {
    _DEFAULT_SCHEMA_NORMALIZER: _OpenAISchemaNormalizer(),
}


def register_schema_normalizer(provider: str, normalizer: SchemaNormalizer) -> None:
    """
    Register a :class:`SchemaNormalizer` for *provider*.

    Allows e.g. an Anthropic normalizer (oneOf→anyOf, $ref inlining)
    to be plugged in later without touching call sites. Re-registering
    an existing provider overwrites it.

    :param provider: Registry key, e.g. ``"anthropic"``.
    :param normalizer: A :class:`SchemaNormalizer` implementation.
    """
    _SCHEMA_NORMALIZERS[provider] = normalizer


def get_schema_normalizer(
    provider: str = _DEFAULT_SCHEMA_NORMALIZER,
) -> SchemaNormalizer:
    """
    Look up a registered :class:`SchemaNormalizer` by provider.

    :param provider: Registry key. Defaults to the OpenAI normalizer.
    :returns: The registered normalizer.
    :raises KeyError: If no normalizer is registered for *provider*.
    """
    return _SCHEMA_NORMALIZERS[provider]


def _normalize_input_schema(
    schema: dict[str, Any] | None,
    tool_name: str,
    provider: str = _DEFAULT_SCHEMA_NORMALIZER,
) -> dict[str, Any]:
    """
    Normalize an MCP ``inputSchema`` for LLM consumption.

    Delegates to a pluggable :class:`SchemaNormalizer` strategy keyed
    by LLM *provider* (default: OpenAI). The default path is
    byte-identical to the historical OpenAI-locked behavior.

    :param schema: The raw ``inputSchema`` dict from the MCP tool
        definition, or ``None`` if the tool has no parameters.
    :param tool_name: Tool name for log messages, e.g.
        ``"list_directory"``.
    :param provider: LLM provider key selecting the normalizer
        strategy. Defaults to ``"openai"``.
    :returns: A normalized schema dict safe for the selected provider.
    """
    return get_schema_normalizer(provider).normalize(schema, tool_name)


def _warn_problematic_keywords(
    schema: dict[str, Any],
    tool_name: str,
) -> None:
    """
    Log warnings for JSON Schema keywords that LLM providers
    handle poorly or reject.

    Walks the schema tree (objects, arrays, anyOf/oneOf/allOf
    branches, and $defs) to find problematic keywords at any
    nesting depth.

    :param schema: The input schema dict to inspect.
    :param tool_name: Tool name for log messages.
    """
    found = _collect_problematic_keywords(schema)
    for keyword in sorted(found):
        _logger.warning(
            "MCP tool %r schema contains %r which some LLM "
            "providers reject or handle inconsistently — "
            "the tool may fail at call time",
            tool_name,
            keyword,
        )


def _collect_problematic_keywords(
    schema: dict[str, Any],
) -> set[str]:
    """
    Recursively collect problematic JSON Schema keywords from
    a schema tree.

    :param schema: A JSON Schema dict node to inspect.
    :returns: Set of problematic keyword strings found anywhere
        in the schema tree.
    """
    found: set[str] = set()
    found.update(kw for kw in _PROBLEMATIC_SCHEMA_KEYWORDS if kw in schema)

    # Recurse into object properties.
    for prop_schema in schema.get("properties", {}).values():
        if isinstance(prop_schema, dict):
            found.update(_collect_problematic_keywords(prop_schema))

    # Recurse into array items.
    items = schema.get("items")
    if isinstance(items, dict):
        found.update(_collect_problematic_keywords(items))

    # Recurse into composition keywords (anyOf, oneOf, allOf).
    for keyword in ("anyOf", "oneOf", "allOf"):
        for branch in schema.get(keyword, []):
            if isinstance(branch, dict):
                found.update(_collect_problematic_keywords(branch))

    # Recurse into $defs / definitions.
    for defs_key in ("$defs", "definitions"):
        for def_schema in schema.get(defs_key, {}).values():
            if isinstance(def_schema, dict):
                found.update(_collect_problematic_keywords(def_schema))

    return found


# Exception types that indicate a dead/broken connection
# rather than a legitimate tool error. These are worth
# retrying after a reconnect.
_CONNECTION_ERROR_TYPES = (
    EOFError,
    BrokenPipeError,
    ConnectionError,
    OSError,
)


def _is_connection_error(exc: BaseException) -> bool:
    """
    Determine if an exception indicates a dead MCP connection.

    Returns ``True`` for transport-level failures (broken pipe,
    EOF, connection reset) and MCP-level connection-closed
    errors. Returns ``False`` for tool-level errors (invalid
    args, tool not found) which should not trigger a reconnect.

    :param exc: The exception to classify.
    :returns: ``True`` if the error is connection-related.
    """
    if isinstance(exc, _CONNECTION_ERROR_TYPES):
        return True
    if isinstance(exc, McpError):
        return exc.error.code == CONNECTION_CLOSED
    return False


def _backoff_delay(attempt: int, retry: RetryPolicy) -> float:
    """
    Compute the backoff delay for a reconnect attempt.

    Delegates to :meth:`RetryPolicy.compute_backoff_delay` which
    handles the exponential shape, ``backoff_max_s`` cap, and
    optional jitter.

    :param attempt: Zero-based retry index (0 = first retry).
    :param retry: Retry policy with backoff parameters.
    :returns: Sleep duration in seconds.
    """
    return retry.compute_backoff_delay(retry_index=attempt + 1)


async def _sleep(seconds: float) -> None:
    """
    Indirection point for the reconnect backoff sleep.

    Exists so tests can stub the retry delay without patching
    ``asyncio.sleep`` globally (patching ``omnigent.tools.mcp.asyncio.sleep``
    walks the dotted path into the real ``asyncio`` module singleton
    and leaks the mock into every other test in the process).

    :param seconds: Delay in seconds.
    """
    await asyncio.sleep(seconds)


async def _call_tool_with_reconnect(
    conn: McpServerConnection,
    name: str,
    arguments: dict[str, Any],  # JSON values — see call_tool
    retry: RetryPolicy,
    session_id: str | None = None,
) -> str:
    """
    Invoke a tool, reconnecting with backoff on connection errors.

    On a connection-level failure (dead transport, server crash),
    reconnects and retries up to ``retry.max_retries`` times with
    exponential backoff. Permanent errors (invalid args, tool not
    found) are raised immediately without retrying.

    :param conn: The MCP server connection to invoke on.
    :param name: The tool name as returned by discovery.
    :param arguments: The tool arguments dict.
    :param retry: Retry policy controlling max attempts, backoff
        base, and backoff cap.
    :param session_id: Omnigent session id forwarded to
        ``_invoke_tool`` for inline elicitation context.
    :returns: The formatted tool result string.
    """
    last_exc: Exception | None = None
    total_tries = retry.max_retries + 1

    for attempt in range(total_tries):
        try:
            return await conn._invoke_tool(name, arguments, session_id=session_id)
        except Exception as exc:
            if not _is_connection_error(exc):
                raise
            last_exc = exc
            # Last attempt — don't reconnect, just raise.
            if attempt + 1 >= total_tries:
                break
            delay = _backoff_delay(attempt, retry)
            _logger.warning(
                "MCP server %r: connection lost during tool call "
                "%r (attempt %d/%d), reconnecting in %.1fs",
                conn.config.name,
                name,
                attempt + 1,
                total_tries,
                delay,
            )
            await _sleep(delay)
            await conn._reconnect()

    # All attempts exhausted — re-raise the last connection error.
    assert last_exc is not None
    raise last_exc


def _format_call_result(result: CallToolResult) -> str:
    """
    Convert an MCP ``CallToolResult`` to a plain string.

    Extracts text content blocks and joins them. If the result
    indicates an error, prefixes the output with ``"Error: "``.

    :param result: The ``CallToolResult`` from
        ``session.call_tool()``.
    :returns: A string representation of the tool result.
        Returns ``"(empty response)"`` when the server sends no
        content blocks.
    """
    parts: list[str] = []
    for block in result.content:
        parts.append(_format_content_block(block))
    joined = "\n".join(parts)
    if not joined:
        joined = "(empty response)"
    if result.isError:
        return f"Error: {joined}"
    return joined


def _format_content_block(block: ContentBlock) -> str:
    """
    Convert a single MCP content block to a string.

    Delegates to the shared :data:`DEFAULT_TOOL_RESULT_FORMATTER` so
    the MCP content-block path and the in-process raw-value path
    (``local_callable._stringify``) render image/audio/resource blocks
    in exactly one place — see :mod:`omnigent.tools.result_formatter`.

    :param block: A content block from ``CallToolResult.content``,
        e.g. ``TextContent(type="text", text="hello")``.
    :returns: A string representation of the block.
    """
    return DEFAULT_TOOL_RESULT_FORMATTER.format_content_block(block)
