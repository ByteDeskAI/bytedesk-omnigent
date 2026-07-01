"""Forward Codex app-server notifications into Omnigent sessions."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from omnigent._native_post_delivery import post_may_have_been_delivered
from omnigent.claude_native_bridge import url_component
from omnigent.codex_native_app_server import (
    CodexAppServerClient,
    CodexMessage,
    client_for_transport,
)
from omnigent.codex_native_bridge import (
    CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
    CodexNativeBridgeState,
    clear_active_turn_id_if_matches,
    codex_home_for_bridge_dir,
    read_bridge_state,
    read_codex_config_model,
    update_active_turn_id,
    update_thread_id,
    write_bridge_state,
)
from omnigent.codex_native_elicitation import (
    codex_elicitation_id,
)
from omnigent.codex_native_elicitation import (
    is_codex_request_id as _is_codex_request_id,
)
from omnigent.entities.session_resources import terminal_resource_id

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

async def _sleep(seconds: float) -> None:
    """
    Stubbable indirection for Codex forwarder sleeps.

    Exists so tests can stub retry delays without patching
    ``asyncio.sleep`` through the imported module singleton.

    :param seconds: Delay in seconds.
    :returns: None after the sleep completes.
    """
    await asyncio.sleep(seconds)

def _turn_started_status_edge(
    bridge_dir: Path,
    params: dict[str, Any],
) -> _CodexTurnStatusEdge:
    """
    Record a Codex turn start and return the Omnigent running edge.

    :param bridge_dir: Native Codex bridge directory.
    :param params: Codex ``turn/started`` params.
    :returns: Running status edge for the observed turn start.
    """
    turn = params.get("turn")
    turn_id = _turn_id_from_payload(turn) or _turn_id_from_payload(params)
    update_active_turn_id(bridge_dir, turn_id)
    return _CodexTurnStatusEdge(
        status="running",
        turn_id=turn_id,
        source="turn/started",
    )

async def _ensure_user_message_posted(
    client: httpx.AsyncClient,
    session_id: str,
    params: dict[str, Any],
    forwarder_state: _CodexForwarderState | None,
) -> None:
    """
    Guarantee a turn's user message is posted before its assistant reply.

    The forwarder's live stream normally delivers ``userMessage`` before
    ``agentMessage`` for a turn, so this is a no-op. But on a fresh thread
    the subscription can miss the early ``userMessage`` event; this
    recovers it via a targeted ``thread/resume`` and posts it through the
    normal claim/post path so it takes an earlier Omnigent position than the
    reply. The recovered item carries Codex's resume id (e.g. ``item-1``),
    matching the id the resume backfill would later use — so the dedup
    gate drops the backfill's duplicate.

    No-op when ``forwarder_state`` is absent (tests bypassing
    ``supervise_forwarder``), when no Codex client is wired, or when the
    turn's user message was already posted this connection.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param params: Codex ``item/completed`` params for the assistant
        message whose turn's user message must already be posted.
    :param forwarder_state: Mutable forwarder state tracking posted user
        turns and holding the Codex app-server client.
    :returns: None.
    """
    if forwarder_state is None:
        return
    turn_id = _turn_id_from_payload(params)
    if not turn_id or forwarder_state.has_posted_user_message(turn_id):
        return
    codex_client = forwarder_state.codex_client
    thread_id = _thread_id_from_params(params)
    if codex_client is None or thread_id is None:
        return
    try:
        response = await codex_client.request("thread/resume", {"threadId": thread_id})
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - degrade to current behavior on resume failure.
        _logger.warning(
            "Codex forwarder could not resume to recover user message: thread=%s turn=%s",
            thread_id,
            turn_id,
            exc_info=True,
        )
        return
    user_item = _find_turn_user_message(response, turn_id)
    if user_item is None:
        return
    recovered_params: dict[str, Any] = {
        "threadId": thread_id,
        "turnId": turn_id,
        "item": user_item,
    }
    if not _claim_completed_item(recovered_params, user_item, forwarder_state):
        return
    await _post_user_message(client, session_id, recovered_params, user_item)
    forwarder_state.note_user_message_posted(turn_id)

def _codex_tool_call_from_item(item: dict[str, Any]) -> _CodexToolCall | None:
    """
    Translate a completed Codex tool item into a normalized tool call.

    :param item: Codex tool item from an ``item/completed`` notification,
        e.g. ``{"type": "commandExecution", "id": "call_abc", ...}``.
    :returns: Normalized tool call, or ``None`` for a malformed item that
        should be dropped rather than mirrored with invented fields.
    """
    call_id = item.get("id")
    item_type = item.get("type")
    if not isinstance(call_id, str) or not call_id:
        _logger.warning("Codex tool item missing string id: type=%s", item_type)
        return None
    builder = _TOOL_ITEM_BUILDERS.get(item_type) if isinstance(item_type, str) else None
    if builder is None:
        return None
    return builder(call_id, item)

def _command_execution_tool_call(call_id: str, item: dict[str, Any]) -> _CodexToolCall | None:
    """
    Build a tool call from a Codex ``commandExecution`` item.

    :param call_id: Codex item id, e.g. ``"call_abc"``.
    :param item: Codex ``commandExecution`` item, e.g.
        ``{"command": "/bin/zsh -lc 'pwd'", "cwd": "/repo",
        "aggregatedOutput": "/repo\n", "exitCode": 0}``.
    :returns: Normalized tool call, or ``None`` when the command is
        missing.
    """
    command = item.get("command")
    if not isinstance(command, str) or not command:
        _logger.warning("Codex commandExecution missing command: call_id=%s", call_id)
        return None
    arguments: dict[str, Any] = {"command": command}
    cwd = item.get("cwd")
    if isinstance(cwd, str) and cwd:
        arguments["cwd"] = cwd
    output = item.get("aggregatedOutput")
    # A command that prints nothing (e.g. ``touch x``) legitimately has no
    # aggregated output; Codex reports that as "" or null. AP's
    # function_call_output requires a string, so "" is the faithful
    # representation of "no output captured" here — not an invented default.
    output_text = output if isinstance(output, str) else ""
    exit_code = item.get("exitCode")
    # Codex reports a non-zero exit separately from stdout/stderr; surface
    # it inline so a failed command does not look successful in the UI.
    if isinstance(exit_code, int) and exit_code != 0:
        suffix = f"[exit code: {exit_code}]"
        output_text = f"{output_text}\n{suffix}" if output_text else suffix
    return _CodexToolCall(call_id=call_id, name="shell", arguments=arguments, output=output_text)

def _file_change_tool_call(call_id: str, item: dict[str, Any]) -> _CodexToolCall | None:
    """
    Build a tool call from a Codex ``fileChange`` item.

    :param call_id: Codex item id, e.g. ``"call_abc"``.
    :param item: Codex ``fileChange`` item, e.g.
        ``{"changes": [{"path": "/repo/x.py", "kind": {"type": "add"},
        "diff": "print('hi')\n"}], "status": "completed"}``.
    :returns: Normalized tool call, or ``None`` when no changes are
        present.
    """
    changes = item.get("changes")
    if not isinstance(changes, list) or not changes:
        _logger.warning("Codex fileChange missing changes: call_id=%s", call_id)
        return None
    summary_lines: list[str] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        path = change.get("path")
        kind = change.get("kind")
        kind_type = kind.get("type") if isinstance(kind, dict) else None
        label = kind_type if isinstance(kind_type, str) and kind_type else "change"
        summary_lines.append(f"{label} {path}")
    output_text = "\n".join(summary_lines)
    return _CodexToolCall(
        call_id=call_id,
        name="apply_patch",
        arguments={"changes": changes},
        output=output_text,
    )

def _web_search_tool_call(call_id: str, item: dict[str, Any]) -> _CodexToolCall | None:
    """
    Build a tool call from a Codex ``webSearch`` item.

    Codex does not surface the search results, so the queries it ran are
    the only result data available and are used as the output text.

    :param call_id: Codex item id, e.g. ``"ws_abc"``.
    :param item: Codex ``webSearch`` item, e.g.
        ``{"query": "python latest version",
        "action": {"type": "search", "queries": ["python latest"]}}``.
    :returns: Normalized tool call, or ``None`` when no query is present.
    """
    query = item.get("query")
    action = item.get("action")
    queries = action.get("queries") if isinstance(action, dict) else None
    query_list = [q for q in queries if isinstance(q, str)] if isinstance(queries, list) else []
    if not query_list and isinstance(query, str) and query:
        query_list = [query]
    if not query_list:
        _logger.warning("Codex webSearch missing query: call_id=%s", call_id)
        return None
    return _CodexToolCall(
        call_id=call_id,
        name="web_search",
        arguments={"query": query_list[0]},
        output="\n".join(query_list),
    )

def _turn_id_from_payload(payload: object) -> str | None:
    """
    Extract a turn id from a Codex payload.

    :param payload: Codex notification params or nested turn object.
    :returns: Turn id, or ``None`` when absent.
    """
    if not isinstance(payload, dict):
        return None
    value = payload.get("id") or payload.get("turnId")
    return value if isinstance(value, str) and value else None

def _turn_status_from_params(params: dict[str, Any]) -> str | None:
    """
    Extract a Codex turn status from terminal notification params.

    :param params: Codex terminal params, e.g.
        ``{"turn": {"id": "turn_123", "status": "interrupted"}}``.
    :returns: Status string, e.g. ``"interrupted"``, or ``None``.
    """
    status: object = params.get("status")
    turn = params.get("turn")
    if isinstance(turn, dict):
        status = turn.get("status")
    if isinstance(status, dict):
        status = status.get("type") or status.get("status")
    return status if isinstance(status, str) and status else None

def _turn_status_is_interrupted(status: str | None) -> bool:
    """
    Return whether a Codex turn status represents user interruption.

    :param status: Codex turn status, e.g. ``"interrupted"``.
    :returns: ``True`` for interrupted/cancelled terminal statuses.
    """
    if status is None:
        return False
    normalized = status.replace("_", "").replace("-", "").lower()
    return normalized in {"interrupted", "cancelled", "canceled"}

def _params_with_turn_id(params: dict[str, Any], turn_id: str) -> dict[str, Any]:
    """
    Return params with a top-level ``turnId`` for Omnigent response ids.

    :param params: Codex notification params.
    :param turn_id: Codex turn id, e.g. ``"turn_123"``.
    :returns: Shallow-copied params containing ``turnId``.
    """
    scoped = dict(params)
    scoped["turnId"] = turn_id
    return scoped

def _user_message_text(item: dict[str, Any]) -> str:
    """
    Convert a Codex ``userMessage`` item into plain text.

    :param item: Codex ``userMessage`` item.
    :returns: Joined text content.
    """
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n\n".join(parts)

def _user_message_has_file_content(item: dict[str, Any]) -> bool:
    """
    Return whether a Codex ``userMessage`` carries a non-text block.

    Codex echoes an attached image/file as a non-text content block (an
    image-only message arrives as ``[{"type": "image", "url": ...}]`` with
    no text block). Callers use this to decide whether a text-less message
    is still real and must be persisted, versus a genuinely empty one.

    :param item: Codex ``userMessage`` item.
    :returns: ``True`` when any content block is a non-text (image/file)
        block, ``False`` otherwise.
    """
    content = item.get("content")
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if isinstance(block_type, str) and block_type and block_type != "text":
            return True
    return False

def _is_codex_skill_wrapper(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("<skill>") and stripped.endswith("</skill>")

def _json_string(value: dict[str, Any]) -> str | None:
    """
    Serialize a dict for OpenAI-compatible function call arguments.

    :param value: JSON-serializable dictionary, e.g.
        ``{"command": "pwd"}``.
    :returns: JSON string, or ``None`` when serialization fails.
    """
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return None

def _plan_text_from_update(params: dict[str, Any]) -> str | None:
    """
    Render a Codex ``turn/plan/updated`` payload as Markdown text.

    :param params: Codex plan update params.
    :returns: Markdown plan text, or ``None`` when no valid plan steps
        are present.
    """
    plan = params.get("plan")
    if not isinstance(plan, list) or not plan:
        return None
    lines: list[str] = []
    explanation = params.get("explanation")
    if isinstance(explanation, str) and explanation:
        lines.append(explanation)
        lines.append("")
    lines.append("Plan:")
    for entry in plan:
        if not isinstance(entry, dict):
            continue
        step = entry.get("step")
        if not isinstance(step, str) or not step:
            continue
        status = entry.get("status")
        marker = _plan_status_marker(status)
        lines.append(f"{marker} {step}")
    if len(lines) == 1 or (len(lines) == 3 and lines[-1] == "Plan:"):
        return None
    return "\n".join(lines)

def _plan_status_marker(status: Any) -> str:
    """
    Return a readable Markdown marker for a Codex plan step status.

    :param status: Codex step status value.
    :returns: Markdown list marker.
    """
    if status == "completed":
        return "- [x]"
    if status in {"inProgress", "in_progress"}:
        return "- [~]"
    return "- [ ]"

def _response_id(params: dict[str, Any]) -> str:
    """
    Build a stable Omnigent response id for a Codex notification.

    :param params: Codex notification params.
    :returns: Response id, e.g. ``"codex_turn_abc123"``.
    """
    turn_id = params.get("turnId")
    if isinstance(turn_id, str) and turn_id:
        return f"codex_{turn_id}"
    return "codex_native"

def _source_id(params: dict[str, Any], item: dict[str, Any]) -> str:
    """
    Build a stable per-record label for one Codex item.

    Only used for debug-log correlation — it is not sent to the server
    and is not a dedup key (the server persists external items with a
    random primary key).

    :param params: Codex notification params.
    :param item: Codex item payload.
    :returns: Record label, e.g. ``"turn_abc:item_xyz"``.
    """
    turn_id = params.get("turnId")
    item_id = item.get("id")
    left = turn_id if isinstance(turn_id, str) and turn_id else "thread"
    right = item_id if isinstance(item_id, str) and item_id else "item"
    return f"{left}:{right}"

def _completed_item_key(
    params: dict[str, Any],
    item: dict[str, Any],
    forwarder_state: _CodexForwarderState,
) -> tuple[str, bool]:
    """
    Build a total dedup key for one durable Codex transcript item.

    The key is always non-empty so dedup is never silently disabled.
    Items with stable Codex-assigned ``id`` fields use
    ``threadId:turnId:item.id`` — identical across replay and live
    deliveries of the same item, so the second delivery is correctly
    dropped by the dedup gate.

    Items without a stable ``id`` fall back to a per-(thread, turn)
    positional counter. The counter is peeked here and only advanced by
    the caller after a successful claim. This guarantees *distinctness
    within a turn* (two genuinely different anonymous items get different
    keys) and ensures the key is never ``None`` (which would silently
    disable dedup). It does **not** guarantee cross-delivery dedup for
    anonymous items: if replay and live each deliver an anonymous item in
    the same (thread, turn), both advance the counter from the same
    starting value and therefore collide — one will be dropped. However,
    because Codex emits a stable ``id`` on all durable transcript items
    in practice, this anonymous path is a safety net for malformed events,
    not a primary dedup mechanism.

    :param params: Codex ``item/completed`` params.
    :param item: Codex item payload.
    :param forwarder_state: Mutable state holding per-(thread, turn)
        anonymous item counters.
    :returns: ``(key, is_anonymous)`` where ``key`` is the dedup key and
        ``is_anonymous`` is ``True`` when a positional counter was used.
    """
    thread_id = _thread_id_from_params(params) or "thread"
    turn_id = params.get("turnId")
    turn_id = turn_id if isinstance(turn_id, str) and turn_id else "turn"
    item_id = item.get("id")
    if isinstance(item_id, str) and item_id:
        return f"{thread_id}:{turn_id}:{item_id}", False
    return forwarder_state.peek_anon_item_key(thread_id, turn_id), True


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _collab as _sib_collab
    from . import _deltas as _sib_deltas
    from . import _elicitation as _sib_elicitation
    from . import _events as _sib_events
    from . import _fwd_state as _sib_fwd_state
    from . import _helpers as _sib_helpers
    from . import _posting as _sib_posting
    from . import _resume as _sib_resume
    from . import _supervisor as _sib_supervisor
    for _key, _value in _sib_collab.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_deltas.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_elicitation.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_events.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_fwd_state.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_helpers.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_posting.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_resume.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_supervisor.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
