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

async def _replay_resume_response(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    response: CodexMessage,
    usage_coalescer: _SessionUsageCoalescer,
    elicitation_tracker: _CodexElicitationTaskTracker,
    forwarder_state: _CodexForwarderState | None = None,
) -> None:
    """
    Mirror message items returned by ``thread/resume``.

    Passes ``forwarder_state`` into each replayed event so the dedup gate
    in ``_handle_completed_item`` can skip items that the live stream
    already delivered.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id.
    :param bridge_dir: Native Codex bridge directory.
    :param response: Codex ``thread/resume`` response envelope.
    :param usage_coalescer: Token-usage coalescer for replayed
        app-server events.
    :param elicitation_tracker: Background Codex elicitation tracker.
    :param forwarder_state: Optional mutable state for dedup and
        sub-agent registration.
    :returns: None.
    """
    result = response.get("result")
    if not isinstance(result, dict):
        return
    thread = result.get("thread")
    if not isinstance(thread, dict):
        return
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return
    thread_id = thread.get("id")
    thread_id = thread_id if isinstance(thread_id, str) and thread_id else None
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        turn_id = _turn_id_from_payload(turn)
        items = turn.get("items")
        if not turn_id or not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            await _handle_event(
                client,
                session_id=session_id,
                bridge_dir=bridge_dir,
                event={
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "item": item,
                    },
                },
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
                expected_thread_id=thread_id,
                forwarder_state=forwarder_state,
            )
    await _post_resume_terminal_status(
        client,
        session_id=session_id,
        bridge_dir=bridge_dir,
        thread_id=thread_id,
        turns=turns,
    )

def _resume_terminal_status_edge_for_latest_turn(
    bridge_dir: Path,
    thread_id: str,
    turns: list[Any],
) -> _CodexTurnStatusEdge | None:
    """
    Return the Omnigent terminal status represented by the latest resume turn.

    :param bridge_dir: Native Codex bridge directory.
    :param thread_id: Codex thread id from the resume payload, e.g.
        ``"thread_123"``.
    :param turns: Raw Codex resume turn list.
    :returns: Terminal status edge when the latest turn is terminal and
        belongs to the bridge's current thread; otherwise ``None``.
    """
    state = read_bridge_state(bridge_dir)
    if state is None or state.thread_id != thread_id:
        return None
    for turn in reversed(turns):
        if not isinstance(turn, dict):
            continue
        turn_id = _turn_id_from_payload(turn)
        if turn_id is None:
            return None
        if state.active_turn_id is not None and state.active_turn_id != turn_id:
            return None
        status = _omnigent_status_from_resume_turn(turn)
        if status is None:
            return None
        update_active_turn_id(bridge_dir, None)
        return _CodexTurnStatusEdge(
            status=status,
            turn_id=turn_id,
            source="thread/resume",
        )
    return None

def _omnigent_status_from_resume_turn(turn: dict[str, Any]) -> str | None:
    """
    Convert an explicit Codex resume turn status to Omnigent session status.

    :param turn: Codex resume turn object, e.g.
        ``{"id": "turn_123", "status": "completed"}``.
    :returns: Omnigent status literal for terminal turns, or ``None`` for active
        or unrecognized statuses.
    """
    status = turn.get("status")
    if isinstance(status, dict):
        status = status.get("type") or status.get("status")
    if status in {"completed", "interrupted", "cancelled", "canceled"}:
        return "idle"
    if status in {"failed", "errored"}:
        return "failed"
    return None

def _refresh_model_from_config(bridge_dir: Path, forwarder_state: _CodexForwarderState) -> None:
    """
    Update the forwarder's known model from this session's ``config.toml``.

    Reads the source-of-truth model via the shared
    :func:`~omnigent.codex_native_bridge.read_codex_config_model` (the
    ``model`` key an in-TUI ``/model`` writes — see that function for why
    config.toml is the source of truth and its caveats) and stores it on
    ``forwarder_state.model`` so a following ``_sync_model_change`` mirrors
    it to Omnigent as ``model_override``. This mirror is a fallback to the codex
    hook, which stamps the live model onto the evaluation request at gate
    time; the gate prefers the hook's value. No-op when the model can't be
    determined, leaving the prior value.

    :param bridge_dir: The session's native-Codex bridge directory.
    :param forwarder_state: Mutable forwarder state whose ``model`` is
        updated in place.
    :returns: None.
    """
    model = read_codex_config_model(bridge_dir)
    if model:
        forwarder_state.model = model

async def _sync_model_change(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    forwarder_state: _CodexForwarderState,
) -> None:
    """
    Mirror a Codex TUI ``/model`` switch to Omnigent (web picker + cost gate).

    The active model is recorded on ``forwarder_state.model`` by
    ``_refresh_model_from_config`` (read from ``config.toml``, the source of
    truth for codex — see ``read_codex_config_model``) at subscription and at
    each ``turn/started``, and also by ``thread/settings/updated`` when Codex
    emits one. When that differs from the last-mirrored ``posted_model``
    baseline, POST an
    ``external_model_change`` event so the Omnigent server persists
    ``conv.model_override`` — which keeps the web model dropdown in sync and
    lets the cost-budget policy re-evaluate against the new model. Codex
    model ids are stable per model (unlike Claude's per-turn concrete id),
    so the raw id is posted as-is. Best-effort: a failed post leaves the
    baseline unchanged so the next settings update retries.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param forwarder_state: Mutable forwarder state carrying the current
        model and the last-mirrored baseline.
    :returns: None.
    """
    model = forwarder_state.model
    if not model or model == forwarder_state.posted_model:
        return
    response = await _post_session_event(
        client,
        session_id,
        event_type="external_model_change",
        data={"model": model},
    )
    _log_failed_session_event_post("external_model_change", response)
    if response is not None and response.status_code < 400:
        forwarder_state.posted_model = model

async def _apply_child_resume(
    client: httpx.AsyncClient,
    *,
    parent_session_id: str,
    child_session_id: str,
    child_thread_id: str,
    response: CodexMessage,
    forwarder_state: _CodexForwarderState,
) -> None:
    """
    Upsert child name labels and replay its backlogged transcript.

    :param client: HTTP client for Omnigent event posts.
    :param parent_session_id: Parent Omnigent session id, e.g. ``"conv_parent"``.
    :param child_session_id: Omnigent child session id, e.g. ``"conv_child"``.
    :param child_thread_id: Codex child thread id, e.g. ``"thread_child"``.
    :param response: Validated ``thread/resume`` response envelope.
    :param forwarder_state: Mutable state for sub-agent mappings.
    :returns: None.
    """
    await _upsert_child_name_from_resume(
        client,
        parent_session_id=parent_session_id,
        child_thread_id=child_thread_id,
        response=response,
    )
    # Seed the session model (sub-agents inherit it) so replayed child token
    # usage is priced into the child's total_cost_usd — see _SessionUsageCoalescer.
    usage_coalescer = _SessionUsageCoalescer(client, child_session_id, model=forwarder_state.model)
    # A fresh tracker is used for child replay rather than the parent's,
    # because child items do not trigger elicitation requests on the parent.
    child_elicitation_tracker = _CodexElicitationTaskTracker()
    try:
        await _replay_resume_response(
            client,
            session_id=child_session_id,
            bridge_dir=Path(),
            response=response,
            usage_coalescer=usage_coalescer,
            elicitation_tracker=child_elicitation_tracker,
            forwarder_state=forwarder_state,
        )
    finally:
        await child_elicitation_tracker.close()
    forwarder_state.note_child_thread_subscribed(child_thread_id)

async def _upsert_child_name_from_resume(
    client: httpx.AsyncClient,
    *,
    parent_session_id: str,
    child_thread_id: str,
    response: CodexMessage,
) -> None:
    """
    Upsert ``agent_nickname`` / ``agent_role`` from a child resume response.

    Idempotent — the server merges labels. No-ops when the resume carries
    no name fields beyond the thread id.

    :param client: HTTP client for Omnigent event posts.
    :param parent_session_id: Parent Omnigent session id, e.g. ``"conv_parent"``.
    :param child_thread_id: Codex child thread id, e.g. ``"thread_child"``.
    :param response: Codex ``thread/resume`` response envelope.
    :returns: None.
    """
    result = response.get("result")
    if not isinstance(result, dict):
        return
    thread = result.get("thread")
    if not isinstance(thread, dict):
        return
    data = _codex_child_name_data(child_thread_id, thread)
    if len(data) <= 1:
        return
    response_obj = await _post_session_event(
        client, parent_session_id, event_type=_EXTERNAL_CODEX_SUBAGENT_START_TYPE, data=data
    )
    _log_failed_session_event_post(_EXTERNAL_CODEX_SUBAGENT_START_TYPE, response_obj)

def _find_turn_user_message(response: CodexMessage, turn_id: str) -> dict[str, Any] | None:
    """
    Locate a turn's ``userMessage`` item in a ``thread/resume`` response.

    :param response: Codex ``thread/resume`` response envelope.
    :param turn_id: Codex turn id whose user message to find, e.g.
        ``"turn_123"``.
    :returns: The ``userMessage`` item dict, or ``None`` when the turn or
        its user message is absent.
    """
    result = response.get("result")
    if not isinstance(result, dict):
        return None
    thread = result.get("thread")
    if not isinstance(thread, dict):
        return None
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return None
    for turn in turns:
        if not isinstance(turn, dict) or _turn_id_from_payload(turn) != turn_id:
            continue
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("type") == "userMessage":
                return item
    return None

def _thread_id_from_started_event(event: CodexMessage) -> str | None:
    """
    Extract a thread id from a Codex ``thread/started`` event.

    :param event: Codex app-server notification envelope.
    :returns: Thread id, e.g. ``"thread_abc"``, or ``None``.
    """
    if event.get("method") != "thread/started":
        return None
    params = event.get("params")
    if not isinstance(params, dict):
        return None
    thread = params.get("thread")
    if not isinstance(thread, dict):
        return None
    thread_id = thread.get("id")
    return thread_id if isinstance(thread_id, str) and thread_id else None

def _parent_thread_id_from_started_event(event: CodexMessage) -> str | None:
    """
    Extract the spawning parent thread id from a child ``thread/started``.

    :param event: Codex app-server notification envelope.
    :returns: Parent Codex thread id from
        ``source.subAgent.thread_spawn.parent_thread_id``, e.g.
        ``"thread_parent"``, or ``None`` when absent.
    """
    if event.get("method") != "thread/started":
        return None
    params = event.get("params")
    if not isinstance(params, dict):
        return None
    thread = params.get("thread")
    if not isinstance(thread, dict):
        return None
    source = _thread_spawn_source(thread)
    if source is None:
        return None
    parent_thread_id = source.get("parent_thread_id")
    return parent_thread_id if isinstance(parent_thread_id, str) and parent_thread_id else None

def _thread_started_is_subagent(event: CodexMessage) -> bool:
    """
    Return whether a ``thread/started`` event announces a child sub-agent.

    Codex AgentControl children emit ``thread/started`` when they begin.
    These events carry a ``source.subAgent.thread_spawn`` object that
    distinguishes them from a top-level session rotation triggered by
    the user running ``/clear``.

    :param event: Codex app-server notification envelope.
    :returns: ``True`` when the started thread declares itself a
        sub-agent via ``source.subAgent.thread_spawn``.
    """
    if event.get("method") != "thread/started":
        return False
    params = event.get("params")
    if not isinstance(params, dict):
        return False
    thread = params.get("thread")
    if not isinstance(thread, dict):
        return False
    return _thread_spawn_source(thread) is not None

async def wait_for_thread_started(
    client: CodexAppServerClient,
    *,
    timeout: float = _THREAD_START_TIMEOUT_SECONDS,
) -> str:
    """
    Wait for a freshly launched Codex TUI to create its app-server thread.

    A cold-start Codex TUI (launched with ``--remote`` and no ``resume``)
    creates a new thread, and the app-server emits a ``thread/started``
    notification to connected listeners. *client* must already be connected
    so it observes that notification. The returned id is then used to
    subscribe the forwarder and to drive web-UI message injection, so the
    terminal and chat share one thread. The host-spawned runner auto-create
    uses this because — unlike the local CLI — it has no TTY to ``resume`` an
    existing thread into, and ``resume`` of a not-yet-persisted thread fails.

    :param client: A connected :class:`CodexAppServerClient` listening for
        app-server notifications.
    :param timeout: Seconds to wait for ``thread/started`` before failing.
    :returns: The Codex thread id, e.g.
        ``"019e8720-98d7-7b23-ac0a-bfb0eb02e0c9"``.
    :raises TimeoutError: If no ``thread/started`` arrives within *timeout*.
    :raises RuntimeError: If the event stream ends before a thread starts.
    """
    async with asyncio.timeout(timeout):
        async for event in client.iter_events():
            thread_id = _thread_id_from_started_event(event)
            if thread_id is not None:
                return thread_id
    raise RuntimeError("Codex app-server event stream ended before thread startup.")

def _thread_id_from_params(params: dict[str, Any]) -> str | None:
    """
    Extract the thread id carried by a Codex notification params object.

    :param params: Codex notification params, e.g.
        ``{"threadId": "thread_abc"}``.
    :returns: Thread id, or ``None`` when the event does not carry one.
    """
    thread_id = params.get("threadId")
    if isinstance(thread_id, str) and thread_id:
        return thread_id
    thread = params.get("thread")
    if isinstance(thread, dict):
        nested_thread_id = thread.get("id")
        if isinstance(nested_thread_id, str) and nested_thread_id:
            return nested_thread_id
    return None


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _collab as _sib_collab
    from . import _deltas as _sib_deltas
    from . import _elicitation as _sib_elicitation
    from . import _events as _sib_events
    from . import _fwd_state as _sib_fwd_state
    from . import _helpers as _sib_helpers
    from . import _posting as _sib_posting
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
    for _key, _value in _sib_posting.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_supervisor.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_turn.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
