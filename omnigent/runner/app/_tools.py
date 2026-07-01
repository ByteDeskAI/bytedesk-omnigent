"""Runner FastAPI app — spawns harness subprocesses and dispatches to them.

Per ``designs/RUNNER.md`` §1, the runner owns harness subprocesses.
It resolves the harness type + spawn-env from the agent spec (either
via a spec_resolver callback for in-process use, or via
GET /v1/agents/{id}/contents for out-of-process use).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import mimetypes
import os
import sys
import tempfile
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Type-only import: the runner keeps codex deps out of its runtime import
    # graph (they are imported lazily inside the codex-native helpers).
    from omnigent.codex_native_app_server import CodexAppServerClient
    from omnigent.runner.cost_advisor import AdvisorTurnResult

    # Boundary payload TypedDicts (sweep-2 BDP-2366). Imported type-only so
    # the runtime ``app`` <-> ``tool_dispatch`` import stays lazy (the cycle
    # both modules already break with function-level imports).
    from omnigent.runner.tool_dispatch import (
        SessionSnapshotPayload,
        SubagentInboxPayload,
    )
    from omnigent.terminals.registry import TerminalListEntry

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse

from omnigent.entities.session_resources import (
    DEFAULT_ENVIRONMENT_ID,
    SessionResourceView,
    resolve_terminal_entry_by_resource_id,
    session_resource_view_to_dict,
    terminal_resource_id,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness, is_native_harness
from omnigent.llms.summarize import (
    build_summarization_input,
    build_summarization_prompt,
    extract_summary_text,
)
from omnigent.model_override import validate_model_override
from omnigent.runner import pending_approvals
from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
from omnigent.runner.resource_registry import (
    CLAUDE_NATIVE_TERMINAL_ROLE,
    CODEX_NATIVE_TERMINAL_ROLE,
    OMNIGENT_REPL_TERMINAL_ROLE,
    PI_NATIVE_TERMINAL_ROLE,
    SessionResourceRegistry,
    TerminalExitEvent,
    TerminalLifecycle,
)
from omnigent.runner.subagent_status import (
    _TERMINAL as _SUBAGENT_TERMINAL_STATUSES,
)
from omnigent.runner.subagent_status import (
    SubagentWorkStatus,
    TerminalStatus,
)
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
from omnigent.spec.parser import discover_host_skills
from omnigent.spec.types import AgentSpec, LocalToolInfo, SkillSpec
from omnigent.terminals.ws_bridge import (
    WS_CLOSE_TERMINAL_NOT_FOUND,
    bridge_tmux_pty_to_websocket,
)
from omnigent.tools.builtins.load_skill import (
    find_skill_by_name,
    format_skill_meta_text,
)

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

def _inject_mcp_schemas(
    event_body: dict[str, Any],
    mcp_schemas: list[dict[str, Any]],
) -> None:
    """Append *mcp_schemas* to ``event_body["tools"]`` in place.

    Preserves any existing tools (builtins / client-side from the AP
    server) and adds MCP schemas after them. No-op when *mcp_schemas*
    is empty. See ``designs/RUNNER_MCP.md`` §Schema injection.

    Skips schemas already present by name: the per-session tool cache
    also folds in MCP schemas, and Codex rejects duplicate dynamic tool
    names after the executor normalizes both flat and nested schemas.
    """
    if not mcp_schemas:
        return
    existing = event_body.get("tools") or []
    seen_names = {
        name
        for t in existing
        if isinstance(t, dict) and (name := _schema_tool_name(t)) is not None
    }
    new_schemas: list[dict[str, Any]] = []
    for schema in mcp_schemas:
        name = _schema_tool_name(schema)
        if name is not None:
            if name in seen_names:
                continue
            seen_names.add(name)
        new_schemas.append(schema)
    event_body["tools"] = list(existing) + new_schemas

def _spec_builtin_tool_schemas(spec: Any, workdir: Any) -> list[dict[str, Any]]:
    """Return a spec's builtin (``sys_*`` etc.) tool schemas for a turn.

    Mirrors the builtin half of the fire-and-forget assembly in
    ``_run_turn_bg`` (``ToolManager(spec).get_tool_schemas()``) so the
    STREAMING turn path injects the same builtins. Without this the
    streaming path injects ONLY MCP schemas, so orchestration builtins
    such as ``sys_agent_list`` / ``sys_session_create`` never reach the
    harness and the model gets ``No such tool available:
    mcp__omnigent__sys_agent_list`` (BDP-2204). Returns ``[]`` (logged) on
    a ``ToolManager`` build failure, matching the ``_run_turn_bg`` guard so
    a broken local-tool dir degrades to MCP-only rather than failing the
    turn. The schemas are nested OpenAI shape; ``_normalize_tool_schemas``
    flattens them before the inner executor reads them.
    """
    if spec is None:
        return []
    try:
        from omnigent.tools.manager import ToolManager

        return ToolManager(spec, workdir=workdir).get_tool_schemas()
    except (ImportError, ValueError, RuntimeError):
        _logger.warning("streaming builtin schema build failed", exc_info=True)
        return []

def _schema_tool_name(schema: dict[str, Any]) -> str | None:
    """
    Extract a tool's function name from a tool schema.

    :param schema: A tool schema dict in nested OpenAI format, e.g.
        ``{"type": "function", "function": {"name": "Read", ...}}``, or
        the flattened harness format, e.g. ``{"type": "function",
        "name": "Read", ...}``.
    :returns: The tool name (e.g. ``"Read"``), or ``None`` when the
        schema is malformed / missing a string name field.
    """
    function = schema.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        return name if isinstance(name, str) else None
    name = schema.get("name")
    if isinstance(name, str):
        return name
    return None

def _merge_request_client_tools(
    spec_tools: list[dict[str, Any]],
    client_tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Append request-supplied client-side tools to the spec tool schemas.

    The runner-native session path assembles the harness tool list from
    the agent spec's builtin + MCP schemas only. Client-side tools the
    caller registers on the event (``request.tools`` — e.g. a REPL's
    ``Read`` / ``Write`` / ``Glob``) must also reach non-native harnesses
    so the model can emit them. The resulting call is not in
    ``_ALL_LOCAL_TOOLS``, so ``dispatch_tool_locally`` relays the
    ``action_required`` event upstream and it tunnels back to the caller.
    Without this merge the schemas never reach the executor and the model
    cannot invoke client tools at all.

    Builtins win on a name clash: a request tool must not shadow a
    policy-enforced server-side builtin of the same name.

    :param spec_tools: Spec-derived builtin + MCP tool schemas, each in
        nested OpenAI format, e.g.
        ``{"type": "function", "function": {"name": "load_skill", ...}}``.
    :param client_tools: Request-supplied client-side tool schemas in the
        same nested OpenAI format, e.g.
        ``{"type": "function", "function": {"name": "Read", ...}}``.
    :returns: ``spec_tools`` followed by the named client tools whose names
        don't collide with a spec tool. Non-dict and nameless client
        entries are dropped. A fresh list; inputs are not mutated. Empty
        when both inputs are empty.
    """
    seen: set[str] = {
        name
        for t in spec_tools
        if isinstance(t, dict) and (name := _schema_tool_name(t)) is not None
    }
    merged: list[dict[str, Any]] = list(spec_tools)
    for tool in client_tools:
        if not isinstance(tool, dict):
            continue
        name = _schema_tool_name(tool)
        # Drop nameless/malformed entries: the executor rejects an unnamed
        # FunctionTool, so forwarding one would only risk a hard error.
        if name is None or name in seen:
            continue
        seen.add(name)
        merged.append(tool)
    return merged

def _should_dispatch_tool_locally(
    tool_name: str,
    *,
    dispatch: TurnDispatch | None,
    is_mcp: bool,
    is_runner_builtin: bool,
    is_spec_local: bool,
) -> bool:
    """
    Decide whether the runner dispatches *tool_name* locally vs. relays it.

    Client-side (request-supplied) tools execute on the caller, so their
    ``action_required`` events must relay upstream to tunnel — dispatching
    them locally would error ``"<tool> not in local dispatch table"``. Every
    other tool keeps the prior behavior, including the ``dispatch is not
    None`` catch-all that covers spec-local / UC / spec-callable tools in
    session-native mode.

    :param tool_name: The tool the LLM called, e.g. ``"Read"`` or
        ``"sys_session_send"``.
    :param dispatch: The turn's :class:`TurnDispatch` (carries
        ``client_side_tool_names``), or ``None`` on the legacy path.
    :param is_mcp: ``True`` when *tool_name* is an MCP-server tool for
        this turn.
    :param is_runner_builtin: ``True`` when *tool_name* is a
        runner-dispatched builtin (``should_dispatch_locally(tool_name)``).
    :param is_spec_local: ``True`` when *tool_name* is a spec-declared
        local python/callable tool.
    :returns: ``True`` to dispatch locally on the runner; ``False`` to
        relay the ``action_required`` event upstream (client-side tunnel).
    """
    if dispatch is not None and tool_name in dispatch.client_side_tool_names:
        return False
    return dispatch is not None or is_mcp or is_runner_builtin or is_spec_local

