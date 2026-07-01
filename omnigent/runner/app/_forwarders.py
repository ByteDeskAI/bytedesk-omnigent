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

_AUTO_FORWARDER_CANCEL_TIMEOUT_S = 10.0

async def _cancel_auto_forwarder_task(session_id: str) -> None:
    """
    Cancel and await the session's registered transcript forwarder, if any.

    Native terminal (re)creation calls this before wiping the bridge's
    forward-cursor state: the claude forwarder is restart-forever and tails
    the transcript file across pane death, so without an explicit cancel
    the surviving task keeps mirroring alongside the newly spawned one and
    every post-recovery record is persisted twice (the server has no dedup
    for external conversation items).

    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :returns: None.
    """
    task = _AUTO_FORWARDER_TASKS.pop(session_id, None)
    if task is None or task.done():
        return
    task.cancel()
    # asyncio.wait absorbs the CancelledError and bounds the wait on a hung cancellation.
    _done, pending = await asyncio.wait({task}, timeout=_AUTO_FORWARDER_CANCEL_TIMEOUT_S)
    if pending:
        _logger.warning(
            "Cancelled transcript forwarder for %s did not finish within %.0fs",
            session_id,
            _AUTO_FORWARDER_CANCEL_TIMEOUT_S,
        )

def _register_auto_forwarder_task(session_id: str, task: asyncio.Task[Any]) -> None:
    """
    Register a session's transcript-forwarder task in the keyed registry.

    Keeps a strong reference so the task isn't garbage-collected mid-run.
    If a different live task already occupies the slot (a concurrent
    create that slipped past :func:`_cancel_auto_forwarder_task`), it is
    cancelled so a session never runs two forwarders at once.

    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :param task: Freshly created forwarder task for this session.
    :returns: None.
    """
    incumbent = _AUTO_FORWARDER_TASKS.get(session_id)
    if incumbent is not None and incumbent is not task:
        incumbent.cancel()
    _AUTO_FORWARDER_TASKS[session_id] = task

    def _evict(done_task: asyncio.Task[Any]) -> None:
        """Drop the registry entry unless a successor already replaced it."""
        if _AUTO_FORWARDER_TASKS.get(session_id) is done_task:
            del _AUTO_FORWARDER_TASKS[session_id]

    task.add_done_callback(_evict)

