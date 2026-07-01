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

class _SessionSnapshot:
    """One ``GET /v1/sessions/{id}`` projected for all runner readers.

    The single source registration, workspace resolution, and spec
    resolution share instead of each fetching. See
    :func:`_session_snapshot` for the single-flight loader.

    :param ok: ``True`` only when the fetch returned HTTP 200.
    :param status_code: The fetch's HTTP status, or ``None`` on a
        transport error before any response, e.g. ``200`` / ``404``.
    :param created_at: Server creation time (UNIX seconds), or the
        runner's wall clock when the fetch failed / omitted it.
    :param workspace: Server-stored workspace path, or ``None``.
    :param agent_id: Bound agent id, or ``None`` when not yet bound /
        the fetch failed, e.g. ``"ag_abc123"``.
    :param sub_agent_name: For sub-agent sessions, the dispatched
        sub-agent's name, e.g. ``"claude_code"`` — used to swap the
        parent spec to the child's sub-spec so the child's harness
        (e.g. ``claude-native``) is resolved instead of the parent's.
        ``None`` for top-level sessions. Projected from the server
        snapshot so the identity survives a runner reconnect / spec-cache
        eviction (the in-memory ``_session_sub_agent_names`` map does not).
    """

    ok: bool
    status_code: int | None
    created_at: float
    workspace: str | None
    agent_id: str | None
    sub_agent_name: str | None = None

def _spec_with_workdir_paths(spec: Any, workdir: Path | None) -> Any:
    if workdir is None or spec is None:
        return spec
    local_tools = getattr(spec, "local_tools", None)
    if not local_tools:
        return spec
    resolved_tools: list[LocalToolInfo] = []
    changed = False
    for info in local_tools:
        path = getattr(info, "path", None)
        if path and not Path(path).is_absolute():
            resolved_tools.append(dataclasses.replace(info, path=str((workdir / path).resolve())))
            changed = True
        else:
            resolved_tools.append(info)
    if not changed:
        return spec
    return dataclasses.replace(spec, local_tools=resolved_tools)

class TurnDispatch:
    """
    Runner-side dispatch context for a single turn.

    Carries metadata the runner needs for harness resolution,
    MCP schema injection, and system prompt — separated from
    the harness message body so no field-stripping is needed.

    :param agent_id: Agent identifier for spec resolution,
        e.g. ``"ag_abc123"``.
    :param harness: Harness type, e.g. ``"openai-agents"``.
    :param has_mcp_servers: Whether to inject MCP tool schemas.
    :param instructions: System prompt for the LLM.
    :param agent_version: Spec version for invalidation.
    :param spawn_env: Harness subprocess environment overrides.
    :param client_side_tool_names: Names of request-supplied
        client-side tools for this turn (e.g. ``{"Read", "Glob"}``).
        These are executed by the caller, not the runner, so the
        proxy_stream relays their ``action_required`` events upstream
        to tunnel rather than dispatching them locally.
    """

    agent_id: str | None = None
    harness: str | None = None
    has_mcp_servers: bool = False
    instructions: str | None = None
    agent_version: int | None = None
    spawn_env: dict[str, str] | None = None
    client_side_tool_names: frozenset[str] = frozenset()

def _merge_advisor_note(
    content: list[dict[str, Any]] | str | None,
    note_item: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Merge the advisor note into the turn's user message, copy-on-write.

    The note must NOT be appended as its own trailing user message: the
    claude-sdk executor sends only the LATEST user message on resumed
    sessions (``_build_prompt``), so a trailing note-only message would
    shadow the user's actual question — the brain answers the note
    ("Got it, the model is now set to …") and the question is silently
    dropped. Riding the note's text inside the real user message keeps
    the question primary and the note visible.

    Handles both body shapes that reach the advisor: history-shaped
    message items (the background-turn path) get the note blocks
    appended to the latest ``role == "user"`` message; raw content
    blocks (the ``?stream=true`` path) and string shorthand get the
    note appended as additional ``input_text`` blocks of the same
    message.

    :param content: The harness body's ``content`` — message items,
        e.g. ``[{"type": "message", "role": "user", "content":
        [{"type": "input_text", "text": "refactor x"}]}]``, OR content
        blocks, e.g. ``[{"type": "input_text", "text": "refactor x"}]``,
        OR a plain-string shorthand, OR ``None``.
    :param note_item: The advisor's note message item (see
        :func:`omnigent.runner.cost_advisor._advisor_note_item`), e.g.
        ``{"type": "message", "role": "user", "content": [{"type":
        "input_text", "text": "[Cost advisor: …]"}]}``.
    :returns: A new content list with the note merged in; the input list
        and the merged message are copied so the cached session history
        is never mutated.
    """
    note_blocks = list(note_item.get("content") or [])
    if isinstance(content, str):
        # String shorthand: normalize to blocks so the note can ride along.
        return [{"type": "input_text", "text": content}, *note_blocks]
    items: list[dict[str, Any]] = list(content or [])
    for i in range(len(items) - 1, -1, -1):
        item = items[i]
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        merged = dict(item)
        existing = merged.get("content")
        if isinstance(existing, str):
            existing = [{"type": "input_text", "text": existing}]
        merged["content"] = [*(existing or []), *note_blocks]
        items[i] = merged
        return items
    if any(isinstance(it, dict) and it.get("type") == "message" for it in items):
        # Message-shaped history with no user message (degenerate): keep the
        # old trailing-item behavior rather than dropping the note.
        return [*items, note_item]
    # Raw content blocks: the whole list IS the user message's content.
    return [*items, *note_blocks]

def _apply_advisor_to_body(
    body: dict[str, Any],
    result: AdvisorTurnResult,
) -> None:
    """
    Apply a cost-advisor turn result to the harness request body in place.

    Optimize mode (claude-sdk, no user pin): sets ``model_override`` so the
    inner executor runs THIS turn on the verdict model via its per-turn
    ``set_model`` (claude_sdk_executor: switches only when the model
    changes between turns), and merges the one-line system note into the
    turn's user message (see :func:`_merge_advisor_note`). Advise
    mode (or a user pin / non-applicable harness): ``apply_model`` and
    ``note_item`` are both ``None``, so the body is unchanged — the verdict
    is shadow-recorded in the label only.

    :param body: The harness request body, mutated in place. The caller
        must own this dict (copy-on-write at the streaming call site) so
        the cached session history is not mutated.
    :param result: The advisor turn result.
    """
    if result.apply_model is not None:
        # Per-turn brain-model override; flows to ExecutorConfig.model in
        # the harness adapter, then cfg.model in the claude-sdk executor.
        body["model_override"] = result.apply_model
    if result.note_item is not None:
        body["content"] = _merge_advisor_note(body.get("content"), result.note_item)

def _wrap_as_message_event(body: dict[str, Any]) -> dict[str, Any]:
    """
    Adapt a ``CreateResponseRequest``-shaped body into a
    :class:`MessageEvent` body for the harness's discriminated
    ``POST /v1/sessions/{id}/events`` endpoint.

    The runtime still synthesizes ``CreateResponseRequest``-shaped
    bodies internally to drive harness turns; this helper renames
    ``input`` → ``content`` and stamps the discriminator
    (``type="message"``) and role (``role="user"``) fields without
    copying every other field by name — the harness's
    :class:`MessageEvent` accepts arbitrary extras and forwards them
    onto its synthesized :class:`CreateResponseRequest`, so
    passthrough is automatic.

    :param body: The runner's incoming JSON body, e.g.
        ``{"model": "agent", "input": [...], "tools": [...]}``.
    :returns: A new dict in :class:`MessageEvent` shape, e.g.
        ``{"type": "message", "role": "user", "model": "agent",
        "content": [...], "tools": [...]}``. Does not mutate the
        input dict.
    """
    event_body = dict(body)
    event_body["type"] = "message"
    event_body["role"] = "user"
    if "input" in event_body:
        event_body["content"] = event_body.pop("input")
    return event_body

class _ContextWindowOverflow(Exception):
    """
    Raised by the proxy_stream when the harness reports a context-window overflow.

    Caught by ``_run_turn_bg`` to trigger reactive compaction and retry.

    :param max_tokens: The model's context window, e.g. ``128000``.
    :param actual_tokens: The prompt size that overflowed, e.g. ``131072``.
    """

    def __init__(self, max_tokens: int, actual_tokens: int) -> None:
        self.max_tokens = max_tokens
        self.actual_tokens = actual_tokens
        super().__init__(f"context window exceeded: {actual_tokens} > {max_tokens}")

_CONTEXT_OVERFLOW_PATTERNS = (
    "context_length_exceeded",
    "context window",
    "maximum context length",
)

def _is_context_overflow_error(event: dict[str, Any]) -> tuple[int, int] | None:
    """
    Check if a ``response.failed`` SSE event indicates a context-window overflow.

    :param event: The parsed SSE event dict.
    :returns: ``(max_tokens, actual_tokens)`` if overflow detected, else ``None``.
    """
    if event.get("type") != "response.failed":
        return None
    error = event.get("error", {})
    msg = str(error.get("message", "")).lower()
    if not any(pat in msg for pat in _CONTEXT_OVERFLOW_PATTERNS):
        return None
    import re

    actual_gt_max = re.search(r"(\d{4,})\D*>\D*(\d{4,})", msg)
    if actual_gt_max is not None:
        return int(actual_gt_max.group(2)), int(actual_gt_max.group(1))

    numbers = re.findall(r"(\d{4,})", msg)
    if len(numbers) >= 2:
        return int(numbers[-2]), int(numbers[-1])
    if len(numbers) == 1:
        return int(numbers[0]), int(numbers[0]) + 1
    return 128000, 128001

async def _resolve_forwarded_message_content(
    content: list[dict[str, Any]],
    *,
    session_id: str,
    server_client: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    """Resolve server-uploaded ``file_id`` blocks inside the runner.

    Remote Omnigent servers can forward session messages with raw file IDs
    because their file store is not available to the out-of-process
    runner. The runner can still fetch bytes through the session-scoped
    file resource endpoint and inline them before handing content to a
    harness. Blocks already resolved by the server pass through.
    """
    if not any(isinstance(block, dict) and "file_id" in block for block in content):
        return content

    import base64 as _base64

    resolved: list[dict[str, Any]] = []
    changed = False
    for block in content:
        if not isinstance(block, dict) or "file_id" not in block:
            resolved.append(block)
            continue
        file_id = block.get("file_id")
        if not isinstance(file_id, str) or not file_id:
            resolved.append(block)
            continue
        try:
            meta_resp = await server_client.get(
                f"/v1/sessions/{session_id}/resources/files/{file_id}",
                timeout=10.0,
            )
            content_resp = await server_client.get(
                f"/v1/sessions/{session_id}/resources/files/{file_id}/content",
                timeout=30.0,
            )
            meta_resp.raise_for_status()
            content_resp.raise_for_status()
        except httpx.HTTPError:
            _logger.warning(
                "runner failed to resolve file_id=%s for session=%s",
                file_id,
                session_id,
                exc_info=True,
            )
            resolved.append(block)
            continue

        meta = meta_resp.json()
        content_type = (
            meta.get("content_type")
            or content_resp.headers.get("content-type")
            or "application/octet-stream"
        )
        # Strip any charset suffix: data URIs need the media type hint.
        if isinstance(content_type, str):
            content_type = content_type.split(";", 1)[0]
        else:
            content_type = "application/octet-stream"
        encoded = _base64.b64encode(content_resp.content).decode("ascii")
        new_block = {k: v for k, v in block.items() if k != "file_id"}
        if block.get("type") == "input_image":
            new_block["image_url"] = f"data:{content_type};base64,{encoded}"
        else:
            new_block["file_data"] = f"data:{content_type};base64,{encoded}"
        resolved.append(new_block)
        changed = True

    return resolved if changed else content

