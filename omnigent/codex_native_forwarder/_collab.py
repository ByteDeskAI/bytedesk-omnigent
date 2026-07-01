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

def _default_collaboration_mode(
    forwarder_state: _CodexForwarderState,
) -> dict[str, Any] | None:
    """
    Build Codex's Default collaboration mode for ``turn/start``.

    ``developer_instructions: null`` deliberately asks Codex
    app-server to fill in the built-in Default-mode instructions via
    its own normalization path.

    :param forwarder_state: Mutable state with the current model.
    :returns: Codex ``CollaborationMode`` JSON object, or ``None``.
    """
    if not forwarder_state.model:
        return None
    return {
        "mode": "default",
        "settings": {
            "model": forwarder_state.model,
            "reasoning_effort": None,
            "developer_instructions": None,
        },
    }

async def _ensure_child_session(
    client: httpx.AsyncClient,
    *,
    parent_session_id: str,
    parent_thread_id: str | None,
    child_thread_id: str,
    item: dict[str, Any],
    forwarder_state: _CodexForwarderState,
) -> None:
    """
    Ensure a Codex child thread has an Omnigent child session row.

    Registers the child via ``_register_child_session`` when unknown,
    then backfills its history at most once per connection.

    :param client: HTTP client for Omnigent event posts.
    :param parent_session_id: Parent Omnigent session id, e.g. ``"conv_parent"``.
    :param parent_thread_id: Parent Codex thread id, or ``None``.
    :param child_thread_id: Codex child thread id, e.g. ``"thread_child"``.
    :param item: Codex ``collabAgentToolCall`` item with spawn metadata.
    :param forwarder_state: Mutable state for child-thread mappings.
    :returns: None.
    """
    child_session_id = forwarder_state.session_for_child_thread(child_thread_id)
    if child_session_id is None:
        child_session_id = await _register_child_session(
            client,
            parent_session_id=parent_session_id,
            parent_thread_id=parent_thread_id,
            child_thread_id=child_thread_id,
            item=item,
        )
        if child_session_id is None:
            return
        forwarder_state.note_child_thread(child_thread_id, child_session_id)
    # Backfill is done via the codex_client stored on the state.
    codex_client = forwarder_state.codex_client
    if codex_client is not None and forwarder_state.needs_child_thread_backfill(child_thread_id):
        await _backfill_child_thread(
            client,
            codex_client,
            parent_session_id=parent_session_id,
            child_session_id=child_session_id,
            child_thread_id=child_thread_id,
            forwarder_state=forwarder_state,
        )

async def _register_child_session(
    client: httpx.AsyncClient,
    *,
    parent_session_id: str,
    parent_thread_id: str | None,
    child_thread_id: str,
    item: dict[str, Any],
) -> str | None:
    """
    POST ``external_codex_subagent_start`` and return the child session id.

    :param client: HTTP client for Omnigent event posts.
    :param parent_session_id: Parent Omnigent session id, e.g. ``"conv_parent"``.
    :param parent_thread_id: Parent Codex thread id, or ``None``.
    :param child_thread_id: Codex child thread id, e.g. ``"thread_child"``.
    :param item: Codex ``collabAgentToolCall`` item.
    :returns: Omnigent child session id, or ``None`` on failure.
    """
    data: dict[str, Any] = {"thread_id": child_thread_id}
    if parent_thread_id is not None:
        data["parent_thread_id"] = parent_thread_id
    tool_call_id = item.get("id")
    if isinstance(tool_call_id, str) and tool_call_id:
        data["tool_call_id"] = tool_call_id
    response = await _post_session_event(
        client,
        parent_session_id,
        event_type=_EXTERNAL_CODEX_SUBAGENT_START_TYPE,
        data=data,
    )
    if response is None or response.status_code >= 400:
        _log_failed_session_event_post(_EXTERNAL_CODEX_SUBAGENT_START_TYPE, response)
        return None
    return _extract_child_session_id(response, child_thread_id)

def _extract_child_session_id(
    response: httpx.Response,
    child_thread_id: str,
) -> str | None:
    """
    Extract the child session id from an ``external_codex_subagent_start`` response.

    :param response: Omnigent HTTP response.
    :param child_thread_id: Codex child thread id for error logging.
    :returns: Omnigent child session id, or ``None`` when absent or malformed.
    """
    child_session_id = response.json().get("child_session_id")
    if not isinstance(child_session_id, str) or not child_session_id:
        _logger.warning(
            "Codex sub-agent registration missing child_session_id: thread_id=%s",
            child_thread_id,
        )
        return None
    return child_session_id

async def _backfill_child_thread(
    client: httpx.AsyncClient,
    codex_client: CodexAppServerClient,
    *,
    parent_session_id: str,
    child_session_id: str,
    child_thread_id: str,
    forwarder_state: _CodexForwarderState,
) -> None:
    """
    Replay a child thread's backlog and upsert its name metadata.

    Called at most once per connection per child (guarded by
    ``subscribed_child_threads``). Fetches the child's rollout via
    ``thread/resume``, upserts the nickname/role labels, and replays
    any already-completed items. Live items arriving after discovery
    flow through the normal routing path; the dedup key prevents
    overlap.

    :param client: HTTP client for Omnigent event posts.
    :param codex_client: Connected Codex app-server client.
    :param parent_session_id: Parent Omnigent session id, e.g.
        ``"conv_parent"``.
    :param child_session_id: Omnigent child session id, e.g.
        ``"conv_child"``.
    :param child_thread_id: Codex child thread id, e.g.
        ``"thread_child"``.
    :param forwarder_state: Mutable state for sub-agent mappings.
    :returns: None.
    """
    response = await _resume_child_thread_or_log(
        client, codex_client, child_session_id=child_session_id, child_thread_id=child_thread_id
    )
    if response is None:
        return
    await _apply_child_resume(
        client,
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        child_thread_id=child_thread_id,
        response=response,
        forwarder_state=forwarder_state,
    )

async def _resume_child_thread_or_log(
    client: httpx.AsyncClient,
    codex_client: CodexAppServerClient,
    *,
    child_session_id: str,
    child_thread_id: str,
) -> CodexMessage | None:
    """
    Request ``thread/resume`` for a child thread, logging errors.

    :param client: HTTP client for Omnigent status posts on failure.
    :param codex_client: Connected Codex app-server client.
    :param child_session_id: Omnigent child session id, e.g. ``"conv_child"``.
    :param child_thread_id: Codex child thread id, e.g.
        ``"thread_child"``.
    :returns: JSON-RPC response on success, or ``None`` on error.
    """
    try:
        return await codex_client.request("thread/resume", {"threadId": child_thread_id})
    except RuntimeError as exc:
        if _is_thread_not_ready_error(exc):
            _logger.info("Codex child thread %s not ready yet; skipping backfill", child_thread_id)
        else:
            _logger.warning(
                "Codex forwarder failed to backfill child thread %s",
                child_thread_id,
                exc_info=True,
            )
            await _post_status(client, child_session_id, "failed")
        return None

def _codex_child_name_data(
    child_thread_id: str,
    thread: dict[str, Any],
) -> dict[str, Any]:
    """
    Build the name-metadata payload for a Codex child upsert.

    :param child_thread_id: Codex child thread id, e.g. ``"thread_child"``.
    :param thread: Codex thread object from a ``thread/resume`` response.
    :returns: Data dict with at least ``thread_id``; name fields added when
        present on the thread object.
    """
    data: dict[str, Any] = {"thread_id": child_thread_id}
    agent_nickname = thread.get("agentNickname")
    if isinstance(agent_nickname, str) and agent_nickname:
        data["agent_nickname"] = agent_nickname
    agent_role = thread.get("agentRole")
    if isinstance(agent_role, str) and agent_role:
        data["agent_role"] = agent_role
    source = _thread_spawn_source(thread)
    if source is not None:
        parent_thread_id = source.get("parent_thread_id")
        if isinstance(parent_thread_id, str) and parent_thread_id:
            data["parent_thread_id"] = parent_thread_id
        prompt = thread.get("preview") or source.get("prompt")
        if isinstance(prompt, str) and prompt:
            data["prompt"] = prompt
    return data

def _thread_spawn_source(thread: dict[str, Any]) -> dict[str, Any] | None:
    """
    Return the ``thread_spawn`` source metadata from a Codex thread object.

    :param thread: Codex thread object from a ``thread/started`` or
        ``thread/resume`` payload.
    :returns: The ``thread_spawn`` dict when present, otherwise ``None``.
    """
    source = thread.get("source")
    if not isinstance(source, dict):
        return None
    subagent = source.get("subAgent")
    if not isinstance(subagent, dict):
        return None
    thread_spawn = subagent.get("thread_spawn")
    return thread_spawn if isinstance(thread_spawn, dict) else None

async def _post_collab_agent_statuses(
    client: httpx.AsyncClient,
    *,
    item: dict[str, Any],
    forwarder_state: _CodexForwarderState,
) -> None:
    """
    Publish Omnigent status updates from a Codex collab-agent state snapshot.

    :param client: HTTP client for Omnigent event posts.
    :param item: Codex ``collabAgentToolCall`` item carrying
        ``agentsStates``.
    :param forwarder_state: Mutable state for child-thread mappings.
    :returns: None.
    """
    states = item.get("agentsStates")
    if not isinstance(states, dict):
        return
    for thread_id, state in states.items():
        if not isinstance(thread_id, str) or not isinstance(state, dict):
            continue
        child_session_id = forwarder_state.session_for_child_thread(thread_id)
        if child_session_id is None:
            continue
        ap_status = _omnigent_status_from_collab_state(state)
        if ap_status is not None:
            await _post_status(client, child_session_id, ap_status)

def _omnigent_status_from_collab_state(state: dict[str, Any]) -> str | None:
    """
    Convert a Codex collab-agent state dict to an Omnigent session status.

    :param state: Codex ``CollabAgentState`` dict, e.g.
        ``{"status": "running"}``.
    :returns: Omnigent status literal, e.g. ``"running"``, or ``None`` when
        the Codex status is unrecognized.
    """
    status = state.get("status")
    if status in _CODEX_COLLAB_RUNNING_STATUSES:
        return "running"
    if status in _CODEX_COLLAB_FAILED_STATUSES:
        return "failed"
    if status in {"completed", "interrupted", "shutdown"}:
        return "idle"
    return None

def _collab_receiver_thread_ids(item: dict[str, Any]) -> list[str]:
    """
    Extract receiver thread ids from a Codex collab-agent item.

    :param item: Codex ``collabAgentToolCall`` item.
    :returns: Deduplicated receiver thread ids in original order.
    """
    raw = item.get("receiverThreadIds")
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for value in raw:
        if isinstance(value, str) and value and value not in seen:
            result.append(value)
            seen.add(value)
    return result

def _collab_parent_thread_id(
    params: dict[str, Any],
    item: dict[str, Any],
) -> str | None:
    """
    Return the Codex parent thread id for a collab-agent spawn.

    :param params: Codex notification params.
    :param item: Codex ``collabAgentToolCall`` item.
    :returns: Parent thread id, or ``None`` when not determinable.
    """
    sender = item.get("senderThreadId")
    if isinstance(sender, str) and sender:
        return sender
    return _thread_id_from_params(params)


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _deltas as _sib_deltas
    from . import _elicitation as _sib_elicitation
    from . import _events as _sib_events
    from . import _fwd_state as _sib_fwd_state
    from . import _helpers as _sib_helpers
    from . import _posting as _sib_posting
    from . import _resume as _sib_resume
    from . import _supervisor as _sib_supervisor
    from . import _turn as _sib_turn
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
    for _key, _value in _sib_turn.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
