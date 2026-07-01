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
def _import_parent_bindings() -> None:
    from .. import (
        _constants as _parent_constants,
        _dispatch as _parent_dispatch,
        _forwarders as _parent_forwarders,
        _harness as _parent_harness,
        _helpers as _parent_helpers,
        _policy as _parent_policy,
        _state as _parent_state,
        _streaming as _parent_streaming,
        _subagents as _parent_subagents,
        _timers as _parent_timers,
        _tools as _parent_tools,
    )

    g = globals()
    for _mod in (
        _parent_constants,
        _parent_state,
        _parent_dispatch,
        _parent_forwarders,
        _parent_harness,
        _parent_helpers,
        _parent_policy,
        _parent_streaming,
        _parent_subagents,
        _parent_timers,
        _parent_tools,
    ):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_parent_bindings()

async def _auto_create_repl_terminal(
    session_id: str,
    resource_registry: SessionResourceRegistry,
    publish_event: Callable[[str, dict[str, Any]], None],
    *,
    server_client: httpx.AsyncClient,
) -> SessionResourceView:
    """
    Auto-create an Omnigent REPL terminal for a runner-hosted SDK session.

    Called when the runner receives a non-native (SDK-harness) top-level
    session via ``POST /v1/sessions`` and no REPL terminal exists yet. The
    terminal hosts the framework's own TUI (``omnigent attach
    <session_id> --server <url>``) in a tmux pane, exposed through the
    standard terminal-attach WebSocket so the web UI embeds it exactly
    like the claude-/codex-native terminals — with the Omnigent REPL as
    the TUI.

    The REPL is a pure co-drive client: it joins the live session over
    HTTP+SSE and dispatches turns to this runner, so the web chat view and
    the embedded terminal stay in sync. The tmux command is deferred until
    the first client attaches (``tmux_start_on_attach``): a session whose
    terminal is never opened pays only for an idle tmux pane, and by first
    attach the session is fully live (``omnigent attach`` fails loud on a
    non-live session) with the REPL sized to the real attached terminal.

    Auth parity with the native terminals: the spawned ``omnigent
    attach`` resolves credentials for ``--server`` the same way a
    user-launched CLI does (``OMNIGENT_REMOTE_AUTH_TOKEN`` env → stored
    OIDC token from ``omnigent login`` → ``~/.databrickscfg``), which
    holds because the runner lives on the user's machine.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param resource_registry: Session resource registry for launching the
        terminal.
    :param publish_event: The runner's per-session SSE emitter,
        ``(session_id, event_dict) -> None``, used to surface the new
        terminal on the live stream so the web UI's Terminal pill enables
        without a refresh.
    :param server_client: Omnigent server client used to stamp the
        ``omnigent.ui: terminal`` presentation label that makes the web
        UI show the Chat/Terminal toggle.
    :returns: The launched terminal's :class:`SessionResourceView`.
    """
    from omnigent._wrapper_labels import UI_MODE_LABEL_KEY, UI_MODE_TERMINAL_VALUE
    from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec

    started_at = time.monotonic()
    workspace = os.environ.get("OMNIGENT_RUNNER_WORKSPACE", str(Path.cwd()))
    server_url = os.environ.get("RUNNER_SERVER_URL", "http://localhost:6767")
    env_spec = TerminalEnvSpec(
        os_env=OSEnvSpec(type="caller_process", cwd=workspace),
        # The runner's interpreter is the venv with omnigent installed;
        # ``python -m omnigent`` avoids depending on the console script
        # being on the tmux pane's PATH.
        command=sys.executable,
        args=["-m", "omnigent", "attach", session_id, "--server", server_url],
        scrollback=50000,
        # Defer the REPL process until the first web client attaches (see
        # docstring): no cost for never-opened terminals, and the REPL
        # starts against the real attached terminal size.
        tmux_start_on_attach=True,
    )
    terminal_view = await resource_registry.launch_auxiliary_terminal(
        session_id=session_id,
        terminal_name=_REPL_TERMINAL_NAME,
        session_key=_REPL_TERMINAL_SESSION_KEY,
        spec=env_spec,
        # Runner-private marker the attach WebSocket uses to recreate
        # this terminal when its tmux session has died (the REPL exited
        # or crashed) instead of rejecting the attach.
        resource_role=OMNIGENT_REPL_TERMINAL_ROLE,
    )
    # Stamp the presentation label that gates the web UI's Chat/Terminal
    # pill (ap-web TerminalFirstContext). Stamped here — not at session
    # creation — so only sessions whose runner actually hosts a REPL
    # terminal get the toggle; in-process (runner-less) sessions never
    # show a dead pill. The ``omnigent.wrapper`` label is deliberately
    # NOT set: these sessions stay chat-first, the terminal is a
    # secondary view.
    try:
        await server_client.patch(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
            json={"labels": {UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE}},
        )
    except httpx.HTTPError:
        _logger.warning(
            "Could not stamp %s label for %s; the web Terminal toggle may not appear",
            UI_MODE_LABEL_KEY,
            session_id,
        )
    # Surface the terminal on the live SSE stream so an already-connected
    # web UI enables the Terminal toggle immediately (the auxiliary-terminal
    # launch helper registers the resource but does not publish — mirrors the
    # claude-native auto-create path).
    from omnigent.entities.session_resources import session_resource_view_to_dict

    terminal_payload = session_resource_view_to_dict(terminal_view)
    publish_event(
        session_id,
        {
            "type": "session.resource.created",
            "resource": terminal_payload,
        },
    )
    _logger.info(
        "Auto-created omnigent REPL terminal for session %s: terminal_id=%s "
        "server_url=%s elapsed_ms=%.0f",
        session_id,
        terminal_payload.get("id"),
        server_url,
        (time.monotonic() - started_at) * 1000,
    )
    return terminal_view

