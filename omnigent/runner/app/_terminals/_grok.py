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

async def _auto_create_grok_terminal(
    session_id: str,
    resource_registry: SessionResourceRegistry,
    publish_event: Callable[[str, dict[str, Any]], None],
    *,
    server_client: httpx.AsyncClient | None = None,
) -> SessionResourceView:
    """
    Auto-create a Grok TUI terminal for a grok-native session.

    Launches the bare ``grok`` command which auto-starts a leader daemon at
    ``~/.grok/leader.sock``.  The grok-native harness executor attaches to
    that leader to inject prompts into the TUI session.

    :param session_id: Session/conversation identifier.
    :param resource_registry: Session resource registry for launching the
        terminal.
    :param publish_event: Runner session event publisher.
    :param server_client: Runner Omnigent server client.
    :returns: Created terminal resource view.
    """
    from omnigent.grok_native_bridge import grok_leader_socket_for_session
    from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec

    grok_bin = os.environ.get("HARNESS_GROK_BIN", "grok")
    # Per-conversation leader socket so the executor only ever sees THIS
    # conversation's TUI session (no cross-wiring across concurrent convs).
    leader_socket = str(grok_leader_socket_for_session(session_id))

    workspace = os.getcwd()
    if server_client is not None:
        try:
            resp = await server_client.get(f"/v1/sessions/{session_id}")
            if resp.status_code == 200:
                data = resp.json()
                ws = (data.get("session") or data).get("workspace")
                if ws and isinstance(ws, str):
                    workspace = ws
        except Exception:  # noqa: BLE001
            pass

    terminal_view = await resource_registry.launch_required_terminal(
        session_id=session_id,
        terminal_name="grok",
        session_key="main",
        resource_role="grok_native",
        spec=TerminalEnvSpec(
            os_env=OSEnvSpec(type="caller_process", cwd=workspace),
            command=grok_bin,
            # The TUI MUST run on the same per-conversation leader socket the
            # harness executor attaches to, so the executor sees the session the
            # TUI creates. ``grok`` only honors ``--leader-socket`` as a CLI flag
            # (the HARNESS_* env var is Omnigent's, not grok's) — with [cli]
            # use_leader=true this makes the TUI auto-spawn its leader here.
            args=["--leader-socket", leader_socket],
            env={"HARNESS_GROK_LEADER_SOCKET": leader_socket},
            scrollback=100_000,
            tmux_allow_passthrough=True,
            tmux_start_on_attach=False,
        ),
    )
    publish_event(
        session_id,
        {
            "type": "session.resource.created",
            "resource": session_resource_view_to_dict(terminal_view),
        },
    )
    # Advertise the TUI's tmux target so the harness executor can send-keys the
    # first user message into the pane (bootstrapping the TUI's resident
    # session); subsequent turns are delivered over ACP session/prompt, which
    # the TUI also renders. Best-effort — chat still works via the executor's
    # self-owned-session fallback if this is unavailable.
    try:
        from omnigent.grok_native_bridge import (
            bridge_dir_for_session_id,
            write_tmux_target,
        )

        terminal_registry = resource_registry.terminal_registry
        instance = (
            terminal_registry.get(session_id, "grok", "main")
            if terminal_registry is not None
            else None
        )
        if instance is not None and getattr(instance, "running", False):
            write_tmux_target(
                bridge_dir_for_session_id(session_id),
                socket_path=instance.socket_path,
                tmux_target=instance.tmux_target,
            )
            _logger.info("Published grok tmux target for session %s", session_id)
    except Exception:  # noqa: BLE001
        _logger.debug("grok tmux target publish failed", exc_info=True)
    _logger.info("Auto-created grok terminal for session %s", session_id)
    return terminal_view

