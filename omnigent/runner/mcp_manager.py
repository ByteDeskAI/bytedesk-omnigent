"""Runner-side MCP pool. See ``designs/RUNNER_MCP.md``."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from mcp.types import ElicitRequestParams, ElicitResult
from mcp.types import Tool as McpToolDef

from omnigent.spec.types import AgentSpec, MCPServerConfig
from omnigent.tools.base import is_valid_tool_name
from omnigent.tools.mcp import McpServerConnection

if TYPE_CHECKING:
    import httpx

_logger = logging.getLogger(__name__)


@runtime_checkable
class McpManager(Protocol):
    """The runner-side MCP manager seam (BDP-2348).

    Formalizes the ``mcp_manager: Any`` duck type that the runner dispatch
    sites share across the two concrete backends — the direct
    :class:`RunnerMcpManager` (local MCP connections) and the
    :class:`~omnigent.runner.proxy_mcp_manager.ProxyMcpManager` (calls routed
    through the Omnigent server MCP proxy). Both already satisfy this Protocol
    structurally; typing the carrier as :class:`McpManager` (sweep-2
    BDP-2363) lets dispatch depend on the seam, not a concrete class.

    The four methods are exactly the surface the dispatch sites call. Note that
    ``call_tool``'s signature differs between backends (the direct manager
    accepts a ``session_id`` keyword the proxy omits); the Protocol declares the
    common shape and ``runtime_checkable`` matches on method presence.
    """

    async def schemas_for(self, spec: AgentSpec) -> McpSchemasResult:
        """Resolve the MCP tool schemas available for *spec*."""
        ...

    async def call_tool(
        self,
        spec: AgentSpec | None,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """Dispatch *tool_name* with *arguments*; return the result string."""
        ...

    async def prewarm(self, spec: AgentSpec) -> None:
        """Optionally warm connections for *spec* ahead of first use."""
        ...

    async def shutdown(self) -> None:
        """Release any resources held by the manager."""
        ...


def _build_accept_content(
    params: ElicitRequestParams,
) -> dict[str, str | int | float | bool | list[str] | None] | None:
    """
    Auto-fill ``content`` from ``requestedSchema`` for an accept.

    Delegates to the shared utility in
    :func:`omnigent.tools._elicitation_schema.build_accept_content_from_schema`.

    :param params: The elicitation params from the MCP server.
    :returns: A flat content dict, or ``None``.
    """
    from omnigent.tools._elicitation_schema import build_accept_content_from_schema

    schema = getattr(params, "requestedSchema", None)
    if not schema or not isinstance(schema, dict):
        return None
    return build_accept_content_from_schema(schema)


_POOL_SPEC_CAPACITY = 8


@dataclass
class _ServerEntry:
    """One MCP server within a spec's pool entry."""

    config: MCPServerConfig
    connection: McpServerConnection | None = None
    tools: list[McpToolDef] = field(default_factory=list)
    error: str | None = None


@dataclass
class _SpecEntry:
    spec_hash: str
    servers: dict[str, _ServerEntry] = field(default_factory=dict)
    prewarm_task: asyncio.Task[None] | None = None


@dataclass(frozen=True)
class McpSchemasResult:
    """Output of :meth:`RunnerMcpManager.schemas_for`."""

    schemas: list[dict[str, Any]]
    tool_names: set[str]
    failures: dict[str, str]  # server_name → error message


def filter_schemas_by_allowlist(
    schemas: list[dict[str, Any]],
    spec: AgentSpec,
) -> list[dict[str, Any]]:
    """
    Drop MCP tool schemas not on their owning server's ``tool_allowlist``.

    Tool names in *schemas* are namespaced ``<server>__<tool>`` (applied
    by the Omnigent server MCP proxy / runner pool). For each server in
    ``spec.mcp_servers`` whose :attr:`MCPServerConfig.tool_allowlist` is
    non-empty, only that server's tools whose **bare** name is on the
    allowlist survive.

    Behavior:

    - If NO declared server has a non-empty ``tool_allowlist``, *schemas*
      is returned unchanged (zero behavior change for existing agents).
    - A schema is attributed to a server by stripping a ``<server>__``
      prefix that matches a declared server name; if names are bare AND
      there is exactly one MCP server, it is attributed to that server.
    - A schema kept iff its server has no allowlist OR its bare tool name
      is on that server's allowlist.
    - Schemas that cannot be attributed to a declared server pass through
      unchanged (fail-open) so unrelated tools are never dropped.

    :param schemas: Flat OpenAI function-tool schemas (each with a
        ``"name"`` key).
    :param spec: The agent spec carrying ``mcp_servers``.
    :returns: The filtered (or original) schema list, order preserved.
    """
    servers = list(spec.mcp_servers or [])
    allowlists: dict[str, set[str]] = {
        s.name: set(s.tool_allowlist) for s in servers if s.tool_allowlist
    }
    if not allowlists:
        return schemas

    server_names = [s.name for s in servers]
    only_server = server_names[0] if len(server_names) == 1 else None

    kept: list[dict[str, Any]] = []
    for schema in schemas:
        name = str(schema.get("name", ""))
        owner: str | None = None
        bare = name
        for sname in server_names:
            prefix = f"{sname}__"
            if name.startswith(prefix):
                owner = sname
                bare = name[len(prefix) :]
                break
        if owner is None and only_server is not None:
            # Bare names with a single declared server → attribute to it.
            owner = only_server
        if owner is None:
            # Unattributable → fail open.
            kept.append(schema)
            continue
        allow = allowlists.get(owner)
        if allow is None or bare in allow:
            kept.append(schema)
    return kept


def compute_spec_hash(configs: list[MCPServerConfig], cwd: Path | None = None) -> str:
    """Stable content hash over ``spec.mcp_servers`` (+ stdio cwd)."""
    payload = json.dumps(
        {
            "cwd": str(cwd) if cwd is not None else None,
            "servers": [
                {
                    "name": c.name,
                    "transport": c.transport,
                    "url": c.url,
                    "command": c.command,
                    "args": list(c.args or []),
                    "env": dict(c.env or {}),
                    "tools": list(c.tool_allowlist or []),
                }
                for c in configs
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _mcp_tool_schema(
    server_name: str,
    tool_def: McpToolDef,
    allowed: set[str] | None,
) -> dict[str, Any] | None:
    """Translate an MCP tool def to an OpenAI function-tool schema with a
    namespaced name; honor *allowed*.

    Tool names are returned as ``{server_name}__{tool_def.name}`` (double
    underscore separator) so tools from different MCP servers never collide,
    even when two servers expose a tool with the same bare name (e.g.
    ``search``).  The caller is responsible for stripping the prefix before
    dispatching to the MCP server (see ``RunnerMcpManager.call_tool``).

    Returns ``None`` when the tool is filtered out by *allowed* or the bare
    tool name is invalid (must match ``^[a-zA-Z0-9_-]{1,256}$``).

    :param server_name: Config name of the MCP server, e.g. ``"github"``.
    :param tool_def: The raw tool definition returned by the MCP server.
    :param allowed: Optional allowlist of **bare** tool names (as declared
        in the spec's ``tools:`` list).  ``None`` means all tools are
        allowed.
    :returns: OpenAI function-tool schema dict with a namespaced ``name``,
        or ``None`` when the tool is filtered or has an invalid bare name.
    """
    from omnigent.tools.mcp import _normalize_input_schema

    bare_name = tool_def.name
    # Check allowed-list and name validity against the bare name, before
    # constructing the namespaced version, so spec authors write plain tool
    # names in their YAML (e.g. ``tools: [search]``, not ``github__search``).
    if allowed is not None and bare_name not in allowed:
        return None
    if not is_valid_tool_name(bare_name):
        _logger.warning(
            "MCP tool %r from server %r has an invalid name "
            "(must match [a-zA-Z0-9_-]{1,256}) — skipping",
            bare_name,
            server_name,
        )
        return None
    # Namespace: ``{server_name}__{bare_name}`` so two servers with a tool
    # named ``search`` produce ``github__search`` and ``glean__search``.
    namespaced_name = f"{server_name}__{bare_name}"
    return {
        "type": "function",
        "name": namespaced_name,
        "description": tool_def.description or "",
        "parameters": _normalize_input_schema(tool_def.inputSchema, namespaced_name),
    }


class RunnerMcpManager:
    """Per-runner MCP pool. Async methods run on the runner's loop.

    :param stdio_cwd: Working directory for spawned stdio MCP
        subprocesses. Defaults to ``None`` (subprocess inherits the
        runner's cwd). The CLI passes the user's project root here
        so relative ``command: .venv/bin/python`` resolves correctly
        when the runner itself is launched from a different cwd.
    """

    def __init__(
        self,
        stdio_cwd: Path | None = None,
        server_client: Any | None = None,
    ) -> None:
        """
        :param stdio_cwd: Working directory for spawned stdio MCP
            subprocesses.
        :param server_client: ``httpx.AsyncClient`` pointed at the
            Omnigent server. When provided, inline MCP elicitations are
            surfaced to the user via the Omnigent server's session events
            API. When ``None``, inline elicitations are declined.
        """
        self._specs: dict[str, _SpecEntry] = {}
        self._lru: list[str] = []  # most-recent at end
        self._lock = asyncio.Lock()
        # Hold strong refs to fire-and-forget eviction-close tasks so
        # the GC doesn't cancel them mid-flight (RUF006).
        self._evict_tasks: set[asyncio.Task[None]] = set()
        self._stdio_cwd = stdio_cwd
        self._server_client = server_client

    def _build_elicitation_callback(
        self,
    ) -> Callable[[str, ElicitRequestParams], Awaitable[ElicitResult]]:
        """
        Build an inline elicitation callback for MCP connections.

        When ``server_client`` is available, surfaces the
        elicitation to the user via the Omnigent server's session events
        API (approval card in web UI, y/a/n prompt in REPL) and
        parks until the user responds. Falls back to decline
        when no Omnigent server is available.

        :returns: Async callback ``(session_id, params) →
            ElicitResult``.
        """
        server_client = self._server_client

        async def _elicit(
            session_id: str,
            params: ElicitRequestParams,
        ) -> ElicitResult:
            """
            Handle an inline ``elicitation/create`` from the MCP
            server.

            When an Omnigent server client is available, POSTs a
            ``mcp_elicitation`` event to surface the approval
            prompt and parks on ``pending_approvals``. Otherwise
            declines.

            :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
            :param params: MCP elicitation params from the gateway.
            :returns: User verdict as an :class:`ElicitResult`.
            """
            if server_client is None:
                _logger.warning(
                    "MCP elicitation callback: no Omnigent server client available — declining",
                )
                return ElicitResult(action="decline")

            from omnigent.runner import pending_approvals

            message = getattr(params, "message", "")
            requested_schema = getattr(params, "requestedSchema", None)
            body: dict[str, Any] = {
                "type": "mcp_elicitation",
                "data": {"message": message},
            }
            if requested_schema is not None:
                body["data"]["requestedSchema"] = requested_schema

            try:
                resp = await server_client.post(
                    f"/v1/sessions/{session_id}/events",
                    json=body,
                    timeout=30.0,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "MCP elicitation callback: Omnigent server POST failed (%s) — declining",
                    exc,
                )
                return ElicitResult(action="decline")

            elicitation_id = data.get("elicitation_id", "")
            if not elicitation_id:
                _logger.warning(
                    "MCP elicitation callback: Omnigent server returned no "
                    "elicitation_id — declining",
                )
                return ElicitResult(action="decline")

            # Park until the user approves or declines (or timeout).
            # ``pending_approvals`` resolves a bool (accept/decline)
            # — it does not carry the user's form data. Content is
            # auto-filled from the requestedSchema below.
            # No-op publish_event: ``response.elicitation_resolved``
            # won't fire on timeout/cancellation, so the Omnigent server's
            # sidebar badge may stay stale. Same pattern as
            # proxy_mcp_manager. A future enhancement could POST
            # the resolved event back to the Omnigent server here.
            approved = await pending_approvals.wait_for_user_approval(
                elicitation_id=elicitation_id,
                conversation_id=session_id,
                publish_event=lambda _s, _e: None,
            )

            if not approved:
                return ElicitResult(action="decline")

            content = _build_accept_content(params)
            return ElicitResult(action="accept", content=content)

        return _elicit

    async def prewarm(self, spec: AgentSpec) -> None:
        """Fire-and-forget background spawn of *spec*'s MCPs. Idempotent."""
        configs = list(spec.mcp_servers or [])
        if not configs:
            return
        spec_hash = compute_spec_hash(configs, self._stdio_cwd)
        async with self._lock:
            entry = self._ensure_entry(spec_hash, configs)
            if entry.prewarm_task is None or entry.prewarm_task.done():
                entry.prewarm_task = asyncio.create_task(
                    self._connect_all(entry),
                    name=f"runner-mcp-prewarm:{spec_hash}",
                )

    async def schemas_for(self, spec: AgentSpec) -> McpSchemasResult:
        """Resolve MCP schemas for *spec*; awaits any in-flight prewarm."""
        configs = list(spec.mcp_servers or [])
        if not configs:
            return McpSchemasResult(schemas=[], tool_names=set(), failures={})
        spec_hash = compute_spec_hash(configs, self._stdio_cwd)
        async with self._lock:
            entry = self._ensure_entry(spec_hash, configs)
            self._touch(spec_hash)
            prewarm = entry.prewarm_task
            needs_connect = any(s.connection is None for s in entry.servers.values())
            if needs_connect and (prewarm is None or prewarm.done()):
                entry.prewarm_task = asyncio.create_task(
                    self._connect_all(entry),
                    name=f"runner-mcp-on-demand:{spec_hash}",
                )
                prewarm = entry.prewarm_task

        # Await outside the lock so concurrent prewarms can proceed.
        if prewarm is not None:
            try:
                await prewarm
            except Exception:
                _logger.exception("runner mcp prewarm task raised; surfacing partial results")

        schemas: list[dict[str, Any]] = []
        tool_names: set[str] = set()
        failures: dict[str, str] = {}
        for server in entry.servers.values():
            if server.error is not None:
                failures[server.config.name] = server.error
                continue
            allowed = set(server.config.tool_allowlist) or None
            for td in server.tools:
                schema = _mcp_tool_schema(server.config.name, td, allowed)
                if schema is None:
                    continue
                schemas.append(schema)
                tool_names.add(schema["name"])
        return McpSchemasResult(schemas=schemas, tool_names=tool_names, failures=failures)

    async def call_tool(
        self,
        spec: AgentSpec,
        tool_name: str,
        arguments: dict[str, Any],
        session_id: str | None = None,
    ) -> str:
        """
        Dispatch *tool_name* against the pool's cached MCP session.

        :param spec: Agent spec whose MCP servers to dispatch against.
        :param tool_name: Namespaced tool name, e.g.
            ``"github__list_issues"``.
        :param arguments: Decoded tool argument dict.
        :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
            Forwarded to the connection for inline elicitation
            context. ``None`` when no session is available.
        :returns: Tool result string.
        :raises McpElicitationRequired: When the MCP server returns
            an ``InputRequiredResult`` requiring user input before
            the tool can execute.
        """
        configs = list(spec.mcp_servers or [])
        if not configs:
            raise RuntimeError(
                f"runner has no MCPs registered for this spec; cannot dispatch {tool_name!r}"
            )
        spec_hash = compute_spec_hash(configs, self._stdio_cwd)
        entry = self._specs.get(spec_hash)
        if entry is None:
            # Dispatch before schemas_for(): populate + await prewarm.
            await self.schemas_for(spec)
            entry = self._specs.get(spec_hash)
        if entry is None:
            raise RuntimeError(f"runner failed to initialize MCPs for spec {spec.name!r}")

        # Tool names from the LLM are namespaced as ``{server}__{bare}``
        # (see ``_mcp_tool_schema``).  Strip the prefix to get the bare
        # name used by the MCP server internally.
        bare_name = tool_name
        owning_server: _ServerEntry | None = None
        for server in entry.servers.values():
            if server.error is not None:
                continue
            prefix = f"{server.config.name}__"
            if tool_name.startswith(prefix):
                candidate = tool_name[len(prefix) :]
                if any(td.name == candidate for td in server.tools):
                    bare_name = candidate
                    owning_server = server
                    break
            elif any(td.name == tool_name for td in server.tools):
                # Caller already passed the bare name (e.g. in tests).
                owning_server = server
                break
        if owning_server is None or owning_server.connection is None:
            raise RuntimeError(f"runner has no live MCP serving tool {tool_name!r}")

        return await owning_server.connection.call_tool(
            bare_name,
            arguments,
            session_id=session_id,
        )

    def _resolve_owning_server(
        self,
        spec: AgentSpec,
        bare_tool: str,
    ) -> _ServerEntry | None:
        """
        Find the server entry that owns *bare_tool*.

        Used by the MRTR retry path in ``/mcp/execute`` to access
        the ``McpServerConnection`` directly for
        ``call_tool_with_elicitation``.

        :param spec: Agent spec whose MCP servers to search.
        :param bare_tool: The bare tool name (no namespace prefix),
            e.g. ``"get_me"``.
        :returns: The owning ``_ServerEntry``, or ``None`` if the
            tool is not found.
        """
        configs = list(spec.mcp_servers or [])
        if not configs:
            return None
        spec_hash = compute_spec_hash(configs, self._stdio_cwd)
        entry = self._specs.get(spec_hash)
        if entry is None:
            return None
        for server in entry.servers.values():
            if server.error is not None:
                continue
            if any(td.name == bare_tool for td in server.tools):
                return server
        return None

    async def shutdown(self) -> None:
        """Best-effort close of every active MCP connection."""
        for spec_hash, entry in list(self._specs.items()):
            if entry.prewarm_task is not None and not entry.prewarm_task.done():
                entry.prewarm_task.cancel()
            for server in entry.servers.values():
                if server.connection is None:
                    continue
                try:
                    await server.connection.close()
                except Exception:
                    _logger.exception(
                        "error closing MCP %r in spec %s during shutdown",
                        server.config.name,
                        spec_hash,
                    )
        self._specs.clear()
        self._lru.clear()

    def _ensure_entry(self, spec_hash: str, configs: list[MCPServerConfig]) -> _SpecEntry:
        """Return or create the pool entry for *spec_hash*. Caller holds lock."""
        entry = self._specs.get(spec_hash)
        if entry is not None:
            return entry
        entry = _SpecEntry(spec_hash=spec_hash)
        for cfg in configs:
            entry.servers[cfg.name] = _ServerEntry(config=cfg)
        self._specs[spec_hash] = entry
        self._lru.append(spec_hash)
        self._evict_if_needed()
        return entry

    def _touch(self, spec_hash: str) -> None:
        """Mark *spec_hash* most-recently used. Caller holds lock."""
        with contextlib.suppress(ValueError):
            self._lru.remove(spec_hash)
        self._lru.append(spec_hash)

    def _evict_if_needed(self) -> None:
        """LRU-evict over-capacity entries. Caller holds lock."""
        while len(self._lru) > _POOL_SPEC_CAPACITY:
            victim = self._lru.pop(0)
            entry = self._specs.pop(victim, None)
            if entry is None:
                continue
            _logger.info(
                "runner mcp pool evicting spec %s (over capacity %d)",
                victim,
                _POOL_SPEC_CAPACITY,
            )
            if entry.prewarm_task is not None and not entry.prewarm_task.done():
                entry.prewarm_task.cancel()
            for server in entry.servers.values():
                if server.connection is not None:
                    task = asyncio.create_task(
                        self._safe_close(server.connection, victim, server.config.name),
                        name=f"runner-mcp-evict-close:{victim}:{server.config.name}",
                    )
                    self._evict_tasks.add(task)
                    task.add_done_callback(self._evict_tasks.discard)

    @staticmethod
    async def _safe_close(conn: McpServerConnection, spec_hash: str, name: str) -> None:
        try:
            await conn.close()
        except Exception:
            _logger.exception("error closing evicted MCP %r in spec %s", name, spec_hash)

    async def _connect_all(self, entry: _SpecEntry) -> None:
        """Connect every MCP in *entry* concurrently. Failures recorded per server."""

        async def _one(server: _ServerEntry) -> None:
            if server.connection is not None:
                return
            try:
                conn = McpServerConnection(
                    config=server.config,
                    cwd=self._stdio_cwd,
                    elicitation_callback=self._build_elicitation_callback(),
                )
                tools = await conn.connect()
                server.connection = conn
                server.tools = tools
                server.error = None
                _logger.info(
                    "runner mcp connected: spec=%s server=%s tools=%d",
                    entry.spec_hash,
                    server.config.name,
                    len(tools),
                )
            except Exception as exc:  # noqa: BLE001
                server.error = f"{type(exc).__name__}: {exc}"
                server.connection = None
                server.tools = []
                _logger.warning(
                    "runner mcp connect failed: spec=%s server=%s error=%s",
                    entry.spec_hash,
                    server.config.name,
                    server.error,
                )

        await asyncio.gather(*[_one(s) for s in entry.servers.values()])

    def status_snapshot(self) -> dict[str, Any]:
        """JSON-able view of pool state for introspection."""
        out_specs: list[dict[str, Any]] = []
        for spec_hash in self._lru:
            entry = self._specs.get(spec_hash)
            if entry is None:
                continue
            out_specs.append(
                {
                    "spec_hash": spec_hash,
                    "servers": [
                        {
                            "name": s.config.name,
                            "status": "ready"
                            if s.connection is not None and s.error is None
                            else ("failed" if s.error else "pending"),
                            "tools": [t.name for t in s.tools],
                            "error": s.error,
                        }
                        for s in entry.servers.values()
                    ],
                }
            )
        return {"specs": out_specs}


def make_mcp_manager(
    *,
    session_id: str | None = None,
    server_client: httpx.AsyncClient | None = None,
    stdio_cwd: Path | None = None,
    publish_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> McpManager:
    """Construct the right :class:`McpManager` backend for the call context.

    Selects between the two backends by whether a ``session_id`` is supplied:

    - **Proxy mode** (``session_id`` given): a
      :class:`~omnigent.runner.proxy_mcp_manager.ProxyMcpManager` bound to that
      session, routing every MCP call through the Omnigent server proxy
      (central policy enforcement). Requires ``server_client``.
    - **Direct mode** (no ``session_id``): a :class:`RunnerMcpManager` holding
      local MCP connections, enforcing policy in-runner.

    Both returned managers satisfy the :class:`McpManager` Protocol.

    :param session_id: Omnigent session id; its presence selects proxy mode.
    :param server_client: HTTP client pointed at the Omnigent server. Used as
        the proxy transport in proxy mode and as the elicitation channel in
        direct mode.
    :param stdio_cwd: Working directory for spawned stdio MCP subprocesses
        (direct mode only).
    :param publish_event: SSE publish callback for the proxy approval flow
        (proxy mode only).
    :returns: A direct or proxy MCP manager.
    :raises ValueError: if proxy mode is selected without a ``server_client``.
    """
    if session_id is not None:
        if server_client is None:
            raise ValueError("proxy MCP manager requires a server_client")
        from omnigent.runner.proxy_mcp_manager import ProxyMcpManager

        return ProxyMcpManager(session_id, server_client, publish_event=publish_event)

    return RunnerMcpManager(stdio_cwd=stdio_cwd, server_client=server_client)
