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
    from .. import _constants as _parent_constants
    from .. import _state as _parent_state
    g = globals()
    for _mod in (_parent_constants, _parent_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_parent_bindings()

class _PiNativeLaunchConfig:
    """
    Persisted launch config needed for runner-owned Pi terminal setup.

    :param workspace: Workspace cwd for the Pi TUI.
    :param server_url: Omnigent server URL for the Pi extension.
    :param terminal_launch_args: User pass-through Pi CLI args.
    :param external_session_id: Existing Pi session id, when captured by
        the extension.
    """

    workspace: Path
    server_url: str
    terminal_launch_args: list[str] | None
    external_session_id: str | None

async def _pi_native_launch_config(
    *,
    session_id: str,
    server_client: httpx.AsyncClient | None,
) -> _PiNativeLaunchConfig:
    """
    Fetch and validate persisted Pi launch config for a session.

    :param session_id: Session/conversation id.
    :param server_client: Runner Omnigent server client.
    :returns: Parsed launch config.
    """
    if server_client is None:
        raise RuntimeError("server_client is required for runner-owned Pi terminals.")
    try:
        resp = await server_client.get(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Could not fetch Pi launch config for {session_id!r}.") from exc
    if resp.status_code != 200:
        raise RuntimeError(
            f"Could not fetch Pi launch config for {session_id!r}: "
            f"GET /v1/sessions returned {resp.status_code}."
        )
    try:
        snapshot = resp.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Could not fetch Pi launch config for {session_id!r}: invalid JSON."
        ) from exc
    if not isinstance(snapshot, dict):
        raise RuntimeError(
            f"Could not fetch Pi launch config for {session_id!r}: snapshot was not a JSON object."
        )
    terminal_launch_args = snapshot.get("terminal_launch_args")
    if terminal_launch_args is not None and not (
        isinstance(terminal_launch_args, list)
        and all(isinstance(arg, str) for arg in terminal_launch_args)
    ):
        raise RuntimeError(f"Invalid terminal_launch_args for Pi session {session_id!r}.")
    external_session_id = snapshot.get("external_session_id")
    if external_session_id is not None and (
        not isinstance(external_session_id, str) or not external_session_id
    ):
        raise RuntimeError(f"Invalid external_session_id for Pi session {session_id!r}.")
    session_workspace = snapshot.get("workspace")
    if session_workspace is not None and (
        not isinstance(session_workspace, str) or not session_workspace
    ):
        raise RuntimeError(f"Invalid workspace for Pi session {session_id!r}.")
    return _PiNativeLaunchConfig(
        workspace=_pi_session_workspace(session_workspace),
        server_url=os.environ.get("RUNNER_SERVER_URL", "http://localhost:6767").rstrip("/"),
        terminal_launch_args=terminal_launch_args,
        external_session_id=external_session_id,
    )

def _build_pi_native_args(
    *,
    terminal_launch_args: list[str] | None,
    extension_path: Path,
    session_dir: Path,
    external_session_id: str | None,
) -> list[str]:
    """
    Build Pi CLI args for a runner-owned native TUI session.

    :param terminal_launch_args: User pass-through Pi args.
    :param extension_path: Generated Omnigent Pi extension path.
    :param session_dir: Per-Omnigent-session Pi session directory.
    :param external_session_id: Captured Pi session id, if any.
    :returns: Complete Pi arg vector excluding the executable.
    """
    user_args = list(terminal_launch_args or [])
    args = ["--extension", str(extension_path)]
    if not _pi_args_have_session_control(user_args):
        args.extend(["--session-dir", str(session_dir)])
        if external_session_id:
            args.extend(["--session", external_session_id])
    args.extend(user_args)
    return args

async def _auto_create_pi_terminal(
    session_id: str,
    resource_registry: SessionResourceRegistry,
    publish_event: Callable[[str, dict[str, Any]], None],
    *,
    server_client: httpx.AsyncClient | None,
) -> SessionResourceView:
    """
    Auto-create a Pi terminal for a pi-native session.

    :param session_id: Session/conversation identifier.
    :param resource_registry: Session resource registry for launching the
        terminal.
    :param publish_event: Runner session event publisher.
    :param server_client: Runner Omnigent server client.
    :returns: Created terminal resource view.
    """
    from omnigent.conversation_browser import conversation_url
    from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec
    from omnigent.pi_native import resolve_pi_executable
    from omnigent.pi_native_bridge import (
        PI_NATIVE_CONFIG_ENV_VAR,
        clear_inbox,
        pi_session_dir,
        prepare_bridge_dir,
        write_extension_files,
    )
    from omnigent.pi_native_bridge import extension_path as pi_extension_path
    from omnigent.runner._entry import _make_auth_token_factory

    launch_config = await _pi_native_launch_config(
        session_id=session_id,
        server_client=server_client,
    )
    workspace = str(launch_config.workspace)
    bridge_dir = prepare_bridge_dir(session_id)
    # Drop stale payloads so a relaunched Pi process can't replay them.
    clear_inbox(bridge_dir)
    pi_extension = pi_extension_path(bridge_dir)
    session_dir = pi_session_dir(bridge_dir)
    auth_factory = _make_auth_token_factory()
    auth_token = auth_factory() if auth_factory is not None else None
    auth_headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
    _extension, config = write_extension_files(
        bridge_dir,
        session_id=session_id,
        server_url=launch_config.server_url,
        conversation_url=conversation_url(launch_config.server_url, session_id),
        auth_headers=auth_headers,
    )
    pi_command = resolve_pi_executable()
    pi_args = _build_pi_native_args(
        terminal_launch_args=launch_config.terminal_launch_args,
        extension_path=pi_extension,
        session_dir=session_dir,
        external_session_id=launch_config.external_session_id,
    )
    pi_env = {
        PI_NATIVE_CONFIG_ENV_VAR: str(config),
        "OMNIGENT_PI_NATIVE_BRIDGE_DIR": str(bridge_dir),
    }
    # Route the runner-owned Pi process through the provider configured by
    # ``omnigent setup`` (Databricks gateway / API key), so a separate
    # ``pi /login`` isn't required — the parity codex-native/claude-native
    # already have. Skipped when the user pinned their own provider/model via
    # terminal_launch_args, or when no usable provider is configured (Pi then
    # falls back to its own login). Writes a managed per-session Pi config dir,
    # never touching the user's global ``~/.pi/agent``.
    if not _pi_args_have_provider(launch_config.terminal_launch_args or []):
        from omnigent.pi_native_credentials import (
            pi_native_provider_launch,
            resolve_pi_native_provider,
        )

        provider = resolve_pi_native_provider()
        if provider is not None:
            cred_env, cred_args = pi_native_provider_launch(bridge_dir / "pi-agent", provider)
            pi_env.update(cred_env)
            pi_args.extend(cred_args)
    terminal_view = await resource_registry.launch_required_terminal(
        session_id=session_id,
        terminal_name="pi",
        session_key="main",
        resource_role=PI_NATIVE_TERMINAL_ROLE,
        spec=TerminalEnvSpec(
            os_env=OSEnvSpec(type="caller_process", cwd=workspace),
            command=pi_command,
            args=pi_args,
            env=pi_env,
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
    _logger.info(
        "Auto-created pi terminal for session %s with extension %s",
        session_id,
        pi_extension,
    )
    return terminal_view

