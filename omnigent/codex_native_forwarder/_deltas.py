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

class _OutputTextDeltaCoalescer:
    """
    Coalesce high-frequency Codex text deltas before posting to AP.

    Codex can emit many tiny ``item/agentMessage/delta`` notifications.
    Posting each one through Omnigent as an awaited HTTP request makes the
    forwarder drain behind Codex. This worker keeps event ingestion
    cheap while preserving the order of flushed text relative to
    explicit flush barriers.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param flush_interval_seconds: Maximum time to hold the first
        buffered delta before posting it.
    :param flush_char_threshold: Maximum buffered character count before
        posting immediately.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        *,
        flush_interval_seconds: float = _DELTA_FLUSH_INTERVAL_SECONDS,
        flush_char_threshold: int = _DELTA_FLUSH_CHAR_THRESHOLD,
    ) -> None:
        """
        Initialize the coalescer.

        :param client: HTTP client for Omnigent event posts.
        :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
        :param flush_interval_seconds: Maximum buffering delay in
            seconds, e.g. ``0.05``.
        :param flush_char_threshold: Character threshold that triggers
            an immediate flush, e.g. ``64``.
        """
        self._client = client
        self._session_id = session_id
        self._flush_interval_seconds = flush_interval_seconds
        self._flush_char_threshold = flush_char_threshold
        self._queue: asyncio.Queue[_DeltaChunk | _DeltaFlushBarrier | _DeltaFlushStop] = (
            asyncio.Queue()
        )
        self._worker_task: asyncio.Task[None] | None = None
        self._next_index_by_message_id: dict[str, int] = {}

    async def append(self, delta: str, *, message_id: str | None = None) -> None:
        """
        Queue one assistant text delta for coalesced delivery.

        :param delta: Assistant text fragment, e.g. ``"hel"``.
        :param message_id: Optional stable native message stream id,
            e.g. ``"codex:thread_123:turn_123:agentMessage:item_agent"``.
        :returns: None.
        """
        if not delta:
            return
        self._ensure_worker()
        self._queue.put_nowait(_DeltaChunk(message_id=message_id, delta=delta))

    async def flush(self) -> None:
        """
        Flush all deltas queued before this call.

        :returns: None after all earlier deltas have been posted.
        """
        if self._worker_task is None:
            return
        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()
        self._queue.put_nowait(_DeltaFlushBarrier(done=done))
        await done

    async def close(self) -> None:
        """
        Flush pending deltas and stop the background worker.

        :returns: None after the worker has stopped.
        """
        if self._worker_task is None:
            return
        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()
        self._queue.put_nowait(_DeltaFlushStop(done=done))
        await done
        await self._worker_task
        self._worker_task = None

    def _ensure_worker(self) -> None:
        """
        Start the background worker if it is not already running.

        :returns: None.
        """
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(
                self._run(),
                name="codex-native-delta-coalescer",
            )

    async def _run(self) -> None:
        """
        Drain queued deltas and flush barriers in FIFO order.

        :returns: None after a stop marker is processed.
        """
        buffer: list[str] = []
        buffer_message_id: str | None = None
        buffered_chars = 0
        flush_deadline: float | None = None
        loop = asyncio.get_running_loop()
        while True:
            timeout = None
            if buffer and flush_deadline is not None:
                timeout = max(0.0, flush_deadline - loop.time())
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            except TimeoutError:
                await self._flush_buffer(buffer, message_id=buffer_message_id)
                buffer = []
                buffer_message_id = None
                buffered_chars = 0
                flush_deadline = None
                continue
            if isinstance(item, _DeltaChunk):
                if buffer and item.message_id != buffer_message_id:
                    await self._flush_buffer(buffer, message_id=buffer_message_id)
                    buffer = []
                    buffer_message_id = None
                    buffered_chars = 0
                    flush_deadline = None
                if not buffer:
                    flush_deadline = loop.time() + self._flush_interval_seconds
                    buffer_message_id = item.message_id
                buffer.append(item.delta)
                buffered_chars += len(item.delta)
                if "\n" in item.delta or buffered_chars >= self._flush_char_threshold:
                    await self._flush_buffer(buffer, message_id=buffer_message_id)
                    buffer = []
                    buffer_message_id = None
                    buffered_chars = 0
                    flush_deadline = None
                continue
            if isinstance(item, _DeltaFlushBarrier):
                await self._flush_buffer(buffer, message_id=buffer_message_id)
                buffer = []
                buffer_message_id = None
                buffered_chars = 0
                flush_deadline = None
                item.done.set_result(None)
                continue
            await self._flush_buffer(buffer, message_id=buffer_message_id)
            item.done.set_result(None)
            return

    async def _flush_buffer(self, buffer: list[str], *, message_id: str | None) -> None:
        """
        Post a non-empty coalesced delta buffer to AP.

        :param buffer: Buffered text fragments, e.g. ``["hel", "lo"]``.
        :param message_id: Stable native message stream id for the
            buffer, e.g. ``"codex:thread_123:turn_123:agentMessage:item"``.
        :returns: None.
        """
        if not buffer:
            return
        delta = "".join(buffer)
        index: int | None = None
        final: bool | None = None
        if message_id is not None:
            index = self._next_index_by_message_id.get(message_id, 0)
            self._next_index_by_message_id[message_id] = index + 1
            final = False
        try:
            await _post_output_text_delta(
                self._client,
                self._session_id,
                delta,
                message_id=message_id,
                index=index,
                final=final,
            )
        except Exception:  # noqa: BLE001 - preserve the long-lived forwarder.
            _logger.warning("Codex forwarder delta flush failed", exc_info=True)

async def _maybe_persist_interrupted_partial_text(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    method: str,
    params: dict[str, Any],
    forwarder_state: _CodexForwarderState | None,
) -> None:
    """
    Mirror an interrupted Codex turn and persist any buffered visible text.

    Normal Codex turns emit durable ``item/completed`` records, so their
    streamed deltas remain transient. Interrupted turns can end with only
    streamed deltas and a terminal ``turn/completed`` status of
    ``interrupted``. In that case, publish ``session.interrupted`` and
    persist the visible partial answer as a real assistant message before
    the session goes idle.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param method: Codex terminal method, e.g. ``"turn/completed"``.
    :param params: Codex terminal notification params.
    :param forwarder_state: Mutable forwarder state carrying partial text.
    :returns: None.
    """
    if method != "turn/completed":
        return
    if not _turn_status_is_interrupted(_turn_status_from_params(params)):
        return
    turn_id = _terminal_turn_id_from_params(params)
    response_id = _response_id(_params_with_turn_id(params, turn_id)) if turn_id else None
    await _post_session_interrupted(client, session_id, response_id=response_id)
    if forwarder_state is None:
        return
    if turn_id is None:
        return
    buffers = forwarder_state.consume_partial_text_for_turn(turn_id)
    buffers_to_persist = [
        buffer
        for buffer in buffers
        if _claim_partial_text_buffer(params, turn_id, buffer, forwarder_state)
    ]
    text = "".join(buffer.text() for buffer in buffers_to_persist)
    if not text:
        return
    scoped_params = _params_with_turn_id(params, turn_id)
    await _ensure_user_message_posted(client, session_id, scoped_params, forwarder_state)
    await _post_interrupted_partial_agent_message(client, session_id, scoped_params, text)

def _claim_partial_text_buffer(
    params: dict[str, Any],
    turn_id: str,
    buffer: _PartialTextBuffer,
    forwarder_state: _CodexForwarderState,
) -> bool:
    """
    Claim the completed-item dedup key for a persisted partial text buffer.

    :param params: Codex terminal notification params.
    :param turn_id: Codex turn id, e.g. ``"turn_123"``.
    :param buffer: Partial text buffer being persisted.
    :param forwarder_state: Mutable forwarder state with item dedup keys.
    :returns: ``True`` when the partial buffer should be persisted.
    """
    if buffer.item_id is None:
        return True
    thread_id = _thread_id_from_params(params) or "thread"
    return forwarder_state.claim_item_key(f"{thread_id}:{turn_id}:{buffer.item_id}")

def _is_active_turn_delta(bridge_dir: Path, turn_id: str | None) -> bool:
    """
    Return whether a Codex delta belongs to the current active turn.

    :param bridge_dir: Native Codex bridge directory.
    :param turn_id: Codex turn id from the delta notification, e.g.
        ``"turn_123"``.
    :returns: ``True`` when the bridge state identifies the same
        active turn.
    """
    if turn_id is None:
        return False
    state = read_bridge_state(bridge_dir)
    return state is not None and state.active_turn_id == turn_id

def _item_id_from_delta_params(params: dict[str, Any]) -> str | None:
    """
    Extract a Codex item id from a streaming delta notification.

    :param params: Codex delta params, e.g.
        ``{"itemId": "item_abc123"}``.
    :returns: Item id, or ``None`` when absent.
    """
    item_id = params.get("itemId")
    return item_id if isinstance(item_id, str) and item_id else None

def _streaming_message_id(params: dict[str, Any], item_type: str) -> str | None:
    """
    Build a stable Omnigent live-delta stream id for a Codex item.

    Omnigent Web uses this id to keep terminal-observed live text in a
    provisional native block, then replace that block when the durable
    completed item arrives. Returning ``None`` preserves the generic
    Responses-style text stream for malformed deltas that carry no
    usable Codex identity.

    :param params: Codex delta params, e.g.
        ``{"threadId": "thread_123", "turnId": "turn_123",
        "itemId": "item_agent"}``.
    :param item_type: Codex item type, e.g. ``"agentMessage"``.
    :returns: Stable message id, e.g.
        ``"codex:thread_123:turn_123:agentMessage:item_agent"``, or
        ``None``.
    """
    thread_id = _thread_id_from_params(params)
    turn_id = _turn_id_from_payload(params)
    item_id = _item_id_from_delta_params(params)
    if thread_id is None and turn_id is None and item_id is None:
        return None
    parts = ["codex"]
    if thread_id is not None:
        parts.append(thread_id)
    if turn_id is not None:
        parts.append(turn_id)
    parts.append(item_type)
    if item_id is not None:
        parts.append(item_id)
    return ":".join(parts)

def _record_partial_text_delta(
    forwarder_state: _CodexForwarderState | None,
    *,
    turn_id: str | None,
    item_type: str,
    item_id: str | None,
    delta: str,
) -> None:
    """
    Record a visible Codex text delta for interrupted-turn durability.

    :param forwarder_state: Mutable forwarder state, or ``None`` when direct
        tests bypass stateful supervision.
    :param turn_id: Codex turn id, e.g. ``"turn_123"``.
    :param item_type: Codex item type, e.g. ``"agentMessage"``.
    :param item_id: Codex item id, e.g. ``"item_abc123"``, or ``None``.
    :param delta: Text fragment, e.g. ``"hel"``.
    :returns: None.
    """
    if forwarder_state is None or turn_id is None:
        return
    forwarder_state.record_partial_text_delta(
        turn_id=turn_id,
        item_type=item_type,
        item_id=item_id,
        delta=delta,
    )

def _try_recover_active_turn_from_delta(
    bridge_dir: Path,
    params: dict[str, Any],
    turn_id: str | None,
) -> bool:
    """
    Adopt a Codex delta turn when subscription missed ``turn/started``.

    Fresh remote Codex sessions can begin a TUI turn while the observer
    connection is still retrying ``thread/resume``. In that race the
    first plan delta is already scoped by ``threadId``/``turnId`` but
    bridge state has no active turn yet. Treat that as the current turn
    only when the thread matches the bridge state; an already-active
    different turn remains protected from stale deltas.

    :param bridge_dir: Native Codex bridge directory.
    :param params: Codex delta notification params.
    :param turn_id: Turn id extracted from *params*.
    :returns: ``True`` when the delta was adopted as the active turn.
    """
    if turn_id is None:
        return False
    state = read_bridge_state(bridge_dir)
    if state is None or state.active_turn_id is not None:
        return False
    thread_id = params.get("threadId")
    if thread_id != state.thread_id:
        return False
    update_active_turn_id(bridge_dir, turn_id)
    return True


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _collab as _sib_collab
    from . import _elicitation as _sib_elicitation
    from . import _events as _sib_events
    from . import _fwd_state as _sib_fwd_state
    from . import _helpers as _sib_helpers
    from . import _posting as _sib_posting
    from . import _resume as _sib_resume
    from . import _supervisor as _sib_supervisor
    from . import _turn as _sib_turn
    for _key, _value in _sib_collab.__dict__.items():
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
