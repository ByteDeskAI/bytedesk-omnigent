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

async def _post_resume_terminal_status(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    thread_id: str | None,
    turns: list[Any],
) -> None:
    """
    Publish a missing terminal status edge from ``thread/resume`` data.

    A reconnect can miss the live ``turn/started`` and
    ``turn/completed`` / ``turn/failed`` notifications. When the resume
    payload explicitly says the latest turn on the current thread is
    terminal, the forwarder can close the Omnigent session status even though no
    live terminal boundary was observed. It deliberately does not infer
    terminal state from transcript items alone.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory.
    :param thread_id: Codex thread id from the resume payload, e.g.
        ``"thread_123"``.
    :param turns: Raw Codex resume turn list.
    :returns: None.
    """
    if thread_id is None:
        return
    edge = _resume_terminal_status_edge_for_latest_turn(bridge_dir, thread_id, turns)
    await _post_turn_status_edge(client, session_id, edge)

async def _post_interrupted_partial_agent_message(
    client: httpx.AsyncClient,
    session_id: str,
    params: dict[str, Any],
    text: str,
) -> None:
    """
    Persist an interrupted Codex turn's visible partial assistant text.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param params: Codex turn params including ``turnId``.
    :param text: Partial assistant text, e.g. ``"The answer is"``.
    :returns: None.
    """
    await _post_external_item(
        client,
        session_id,
        item_type="message",
        item_data={
            "role": "assistant",
            "agent": _AGENT_NAME,
            "interrupted": True,
            "content": [{"type": "output_text", "text": text}],
        },
        response_id=_response_id(params),
    )

async def _post_user_message(
    client: httpx.AsyncClient,
    session_id: str,
    params: dict[str, Any],
    item: dict[str, Any],
) -> None:
    """
    Persist a Codex user message observed from the TUI.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param params: Codex notification params.
    :param item: Codex ``userMessage`` item.
    :returns: None.
    """
    text = _user_message_text(item)
    # An image/file-only message has no text but must still be posted: the
    # server drains its optimistic pending-input entry (FIFO) and folds the
    # image in by file_id (``_merge_pending_file_blocks``). Bailing here would
    # leak the pending entry — the user bubble would never persist (rendering
    # the reply above the dangling image) and the NEXT message would drain
    # this stale entry, folding the prior image into it. Only a truly empty
    # message (no text, no file block) is skipped.
    has_file_block = _user_message_has_file_content(item)
    if not text and not has_file_block:
        return
    # Text-only / text+image post the text; image-only posts empty content and
    # relies on the server-side pending fold to supply the image block.
    content: list[dict[str, Any]] = [{"type": "input_text", "text": text}] if text else []
    item_data: dict[str, Any] = {
        "role": "user",
        "content": content,
    }
    if _is_codex_skill_wrapper(text):
        item_data["is_meta"] = True
        _logger.debug(
            "Marked Codex skill wrapper as meta for session=%s source_id=%s",
            session_id,
            _source_id(params, item),
        )
    await _post_external_item(
        client,
        session_id,
        item_type="message",
        item_data=item_data,
        response_id=_response_id(params),
    )

async def _post_agent_message(
    client: httpx.AsyncClient,
    session_id: str,
    params: dict[str, Any],
    item: dict[str, Any],
) -> None:
    """
    Persist a Codex assistant message observed from the TUI/app-server.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param params: Codex notification params.
    :param item: Codex ``agentMessage`` item.
    :returns: None.
    """
    text = item.get("text")
    if not isinstance(text, str) or not text:
        return
    await _post_external_item(
        client,
        session_id,
        item_type="message",
        item_data={
            "role": "assistant",
            "agent": _AGENT_NAME,
            "content": [{"type": "output_text", "text": text}],
        },
        response_id=_response_id(params),
    )

async def _post_tool_item(
    client: httpx.AsyncClient,
    session_id: str,
    params: dict[str, Any],
    item: dict[str, Any],
) -> None:
    """
    Mirror one completed Codex built-in tool call into Omnigent history.

    A native Codex session runs Codex's own tools (shell commands, file
    edits, web search) rather than client-tunneled dynamic tools, so a
    single ``item/completed`` notification carries both the invocation
    and its result. This translates that one item into the AP
    ``function_call`` / ``function_call_output`` pair the web UI renders.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param params: Codex ``item/completed`` params.
    :param item: Codex tool item, e.g.
        ``{"type": "commandExecution", "id": "call_abc",
        "command": "/bin/zsh -lc 'pwd'", "aggregatedOutput": "/repo\n",
        "exitCode": 0}``.
    :returns: None.
    """
    tool_call = _codex_tool_call_from_item(item)
    if tool_call is None:
        return
    arguments_text = _json_string(tool_call.arguments)
    if arguments_text is None:
        _logger.warning(
            "Codex tool call arguments are not JSON serializable: call_id=%s tool=%s",
            tool_call.call_id,
            tool_call.name,
        )
        return
    await _post_external_item(
        client,
        session_id,
        item_type="function_call",
        item_data={
            "agent": _AGENT_NAME,
            "name": tool_call.name,
            "arguments": arguments_text,
            "call_id": tool_call.call_id,
        },
        response_id=_response_id(params),
    )
    await _post_external_item(
        client,
        session_id,
        item_type="function_call_output",
        item_data={"call_id": tool_call.call_id, "output": tool_call.output},
        response_id=_response_id(params),
    )

async def _post_plan_item(
    client: httpx.AsyncClient,
    session_id: str,
    params: dict[str, Any],
    item: dict[str, Any],
) -> None:
    """
    Persist one completed Codex plan item as assistant text.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param params: Codex ``item/completed`` params.
    :param item: Codex ``plan`` thread item.
    :returns: None.
    """
    text = item.get("text")
    if not isinstance(text, str) or not text:
        return
    await _post_external_item(
        client,
        session_id,
        item_type="message",
        item_data={
            "role": "assistant",
            "agent": _AGENT_NAME,
            "content": [{"type": "output_text", "text": text}],
        },
        response_id=_response_id(params),
    )

async def _post_external_item(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    item_type: str,
    item_data: dict[str, Any],
    response_id: str,
) -> None:
    """
    Post one external conversation item to AP.

    The forwarder does not send a dedup key to the server — items are
    persisted with a random primary key. Avoiding re-posts on resume is
    the producer's own responsibility.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param item_type: Conversation item type, e.g. ``"message"``.
    :param item_data: Conversation item payload.
    :param response_id: Response id for the mirrored Codex turn.
    :returns: None.
    """
    response = await _post_session_event(
        client,
        session_id,
        event_type="external_conversation_item",
        data={
            "item_type": item_type,
            "item_data": item_data,
            "response_id": response_id,
        },
    )
    if response is None:
        _logger.warning("failed to post Codex conversation item")
        return
    if response.status_code >= 400:
        _logger.warning(
            "failed to post Codex conversation item: status=%s body=%s",
            response.status_code,
            response.text[:1000],
        )

async def _post_status(
    client: httpx.AsyncClient,
    session_id: str,
    status: str,
    *,
    response_id: str | None = None,
) -> None:
    """
    Publish a native Codex status edge.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param status: Session status, e.g. ``"running"``.
    :param response_id: Optional response id for this status edge,
        e.g. ``"codex_turn_abc123"``.
    :returns: None.
    """
    data = {"status": status}
    if response_id is not None:
        data["response_id"] = response_id
    response = await _post_session_event(
        client,
        session_id,
        event_type="external_session_status",
        data=data,
    )
    _log_failed_session_event_post("external_session_status", response)

async def _post_turn_status_edge(
    client: httpx.AsyncClient,
    session_id: str,
    edge: _CodexTurnStatusEdge | None,
) -> None:
    """
    Publish one Codex turn lifecycle edge if a valid edge was derived.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param edge: Derived lifecycle edge, or ``None`` when no status should
        be published.
    :returns: None.
    """
    if edge is None:
        return
    _logger.info(
        "Codex forwarder publishing turn status: source=%s turn_id=%s status=%s",
        edge.source,
        edge.turn_id,
        edge.status,
    )
    response_id = _response_id(_params_with_turn_id({}, edge.turn_id)) if edge.turn_id else None
    await _post_status(client, session_id, edge.status, response_id=response_id)

async def _post_output_text_delta(
    client: httpx.AsyncClient,
    session_id: str,
    delta: str,
    *,
    message_id: str | None = None,
    index: int | None = None,
    final: bool | None = None,
) -> None:
    """
    Publish a transient Codex assistant text delta.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param delta: Assistant text fragment, e.g. ``"hello"``.
    :param message_id: Optional stable native message stream id,
        e.g. ``"codex:thread_123:turn_123:agentMessage:item_agent"``.
    :param index: Optional zero-based chunk index for ``message_id``,
        e.g. ``0``.
    :param final: Optional final-chunk marker for ``message_id``,
        e.g. ``False``.
    :returns: None.
    """
    data: dict[str, Any] = {"delta": delta}
    if message_id is not None:
        data["message_id"] = message_id
    if index is not None:
        data["index"] = index
    if final is not None:
        data["final"] = final
    response = await _post_session_event(
        client,
        session_id,
        event_type="external_output_text_delta",
        data=data,
    )
    _log_failed_session_event_post("external_output_text_delta", response)

async def _post_session_interrupted(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    response_id: str | None = None,
) -> None:
    """
    Publish a Codex-observed interrupted-turn signal into AP.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param response_id: Optional interrupted response id, e.g.
        ``"codex_turn_abc123"``.
    :returns: None.
    """
    data: dict[str, Any] = {}
    if response_id is not None:
        data["response_id"] = response_id
    response = await _post_session_event(
        client,
        session_id,
        event_type=_EXTERNAL_SESSION_INTERRUPTED_TYPE,
        data=data,
    )
    _log_failed_session_event_post(_EXTERNAL_SESSION_INTERRUPTED_TYPE, response)

async def _post_session_event(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    event_type: str,
    data: dict[str, Any],
) -> httpx.Response | None:
    """
    Post one Omnigent session event with bounded transient retries.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param event_type: Session event type, e.g.
        ``"external_conversation_item"``.
    :param data: Event data payload, e.g.
        ``{"status": "running"}``.
    :returns: Final HTTP response, or ``None`` when all attempts raised
        transport errors — or, for ``external_conversation_item``, after
        a single ambiguous transport failure (the item may already be
        committed server-side, so retrying risks a duplicate).
    """
    url = f"/v1/sessions/{url_component(session_id)}/events"
    payload = {"type": event_type, "data": data}
    for attempt in range(1, _POST_MAX_ATTEMPTS + 1):
        try:
            response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            # Conversation items persist with a random primary key and no
            # server-side dedup, so an ambiguous failure (request sent,
            # response lost — the server may have committed it) must not
            # be retried: a re-post would duplicate the item.
            # Other event types are idempotent / transient, so retrying
            # them on the same errors is safe and preserves delivery.
            if event_type == "external_conversation_item" and post_may_have_been_delivered(exc):
                _logger.warning(
                    "skipping Codex session event after an ambiguous transport "
                    "failure (may already be committed); not retrying to avoid "
                    "a duplicate: type=%s error=%r",
                    event_type,
                    exc,
                )
                return None
            if _is_final_post_attempt(attempt):
                _log_post_transport_failure(event_type, exc)
                return None
            await _sleep(_post_retry_delay(attempt))
            continue
        if _post_response_is_final(response, attempt):
            return response
        await _sleep(_post_retry_delay(attempt))
    return None

def _post_response_is_final(response: httpx.Response, attempt: int) -> bool:
    """
    Return whether a session-event POST response should stop retries.

    :param response: HTTP response from AP.
    :param attempt: One-based attempt number, e.g. ``1``.
    :returns: ``True`` when the caller should return ``response``.
    """
    if response.status_code < 400:
        return True
    if not _should_retry_post_status(response.status_code):
        return True
    return _is_final_post_attempt(attempt)

def _log_post_transport_failure(event_type: str, exc: httpx.HTTPError) -> None:
    """
    Log an exhausted Omnigent session-event transport failure.

    :param event_type: Session event type, e.g.
        ``"external_conversation_item"``.
    :param exc: Final transport error.
    :returns: None.
    """
    _logger.warning(
        "failed to post Codex session event after retries: type=%s attempts=%s error=%r",
        event_type,
        _POST_MAX_ATTEMPTS,
        exc,
    )

def _log_failed_session_event_post(
    event_type: str,
    response: httpx.Response | None,
) -> None:
    """
    Log failed best-effort session events such as status and usage.

    :param event_type: Session event type, e.g.
        ``"external_session_status"``.
    :param response: Final Omnigent response, or ``None`` after transport
        errors exhausted all retries.
    :returns: None.
    """
    if response is None:
        _logger.warning("failed to post Codex session event: type=%s", event_type)
        return
    if response.status_code >= 400:
        _logger.warning(
            "failed to post Codex session event: type=%s status=%s body=%s",
            event_type,
            response.status_code,
            response.text[:1000],
        )

def _should_retry_post_status(status_code: int) -> bool:
    """
    Return whether an Omnigent event POST status is transient.

    :param status_code: HTTP status code, e.g. ``503``.
    :returns: ``True`` when the forwarder should retry.
    """
    return status_code in _POST_RETRY_STATUS_CODES

def _post_retry_delay(attempt: int) -> float:
    """
    Return the retry delay for a failed Omnigent event POST attempt.

    :param attempt: One-based failed attempt number, e.g. ``1``.
    :returns: Delay in seconds before the next attempt.
    """
    return _POST_RETRY_DELAY_SECONDS * attempt


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _collab as _sib_collab
    from . import _deltas as _sib_deltas
    from . import _elicitation as _sib_elicitation
    from . import _events as _sib_events
    from . import _fwd_state as _sib_fwd_state
    from . import _helpers as _sib_helpers
    from . import _resume as _sib_resume
    from . import _supervisor as _sib_supervisor
    from . import _turn as _sib_turn
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
    for _key, _value in _sib_resume.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_supervisor.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_turn.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
