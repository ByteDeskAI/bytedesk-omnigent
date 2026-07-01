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

async def _handle_event(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    event: CodexMessage,
    usage_coalescer: _SessionUsageCoalescer,
    elicitation_tracker: _CodexElicitationTaskTracker,
    delta_coalescer: _OutputTextDeltaCoalescer | None = None,
    expected_thread_id: str | None = None,
    codex_client: CodexAppServerClient | None = None,
    forwarder_state: _CodexForwarderState | None = None,
) -> None:
    """
    Forward one Codex app-server notification.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory.
    :param event: Codex notification envelope.
    :param usage_coalescer: Coalescer for high-frequency usage
        notifications.
    :param elicitation_tracker: Background Codex elicitation tracker.
    :param delta_coalescer: Optional coalescer for high-frequency
        assistant text deltas.
    :param expected_thread_id: Current Codex thread id. When provided,
        events carrying a different ``threadId`` are stale and ignored.
    :param codex_client: Optional Codex app-server client. Required
        when ``event`` is a server-to-client request that needs a
        JSON-RPC response.
    :param forwarder_state: Optional mutable state for Plan-mode prompt
        synthesis and thread setting tracking.
    :returns: None.
    """
    method = event.get("method")
    params = event.get("params")
    if not isinstance(method, str) or not isinstance(params, dict):
        return
    if forwarder_state is not None and _thread_started_is_subagent(event):
        child_thread_id = _thread_id_from_started_event(event)
        if child_thread_id is not None:
            forwarder_state.note_pending_child_thread(
                child_thread_id,
                _parent_thread_id_from_started_event(event),
            )
        return
    # Resolve routing: parent thread, known child thread, or stale/ignored.
    route_session_id, is_child = _resolve_event_session(
        params, method, expected_thread_id, forwarder_state, fallback_session_id=session_id
    )
    if route_session_id is None:
        return
    # item/started: register collab-agent children early (before item/completed)
    # so live child events can be routed to the child session immediately.
    # Only meaningful for the parent thread; children don't spawn grandchildren here.
    if method == "item/started" and not is_child and forwarder_state is not None:
        item = params.get("item")
        if isinstance(item, dict) and item.get("type") == _CODEX_COLLAB_AGENT_ITEM_TYPE:
            await _handle_collab_item(client, params, item, forwarder_state)
        elif isinstance(item, dict) and item.get("type") == "agentMessage":
            # Post the turn's user message NOW — before the assistant's text
            # deltas start streaming. The live ``userMessage`` event can be
            # missed on a fresh thread (subscription lands after it fires);
            # if recovery waited until the assistant's ``item/completed``,
            # the deltas would stream into a transient assistant bubble that
            # renders ABOVE the still-pending user bubble until the turn
            # reconciles. Recovering at assistant-start commits the user
            # message first (it has already materialized in the rollout by
            # now), so the web UI renders the question above the reply. The
            # ``item/completed`` guard below remains the backstop for the
            # resume-backfill path, which replays only ``item/completed``.
            await _ensure_user_message_posted(client, route_session_id, params, forwarder_state)
        return
    if method == _CODEX_SERVER_REQUEST_RESOLVED_METHOD:
        # Resolve on the session the elicitation was published on (a child
        # thread when is_child), not the parent — otherwise a child-thread
        # approval card never flips for the web user watching the child.
        await elicitation_tracker.resolve_by_server_notification(
            client,
            session_id=route_session_id,
            params=params,
        )
        return
    if await _maybe_handle_codex_request(
        client,
        session_id=route_session_id,
        event=event,
        method=method,
        delta_coalescer=delta_coalescer if not is_child else None,
        elicitation_tracker=elicitation_tracker,
        codex_client=codex_client,
        forwarder_state=forwarder_state,
    ):
        return
    # Child token-usage events must post to the child session, not the
    # parent's coalescer. A fresh coalescer is created per-event for
    # children and flushed immediately so accumulated data is not lost.
    # Seed it with the session model so the server can price the child's tokens.
    child_coalescer = (
        _SessionUsageCoalescer(
            client,
            route_session_id,
            model=forwarder_state.model if forwarder_state is not None else None,
        )
        if is_child
        else None
    )
    if await _maybe_handle_turn_event(
        client,
        session_id=route_session_id,
        bridge_dir=bridge_dir if not is_child else Path(),
        method=method,
        params=params,
        usage_coalescer=child_coalescer if is_child else usage_coalescer,
        delta_coalescer=delta_coalescer if not is_child else None,
        elicitation_tracker=elicitation_tracker,
        codex_client=codex_client,
        forwarder_state=forwarder_state if not is_child else None,
    ):
        if child_coalescer is not None:
            await child_coalescer.flush()
        return
    if not is_child and await _maybe_handle_delta_event(
        client,
        session_id=route_session_id,
        bridge_dir=bridge_dir,
        method=method,
        params=params,
        delta_coalescer=delta_coalescer,
        forwarder_state=forwarder_state,
    ):
        return
    if method == "item/completed":
        await _handle_completed_event(
            client,
            session_id=route_session_id,
            params=params,
            delta_coalescer=delta_coalescer if not is_child else None,
            forwarder_state=forwarder_state,
        )

def _resolve_event_session(
    params: dict[str, Any],
    method: str,
    expected_thread_id: str | None,
    forwarder_state: _CodexForwarderState | None,
    *,
    fallback_session_id: str,
) -> tuple[str | None, bool]:
    """
    Resolve which Omnigent session should receive a Codex event.

    Returns ``(session_id, is_child)`` where ``session_id`` is ``None``
    when the event should be silently dropped (stale or unrecognized
    thread). ``is_child`` is ``True`` when the event belongs to a known
    Codex child thread rather than the parent.

    :param params: Codex notification params.
    :param method: Codex method value, e.g. ``"item/completed"``.
    :param expected_thread_id: Active parent Codex thread id, e.g.
        ``"thread_parent"``.
    :param forwarder_state: Optional state holding child-thread mappings.
    :param fallback_session_id: Parent session id used when
        ``forwarder_state`` has no ``parent_session_id`` (e.g. in tests
        that call ``_handle_event`` directly).
    :returns: ``(route_session_id, is_child)`` tuple.
    """
    event_thread_id = _thread_id_from_params(params)
    # Route to a known child session when the event targets a child thread.
    if forwarder_state is not None and event_thread_id is not None:
        child_session_id = forwarder_state.session_for_child_thread(event_thread_id)
        if child_session_id is not None:
            return child_session_id, True
    parent_session_id = (
        forwarder_state.parent_session_id
        if forwarder_state is not None and forwarder_state.parent_session_id is not None
        else fallback_session_id
    )
    # Approval requests from announced child threads must not be dropped just
    # because AP child-session registration is racing behind the request
    # frame. Unknown non-parent threads still hit the stale-thread guard below;
    # only ``thread/started`` events with ``source.subAgent.thread_spawn`` earn
    # this temporary parent routing.
    targets_unregistered_thread = (
        expected_thread_id is not None
        and event_thread_id is not None
        and event_thread_id != expected_thread_id
    )
    targets_pending_child_thread = (
        forwarder_state is not None
        and event_thread_id is not None
        and forwarder_state.is_pending_child_thread(event_thread_id, expected_thread_id)
    )
    if (
        method in _CODEX_ELICITATION_REQUEST_METHODS
        and targets_unregistered_thread
        and targets_pending_child_thread
    ):
        _logger.info(
            "Codex forwarder routed unregistered child-thread elicitation to parent: "
            "method=%s event_thread=%s active_thread=%s",
            method,
            event_thread_id,
            expected_thread_id,
        )
        return parent_session_id, False
    # Drop stale events for threads that are neither the parent nor a child.
    if _event_targets_different_thread(params, method, expected_thread_id):
        return None, False
    return parent_session_id, False

def _event_targets_different_thread(
    params: dict[str, Any],
    method: str,
    expected_thread_id: str | None,
) -> bool:
    """
    Return whether an event belongs to a stale Codex thread.

    :param params: Codex notification params.
    :param method: Codex method value, e.g. ``"item/completed"``.
    :param expected_thread_id: Active Codex thread id, e.g.
        ``"thread_123"``.
    :returns: ``True`` when the event should be ignored as stale.
    """
    event_thread_id = _thread_id_from_params(params)
    if expected_thread_id is None or event_thread_id is None:
        return False
    if event_thread_id == expected_thread_id:
        return False
    _logger.info(
        "Codex forwarder ignored stale thread event: method=%s event_thread=%s active_thread=%s",
        method,
        event_thread_id,
        expected_thread_id,
    )
    return True

async def _maybe_handle_codex_request(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    event: CodexMessage,
    method: str,
    delta_coalescer: _OutputTextDeltaCoalescer | None,
    elicitation_tracker: _CodexElicitationTaskTracker,
    codex_client: CodexAppServerClient | None,
    forwarder_state: _CodexForwarderState | None,
) -> bool:
    """
    Handle Codex server-to-client requests if this event is one.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param event: Codex notification/request envelope.
    :param method: Codex method value, e.g.
        ``"item/tool/requestUserInput"``.
    :param delta_coalescer: Optional text coalescer to flush before a
        blocking request.
    :param elicitation_tracker: Background Codex elicitation tracker.
    :param codex_client: Optional app-server client used to answer
        JSON-RPC requests.
    :param forwarder_state: Optional Plan-mode prompt state.
    :returns: ``True`` when the event was a request and needs no
        further dispatch.
    """
    if _is_codex_elicitation_request(event):
        if codex_client is None:
            _logger.warning(
                "Codex forwarder cannot answer elicitation request without app-server client: "
                "method=%s",
                method,
            )
            return True
        if delta_coalescer is not None:
            await delta_coalescer.flush()
        if forwarder_state is not None:
            _note_native_plan_implementation_prompt(forwarder_state, event)
        elicitation_tracker.start(
            client,
            codex_client,
            session_id=session_id,
            event=event,
        )
        return True
    if isinstance(event.get("id"), int | str) and isinstance(method, str):
        _logger.warning("Codex forwarder ignored unsupported server request: method=%s", method)
        return True
    return False

async def _maybe_handle_turn_event(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    method: str,
    params: dict[str, Any],
    usage_coalescer: _SessionUsageCoalescer,
    delta_coalescer: _OutputTextDeltaCoalescer | None,
    elicitation_tracker: _CodexElicitationTaskTracker,
    codex_client: CodexAppServerClient | None,
    forwarder_state: _CodexForwarderState | None,
) -> bool:
    """
    Handle turn/thread-level Codex events.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory.
    :param method: Codex method value, e.g. ``"turn/started"``.
    :param params: Codex notification params.
    :param usage_coalescer: Token-usage coalescer.
    :param delta_coalescer: Optional text-delta coalescer.
    :param elicitation_tracker: Background Codex elicitation tracker.
    :param codex_client: Optional app-server client for Plan prompts.
    :param forwarder_state: Optional forwarder state.
    :returns: ``True`` when this event was handled.
    """
    if method == "turn/started":
        if delta_coalescer is not None:
            await delta_coalescer.flush()
        await _handle_turn_started(client, session_id, bridge_dir, params)
        if forwarder_state is not None:
            # An in-TUI ``/model`` switch writes config.toml (the cost-policy
            # source of truth) but emits no notification. Re-read it at turn
            # start so a switch made since the last turn lands ``model_override``
            # on Omnigent before this turn's first tool call reaches the cost gate.
            _refresh_model_from_config(bridge_dir, forwarder_state)
            await _sync_model_change(
                client, session_id=session_id, forwarder_state=forwarder_state
            )
        return True
    if method in {"turn/completed", "turn/failed"}:
        await _handle_terminal_turn_boundary(
            client,
            session_id=session_id,
            bridge_dir=bridge_dir,
            method=method,
            params=params,
            usage_coalescer=usage_coalescer,
            delta_coalescer=delta_coalescer,
            elicitation_tracker=elicitation_tracker,
            codex_client=codex_client,
            forwarder_state=forwarder_state,
        )
        return True
    if method == "thread/tokenUsage/updated":
        _handle_usage_update(usage_coalescer, params, forwarder_state)
        # Flush immediately so the web UI cost badge updates live mid-turn.
        # Codex emits these only every few seconds; the coalescer dedups, so the
        # turn-boundary flush becomes a cheap no-op.
        await usage_coalescer.flush()
        return True
    if method == "thread/settings/updated":
        if forwarder_state is not None:
            forwarder_state.note_thread_settings_updated(params)
            await _sync_model_change(
                client, session_id=session_id, forwarder_state=forwarder_state
            )
        return True
    if method == "turn/plan/updated":
        if delta_coalescer is not None:
            await delta_coalescer.flush()
        await _handle_turn_plan_updated(client, session_id, params)
        return True
    return False

async def _maybe_handle_delta_event(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    method: str,
    params: dict[str, Any],
    delta_coalescer: _OutputTextDeltaCoalescer | None,
    forwarder_state: _CodexForwarderState | None,
) -> bool:
    """
    Handle Codex streaming text/plan delta events.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory.
    :param method: Codex method value, e.g.
        ``"item/agentMessage/delta"``.
    :param params: Codex notification params.
    :param delta_coalescer: Text-delta coalescer required for delta
        events.
    :param forwarder_state: Optional forwarder state used to recover a
        missed user message before streaming recovered assistant deltas.
    :returns: ``True`` when this event was a delta event.
    :raises RuntimeError: If a delta event arrives without a text
        coalescer.
    """
    if method == "item/agentMessage/delta":
        if delta_coalescer is None:
            raise RuntimeError("Codex assistant delta handling requires a text-delta coalescer")
        await _handle_agent_message_delta(
            client,
            session_id,
            bridge_dir,
            params,
            delta_coalescer,
            forwarder_state,
        )
        return True
    if method == "item/plan/delta":
        if delta_coalescer is None:
            raise RuntimeError("Codex plan delta handling requires a text-delta coalescer")
        await _handle_plan_delta(
            client,
            session_id,
            bridge_dir,
            params,
            delta_coalescer,
            forwarder_state,
        )
        return True
    return False

async def _handle_completed_event(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    params: dict[str, Any],
    delta_coalescer: _OutputTextDeltaCoalescer | None,
    forwarder_state: _CodexForwarderState | None,
) -> None:
    """
    Flush pending text and mirror one completed Codex item.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param params: Codex ``item/completed`` params.
    :param delta_coalescer: Optional text-delta coalescer to flush
        before the completed item.
    :param forwarder_state: Optional state that records completed
        Plan-mode items.
    :returns: None.
    """
    if delta_coalescer is not None:
        await delta_coalescer.flush()
    if forwarder_state is not None:
        forwarder_state.record_completed_plan(params)
    await _handle_completed_item(client, session_id, params, forwarder_state=forwarder_state)

async def _handle_terminal_turn_boundary(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    method: str,
    params: dict[str, Any],
    usage_coalescer: _SessionUsageCoalescer,
    delta_coalescer: _OutputTextDeltaCoalescer | None,
    elicitation_tracker: _CodexElicitationTaskTracker,
    codex_client: CodexAppServerClient | None,
    forwarder_state: _CodexForwarderState | None,
) -> None:
    """
    Handle a Codex terminal turn completion/failure boundary.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory.
    :param method: Codex method, e.g. ``"turn/completed"``.
    :param params: Codex notification params.
    :param usage_coalescer: Coalescer holding latest token usage.
    :param delta_coalescer: Optional text-delta coalescer to flush
        before terminal status and usage.
    :param elicitation_tracker: Background Codex elicitation tracker.
    :param codex_client: Optional app-server client used for
        synthesized Plan-mode implementation prompts.
    :param forwarder_state: Optional Plan-mode prompt state.
    :returns: None.
    """
    if delta_coalescer is not None:
        await delta_coalescer.flush()
    await _maybe_persist_interrupted_partial_text(
        client,
        session_id=session_id,
        method=method,
        params=params,
        forwarder_state=forwarder_state,
    )
    handled = await _handle_terminal_turn_event(client, session_id, bridge_dir, method, params)
    if handled:
        await elicitation_tracker.resolve_by_terminal_turn_event(
            client,
            session_id=session_id,
            params=params,
        )
    if (
        handled
        and method == "turn/completed"
        and codex_client is not None
        and forwarder_state is not None
    ):
        await _maybe_handle_plan_implementation_prompt(
            client,
            codex_client,
            session_id=session_id,
            bridge_dir=bridge_dir,
            params=params,
            forwarder_state=forwarder_state,
        )
    if handled:
        await usage_coalescer.flush()

def _handle_usage_update(
    usage_coalescer: _SessionUsageCoalescer,
    params: dict[str, Any],
    forwarder_state: _CodexForwarderState | None = None,
) -> None:
    """
    Record a Codex usage notification without blocking visible output.

    :param usage_coalescer: Coalescer receiving latest token usage.
    :param params: Codex ``thread/tokenUsage/updated`` params.
    :param forwarder_state: Optional forwarder state; its ``model`` is
        attached to the post so the server can price cumulative tokens.
    :returns: None.
    """
    model = forwarder_state.model if forwarder_state is not None else None
    usage_coalescer.record(params, model=model)

async def _handle_turn_plan_updated(
    client: httpx.AsyncClient,
    session_id: str,
    params: dict[str, Any],
) -> None:
    """
    Mirror a Codex plan update as a visible assistant message.

    Codex emits plan changes as app-server notifications rather than
    ordinary assistant text. Omnigent web currently renders persisted message
    items, not a dedicated plan item type, so the native bridge converts
    the structured plan into a compact assistant message.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param params: Codex ``turn/plan/updated`` params.
    :returns: None.
    """
    text = _plan_text_from_update(params)
    if not text:
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

async def _handle_turn_started(
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
    params: dict[str, Any],
) -> None:
    """
    Forward a Codex terminal turn start event.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory.
    :param params: Codex ``turn/started`` params.
    :returns: None.
    """
    edge = _turn_started_status_edge(bridge_dir, params)
    await _post_turn_status_edge(client, session_id, edge)

async def _handle_terminal_turn_event(
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
    method: str,
    params: dict[str, Any],
) -> bool:
    """
    Forward a terminal-observed Codex turn completion/failure event.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory.
    :param method: Codex method, e.g. ``"turn/completed"``.
    :param params: Codex turn event params.
    :returns: ``True`` when the terminal event belonged to the active
        turn and was forwarded, ``False`` when it was stale.
    """
    edge = _terminal_turn_status_edge(bridge_dir, method, params)
    if edge is None:
        terminal_turn_id = _terminal_turn_id_from_params(params)
        _logger.info(
            "Codex forwarder ignored stale terminal turn event: method=%s turn_id=%s",
            method,
            terminal_turn_id,
        )
        return False
    await _post_turn_status_edge(client, session_id, edge)
    return True

def _claim_completed_item(
    params: dict[str, Any],
    item: dict[str, Any],
    forwarder_state: _CodexForwarderState | None,
) -> bool:
    """
    Claim one completed Codex transcript item for Omnigent posting.

    Returns ``True`` when the caller should post the item; ``False`` when
    it was already posted this connection (dedup gate). Also advances the
    anonymous-item counter on a successful claim so the next anonymous
    item in the same (thread, turn) gets a fresh key.

    When ``forwarder_state`` is ``None``, dedup is disabled and the
    function always returns ``True`` (used in tests that bypass
    ``supervise_forwarder``).

    :param params: Codex ``item/completed`` params.
    :param item: Codex item payload.
    :param forwarder_state: Optional mutable state holding synced-item
        keys and anonymous-item counters.
    :returns: ``True`` when the item should be posted to AP.
    """
    if forwarder_state is None:
        return True
    item_key, is_anon = _completed_item_key(params, item, forwarder_state)
    if not forwarder_state.claim_item_key(item_key):
        return False
    if is_anon:
        thread_id = _thread_id_from_params(params) or "thread"
        turn_id = params.get("turnId")
        turn_id = turn_id if isinstance(turn_id, str) and turn_id else "turn"
        forwarder_state.advance_anon_counter(thread_id, turn_id)
    return True

async def _handle_completed_item(
    client: httpx.AsyncClient,
    session_id: str,
    params: dict[str, Any],
    *,
    forwarder_state: _CodexForwarderState | None = None,
) -> None:
    """
    Forward one Codex completed item event when it maps to Omnigent history.

    Deduplicates via ``_claim_completed_item`` so replay and live deliveries
    of the same item only write once. Collab items are dispatched separately.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param params: Codex ``item/completed`` params.
    :param forwarder_state: Optional mutable state for dedup tracking.
    :returns: None.
    """
    item = params.get("item")
    if not isinstance(item, dict):
        return
    item_type = item.get("type")
    turn_id = _turn_id_from_payload(params)
    if item_type in {"agentMessage", "plan"} and forwarder_state is not None and turn_id:
        item_id = item.get("id")
        forwarder_state.discard_partial_text_item(
            turn_id=turn_id,
            item_type=item_type,
            item_id=item_id if isinstance(item_id, str) and item_id else None,
        )
    _logger.info(
        "Codex forwarder observed completed item: turn_id=%s item_type=%s",
        turn_id,
        item_type,
    )
    # Collab-agent items register child sessions; they do not append transcript
    # records and must not go through the dedup gate.
    if item_type == _CODEX_COLLAB_AGENT_ITEM_TYPE:
        if forwarder_state is not None:
            await _handle_collab_item(client, params, item, forwarder_state)
        return
    if not _claim_completed_item(params, item, forwarder_state):
        return
    if item_type == "userMessage":
        await _post_user_message(client, session_id, params, item)
        if forwarder_state is not None:
            turn_id = _turn_id_from_payload(params)
            if turn_id:
                forwarder_state.note_user_message_posted(turn_id)
        return
    if item_type == "agentMessage":
        # User-before-assistant ordering guarantee. On a fresh thread the
        # forwarder subscribes via ``thread/resume`` only after the first
        # turn starts, so the early ``userMessage`` event can stream past
        # before the subscription lands — it is then recovered only via a
        # later resume backfill, which can post it AFTER this reply. Since
        # Omnigent assigns each mirrored item a position by POST arrival order
        # and the web UI renders strictly by position, that inverts the
        # bubbles. Recover and post the turn's user message first so it
        # always takes the earlier position.
        await _ensure_user_message_posted(client, session_id, params, forwarder_state)
        await _post_agent_message(client, session_id, params, item)
        return
    if item_type == "plan":
        await _post_plan_item(client, session_id, params, item)
        return
    if item_type in _TOOL_ITEM_TYPES:
        await _post_tool_item(client, session_id, params, item)

async def _handle_collab_item(
    client: httpx.AsyncClient,
    params: dict[str, Any],
    item: dict[str, Any],
    forwarder_state: _CodexForwarderState,
) -> None:
    """
    Handle a Codex ``collabAgentToolCall`` completed item.

    Registers newly discovered child threads and posts Omnigent status updates
    from the collab-agent state snapshot in the item. Does not write
    durable transcript records — the transcript for each child arrives
    via that child's own ``item/completed`` stream.

    :param client: HTTP client for Omnigent event posts.
    :param params: Codex ``item/completed`` params.
    :param item: Codex ``collabAgentToolCall`` item.
    :param forwarder_state: Mutable state for child-thread mappings.
    :returns: None.
    """
    if item.get("tool") != _CODEX_COLLAB_SPAWN_TOOL:
        return
    parent_session_id = _parent_session_id_from_forwarder_state(forwarder_state)
    if parent_session_id is None:
        return
    parent_thread_id = _collab_parent_thread_id(params, item)
    for child_thread_id in _collab_receiver_thread_ids(item):
        await _ensure_child_session(
            client,
            parent_session_id=parent_session_id,
            parent_thread_id=parent_thread_id,
            child_thread_id=child_thread_id,
            item=item,
            forwarder_state=forwarder_state,
        )
    await _post_collab_agent_statuses(client, item=item, forwarder_state=forwarder_state)

async def _handle_agent_message_delta(
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
    params: dict[str, Any],
    delta_coalescer: _OutputTextDeltaCoalescer,
    forwarder_state: _CodexForwarderState | None,
) -> None:
    """
    Forward one live Codex assistant text delta to AP.

    Codex app-server emits ``item/agentMessage/delta`` while a turn is
    running. Omnigent normally persists only the completed ``agentMessage`` item,
    so this path publishes a transient text-delta SSE event and relies on
    the later ``item/completed`` notification for durable completed-turn
    history. The same text is also buffered in memory so an interrupted turn
    that never emits a completed item can still persist the visible partial
    answer.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory.
    :param params: Codex ``item/agentMessage/delta`` params, e.g.
        ``{"turnId": "turn_123", "itemId": "item_123",
        "delta": "hi"}``.
    :param delta_coalescer: Coalescer for high-frequency assistant text
        deltas.
    :param forwarder_state: Optional forwarder state used to recover the
        turn's user message before streaming a recovered assistant delta.
    :returns: None.
    """
    turn_id = _turn_id_from_payload(params)
    delta = params.get("delta")
    if not isinstance(delta, str):
        _logger.warning("Codex agentMessage delta missing string delta: turn_id=%s", turn_id)
        return
    if not _is_active_turn_delta(bridge_dir, turn_id):
        edge = _delta_recovery_status_edge(bridge_dir, params, turn_id)
        if edge is not None:
            await _ensure_user_message_posted(client, session_id, params, forwarder_state)
            await _post_turn_status_edge(client, session_id, edge)
            _record_partial_text_delta(
                forwarder_state,
                turn_id=turn_id,
                item_type="agentMessage",
                item_id=_item_id_from_delta_params(params),
                delta=delta,
            )
            await delta_coalescer.append(
                delta,
                message_id=_streaming_message_id(params, "agentMessage"),
            )
            return
        _logger.info("Codex forwarder ignored stale assistant delta: turn_id=%s", turn_id)
        return
    _record_partial_text_delta(
        forwarder_state,
        turn_id=turn_id,
        item_type="agentMessage",
        item_id=_item_id_from_delta_params(params),
        delta=delta,
    )
    await delta_coalescer.append(
        delta,
        message_id=_streaming_message_id(params, "agentMessage"),
    )

async def _handle_plan_delta(
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
    params: dict[str, Any],
    delta_coalescer: _OutputTextDeltaCoalescer,
    forwarder_state: _CodexForwarderState | None,
) -> None:
    """
    Forward one live Codex plan text delta to AP.

    Plan mode streams visible plan prose through
    ``item/plan/delta`` rather than ``item/agentMessage/delta``.
    Omnigent uses the same transient output-text delta channel for both,
    and the later completed ``plan`` item or structured plan update
    provides the durable completed-turn transcript state. Interrupted turns
    consume the buffered deltas so the visible partial plan is still durable.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory.
    :param params: Codex ``item/plan/delta`` params, e.g.
        ``{"turnId": "turn_123", "itemId": "item_plan",
        "delta": "1. Inspect"}``.
    :param delta_coalescer: Coalescer for high-frequency assistant text
        deltas.
    :param forwarder_state: Optional forwarder state used to recover the
        turn's user message before streaming a recovered plan delta.
    :returns: None.
    """
    turn_id = _turn_id_from_payload(params)
    delta = params.get("delta")
    if not isinstance(delta, str):
        _logger.warning("Codex plan delta missing string delta: turn_id=%s", turn_id)
        return
    if not _is_active_turn_delta(bridge_dir, turn_id):
        edge = _delta_recovery_status_edge(bridge_dir, params, turn_id)
        if edge is not None:
            await _ensure_user_message_posted(client, session_id, params, forwarder_state)
            await _post_turn_status_edge(client, session_id, edge)
            _record_partial_text_delta(
                forwarder_state,
                turn_id=turn_id,
                item_type="plan",
                item_id=_item_id_from_delta_params(params),
                delta=delta,
            )
            await delta_coalescer.append(
                delta,
                message_id=_streaming_message_id(params, "plan"),
            )
            return
        _logger.info("Codex forwarder ignored stale plan delta: turn_id=%s", turn_id)
        return
    _record_partial_text_delta(
        forwarder_state,
        turn_id=turn_id,
        item_type="plan",
        item_id=_item_id_from_delta_params(params),
        delta=delta,
    )
    await delta_coalescer.append(
        delta,
        message_id=_streaming_message_id(params, "plan"),
    )


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _collab as _sib_collab
    from . import _deltas as _sib_deltas
    from . import _elicitation as _sib_elicitation
    from . import _fwd_state as _sib_fwd_state
    from . import _helpers as _sib_helpers
    from . import _posting as _sib_posting
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
    for _key, _value in _sib_turn.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
