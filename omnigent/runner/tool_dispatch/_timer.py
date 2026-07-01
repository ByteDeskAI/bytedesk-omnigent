"""Runner-local tool dispatch for intercepted action_required events.

Per designs/RUNNER_TOOL_DISPATCH.md, the runner dispatches most tools
locally and relays action_required events upstream UNCHANGED for
visibility (the executor emits ToolCallInProgress/ToolCallObserved for
the REPL but doesn't dispatch itself — it checks should_dispatch_locally
and skips).

Tool categories:
- _OS_ENV_TOOLS: execute through a runner-local OSEnvironment (sys_os_*)
- _REST_TOOLS: call server REST APIs (sys_call_async, sys_cancel_async)
- _FILE_TOOLS: call server file APIs (sys_upload/download/list_files)
- _TERMINAL_TOOLS: runner-local TerminalRegistry
- MCP tools: spec-defined; dispatched via RunnerMcpManager passed
  in by proxy_stream (designs/RUNNER_MCP.md). Not in the static
  allow-list because names vary per spec.
- Client-side tools: tunneled via REPL (deferred)
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, cast

if TYPE_CHECKING:
    from omnigent.identity.identity import ActingIdentity
    from omnigent.runner.mcp_manager import McpManager
    from omnigent.runner.resource_registry import SessionResourceRegistry
    from omnigent.runtime.filesystem_registry import FilesystemRegistry
    from omnigent.spec.types import AgentSpec
    from omnigent.terminals.registry import TerminalRegistry

import httpx

from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE,
    CODEX_NATIVE_WRAPPER_VALUE,
)
from omnigent.model_override import (
    harness_supports_model_override,
    model_family_mismatch,
    normalize_model_for_provider,
    validate_model_override,
)
from omnigent.runner.subagent_status import (
    _ACTIVE as _SUBAGENT_ACTIVE_STATUSES,
)
from omnigent.runner.subagent_status import (
    SubagentWorkStatus,
)
from omnigent.runner.tool_execution_context import ToolExecutionContext
from omnigent.runtime import pending_elicitations
from omnigent.session_lifecycle import (
    CLOSED_LABEL_KEY,
    CLOSED_LABEL_VALUE,
    is_session_closed,
    title_without_closed_marker,
)
from omnigent.tools import ToolManager
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.async_inbox import (
    SysCallAsyncTool,
    SysCancelAsyncTool,
    SysCancelTaskTool,
    SysReadInboxTool,
)
from omnigent.tools.builtins.download_file import DownloadFileTool
from omnigent.tools.builtins.list_comments import ListCommentsTool
from omnigent.tools.builtins.os_env import (
    SysOsEditTool,
    SysOsReadTool,
    SysOsShellTool,
    SysOsWriteTool,
)
from omnigent.tools.builtins.spawn import (
    # Shared contract values with the in-process sys_session_* tools. Imported
    # (not duplicated) so the runner's REST-backed peek clamps to the same
    # bounds the LLM-facing tool schema advertises and tombstones with the
    # same marker the in-process close writes.
    _ACTIVITY_MAX_CHARS,
    _CLOSED_TITLE_INFIX,
    _HISTORY_DEFAULT_TAIL,
    _clamp_tail_items,
)
from omnigent.tools.builtins.sys_terminal import (
    SysTerminalCloseTool,
    SysTerminalLaunchTool,
    SysTerminalListTool,
    SysTerminalReadTool,
    SysTerminalSendTool,
)
from omnigent.tools.builtins.update_comment import UpdateCommentTool
from omnigent.tools.builtins.upload_file import UploadFileTool, safe_resolve

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

async def _execute_timer_set(
    args: dict[str, Any],
    *,
    server_client: httpx.AsyncClient | None = None,
    conversation_id: str | None = None,
) -> str:
    """
    Schedule a timer that fires after a delay.

    :param args: Parsed arguments. Keys: ``seconds`` (number),
        ``repeat`` (bool, default False), ``note`` (optional str).
    :param server_client: httpx client for persisting firings.
    :param conversation_id: Session the timer belongs to, e.g.
        ``"conv_abc123"``.
    :returns: JSON string with ``timer_id`` and ``status``.
    """
    from omnigent.runner import app as _app

    seconds_raw = args.get("seconds")
    if not isinstance(seconds_raw, (int, float)) or isinstance(seconds_raw, bool):
        return json.dumps({"error": "seconds must be a number"})
    seconds = float(seconds_raw)
    if seconds < 0:
        return json.dumps({"error": "seconds must be non-negative"})
    if seconds > _MAX_TIMER_SECONDS:
        return json.dumps({"error": f"seconds must be <= {_MAX_TIMER_SECONDS}"})
    repeat = args.get("repeat", False)
    if not isinstance(repeat, bool):
        return json.dumps({"error": "repeat must be a boolean"})
    note: str | None = args.get("note")
    if note is not None and not isinstance(note, str):
        return json.dumps({"error": "note must be a string"})
    if server_client is None or conversation_id is None:
        return json.dumps({"error": "timer requires server_client and conversation_id"})

    timer_id = f"timer_{uuid.uuid4().hex}"
    task = asyncio.create_task(
        _timer_loop(
            timer_id=timer_id,
            conversation_id=conversation_id,
            seconds=seconds,
            repeat=repeat,
            note=note,
            server_client=server_client,
        ),
        name=f"timer-{timer_id}",
    )
    _app.register_timer(conversation_id, timer_id, task)
    return json.dumps(
        {
            "timer_id": timer_id,
            "status": "scheduled",
            "seconds": seconds,
            "repeat": repeat,
            "note": note,
        }
    )

async def _timer_loop(
    *,
    timer_id: str,
    conversation_id: str,
    seconds: float,
    repeat: bool,
    note: str | None,
    server_client: httpx.AsyncClient,
) -> None:
    """
    Background loop: sleep then fire timer notifications.

    :param timer_id: Unique timer id, e.g. ``"timer_a1b2..."``.
    :param conversation_id: Session to fire into.
    :param seconds: Delay between firings.
    :param repeat: Loop indefinitely when True.
    :param note: Optional note echoed in firing text.
    :param server_client: httpx client for persistence.
    """
    from omnigent.runner import app as _app

    try:
        while True:
            await asyncio.sleep(seconds)
            text = f"[System: timer {timer_id} fired]"
            if note:
                text += f"\nnote: {note!r}"
            try:
                await server_client.post(
                    f"/v1/sessions/{conversation_id}/events",
                    json={
                        "type": "message",
                        "data": {
                            "role": "user",
                            "is_meta": True,
                            "content": [{"type": "input_text", "text": text}],
                        },
                    },
                    timeout=30.0,
                )
            except (httpx.HTTPError, asyncio.TimeoutError):
                _logger.warning(
                    "Timer %s firing persist failed for %s",
                    timer_id,
                    conversation_id,
                    exc_info=True,
                )
            if not repeat:
                break
    except asyncio.CancelledError:
        return
    finally:
        _app.unregister_timer(conversation_id, timer_id)

async def _execute_timer_cancel(
    args: dict[str, Any],
    *,
    conversation_id: str | None = None,
) -> str:
    """
    Cancel a previously scheduled timer by ``timer_id``.

    :param args: Parsed arguments with ``timer_id`` (string).
    :param conversation_id: Session the timer belongs to.
    :returns: JSON with ``status`` ``"cancelled"`` or ``"not_found"``.
    """
    from omnigent.runner import app as _app

    timer_id = args.get("timer_id")
    if not isinstance(timer_id, str) or not timer_id:
        return json.dumps({"error": "timer_id is required"})
    if conversation_id is None:
        return json.dumps({"error": "timer_cancel requires conversation_id"})
    cancelled = _app.cancel_timer(conversation_id, timer_id)
    return json.dumps({"timer_id": timer_id, "status": "cancelled" if cancelled else "not_found"})

