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

@dataclass
class _ForwarderTarget:
    """
    Mutable AP/Codex target currently owned by the forwarder.

    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param thread_id: Codex app-server thread id, e.g.
        ``"0196..."``.
    :param delta_coalescer: Text-delta coalescer posting to
        ``session_id``.
    :param usage_coalescer: Token-usage coalescer posting to
        ``session_id``.
    :param elicitation_tracker: Background Codex elicitation hook
        tracker posting to ``session_id``.
    """

    session_id: str
    thread_id: str
    delta_coalescer: _OutputTextDeltaCoalescer
    usage_coalescer: _SessionUsageCoalescer
    elicitation_tracker: _CodexElicitationTaskTracker

@dataclass(frozen=True)
class _CodexToolCall:
    """
    Normalized view of one completed Codex built-in tool call.

    :param call_id: Codex item id reused as the Omnigent call id, e.g.
        ``"call_abc"``.
    :param name: Omnigent function-call name, e.g. ``"shell"``.
    :param arguments: Tool arguments dict, e.g. ``{"command": "pwd"}``.
    :param output: Tool result text rendered as the
        ``function_call_output``, e.g. ``"/repo\n"``.
    """

    call_id: str
    name: str
    arguments: dict[str, Any]
    output: str

@dataclass
class _PartialTextBuffer:
    """
    In-memory visible text collected from one streaming Codex item.

    :param item_type: Codex item type, e.g. ``"agentMessage"``.
    :param item_id: Codex item id, e.g. ``"item_abc123"``, or ``None``
        when a delta omitted it.
    :param parts: Ordered text fragments emitted for this item.
    """

    item_type: str
    item_id: str | None
    parts: list[str] = field(default_factory=list)

    def append(self, delta: str) -> None:
        """
        Append one text fragment to the item buffer.

        :param delta: Text fragment, e.g. ``"hel"``.
        :returns: None.
        """
        self.parts.append(delta)

    def text(self) -> str:
        """
        Return the concatenated item text.

        :returns: Joined text fragments.
        """
        return "".join(self.parts)

@dataclass
class _CodexForwarderState:
    """
    Mutable state for one long-lived Codex forwarder connection.

    :param model: Latest known Codex model for this thread, e.g.
        ``"gpt-5.2-codex"``.
    :param posted_model: Last model already mirrored to Omnigent via an
        ``external_model_change`` post (the dedupe baseline). Seeded from
        the resume/startup model so the spawn default is not echoed back as
        a change; only a later in-TUI ``/model`` switch is mirrored. ``None``
        until seeded.
    :param parent_session_id: Omnigent parent session id, e.g.
        ``"conv_parent"``. Set by ``supervise_forwarder`` so collab-agent
        helpers can register child sessions without extra parameter
        threading.
    :param codex_client: Connected Codex app-server client. Set by
        ``supervise_forwarder`` so child backfill can issue
        ``thread/resume`` requests.
    :param subagents_by_thread: Maps Codex child thread ids to Omnigent child
        session ids, e.g. ``{"thread_child": "conv_child"}``.
    :param pending_child_threads: Codex child thread ids announced by
        ``thread/started`` but not yet mapped to AP child sessions,
        mapped to their spawning parent thread id when known, e.g.
        ``{"thread_child": "thread_parent"}``.
    :param subscribed_child_threads: Codex child thread ids whose backlog
        has been replayed for this connection (guards against re-replay
        if the same collab item is observed multiple times).
    :param synced_item_keys: Stable item keys already posted to Omnigent this
        connection, e.g. ``{"thread_c:turn_c:item-1"}``. In-memory only;
        guards replay-vs-live overlap within one forwarder lifetime.
    :param posted_user_turns: Turn ids whose ``userMessage`` has been
        posted to Omnigent this connection, e.g. ``{"turn_123"}``. Used to
        enforce user-before-assistant ordering: before posting a turn's
        assistant reply, the forwarder recovers and posts the turn's user
        message if the live stream missed it (see
        :func:`_ensure_user_message_posted`).
    :param partial_text_by_turn: Visible assistant/plan text fragments keyed
        by turn id, e.g. ``{"turn_123": [_PartialTextBuffer(...)]}``.
        Normal completed items remain the durable source of truth; this
        buffer is only consumed when Codex reports an interrupted turn with no
        completed item for the streamed text.
    :param _anon_item_counters: Per-(thread, turn) counters used to
        assign deterministic positional keys to items that lack a stable
        ``id`` field.
    :param completed_plan_text_by_turn: Completed proposed-plan text
        keyed by turn id.
    :param plan_thread_by_turn: Codex thread id keyed by plan turn id.
    :param prompted_plan_turns: Turn ids that already exposed the
        implementation prompt, either natively or through the Omnigent bridge.
    """

    model: str | None = None
    posted_model: str | None = None
    parent_session_id: str | None = None
    codex_client: CodexAppServerClient | None = None
    subagents_by_thread: dict[str, str] = field(default_factory=dict)
    pending_child_threads: dict[str, str | None] = field(default_factory=dict)
    subscribed_child_threads: set[str] = field(default_factory=set)
    synced_item_keys: set[str] = field(default_factory=set)
    posted_user_turns: set[str] = field(default_factory=set)
    partial_text_by_turn: dict[str, list[_PartialTextBuffer]] = field(default_factory=dict)
    _anon_item_counters: dict[tuple[str, str], int] = field(default_factory=dict)
    completed_plan_text_by_turn: dict[str, str] = field(default_factory=dict)
    plan_thread_by_turn: dict[str, str] = field(default_factory=dict)
    prompted_plan_turns: set[str] = field(default_factory=set)

    def note_resume_response(self, response: CodexMessage) -> None:
        """
        Record thread settings returned by ``thread/resume``.

        :param response: Codex JSON-RPC response envelope.
        :returns: None.
        """
        result = response.get("result")
        if not isinstance(result, dict):
            return
        self._note_model_fields(result)
        # Do NOT seed ``posted_model`` here. Omnigent must learn the session's
        # ACTUAL model — including the spawn default — because the cost-budget
        # gate resolves the model as ``conv.model_override or spec.llm.model``,
        # and for codex the spawn model (read from ``config.toml`` / the
        # ``--model`` flag) is frequently NOT ``spec.llm.model``. If we seeded
        # the baseline to the spawn model, an unchanged session would never
        # post ``external_model_change``, ``model_override`` would stay
        # ``None``, and the gate would mis-resolve a cheap session as the
        # (possibly expensive/absent) spec model and wrongly DENY it. Leaving
        # ``posted_model`` ``None`` makes the first ``_sync_model_change``
        # mirror the real model; the dedupe still suppresses re-posts after.

    def note_thread_settings_updated(self, params: dict[str, Any]) -> None:
        """
        Record thread settings from a ``thread/settings/updated`` notification.

        :param params: Codex notification params.
        :returns: None.
        """
        settings = params.get("threadSettings")
        if isinstance(settings, dict):
            self._note_model_fields(settings)

    def record_completed_plan(self, params: dict[str, Any]) -> None:
        """
        Remember a completed Codex proposed-plan item for its terminal prompt.

        :param params: Codex ``item/completed`` params.
        :returns: None.
        """
        item = params.get("item")
        if not isinstance(item, dict) or item.get("type") != "plan":
            return
        turn_id = _turn_id_from_payload(params)
        thread_id = params.get("threadId")
        text = item.get("text")
        if not (
            isinstance(turn_id, str)
            and turn_id
            and isinstance(thread_id, str)
            and thread_id
            and isinstance(text, str)
            and text.strip()
        ):
            return
        self.completed_plan_text_by_turn[turn_id] = text
        self.plan_thread_by_turn[turn_id] = thread_id

    def mark_prompted(self, turn_id: str) -> None:
        """
        Mark a plan turn as having exposed its implementation prompt.

        :param turn_id: Codex turn id, e.g. ``"turn_123"``.
        :returns: None.
        """
        self.prompted_plan_turns.add(turn_id)

    def plan_prompt_context(self, turn_id: str) -> tuple[str, str] | None:
        """
        Return plan text and thread id for a not-yet-prompted turn.

        :param turn_id: Codex turn id, e.g. ``"turn_123"``.
        :returns: ``(thread_id, plan_text)`` or ``None``.
        """
        if turn_id in self.prompted_plan_turns:
            return None
        plan_text = self.completed_plan_text_by_turn.get(turn_id)
        thread_id = self.plan_thread_by_turn.get(turn_id)
        if not plan_text or not thread_id:
            return None
        return thread_id, plan_text

    def session_for_child_thread(self, thread_id: str) -> str | None:
        """
        Return the Omnigent child session id for a known Codex child thread.

        :param thread_id: Codex child thread id, e.g. ``"thread_child"``.
        :returns: Omnigent child session id, e.g. ``"conv_child"``, or ``None``
            when the thread is unknown.
        """
        return self.subagents_by_thread.get(thread_id)

    def note_child_thread(self, thread_id: str, session_id: str) -> None:
        """
        Record the Omnigent child session id for a Codex child thread.

        :param thread_id: Codex child thread id, e.g. ``"thread_child"``.
        :param session_id: Omnigent child session id, e.g. ``"conv_child"``.
        :returns: None.
        """
        self.subagents_by_thread[thread_id] = session_id
        self.pending_child_threads.pop(thread_id, None)

    def note_parent_rotation(self, session_id: str) -> None:
        """
        Record that the forwarder moved to a new parent AP session.

        :param session_id: New parent AP session id, e.g.
            ``"conv_new_parent"``.
        :returns: None.
        """
        self.parent_session_id = session_id
        self.pending_child_threads.clear()

    def note_pending_child_thread(
        self,
        thread_id: str,
        parent_thread_id: str | None,
    ) -> None:
        """
        Record a Codex child thread before its AP child session exists.

        :param thread_id: Codex child thread id announced by
            ``thread/started``, e.g. ``"thread_child"``.
        :param parent_thread_id: Codex parent thread id recorded in
            ``source.subAgent.thread_spawn.parent_thread_id``, e.g.
            ``"thread_parent"``. ``None`` when Codex omitted it.
        :returns: None.
        """
        if thread_id not in self.subagents_by_thread:
            self.pending_child_threads[thread_id] = parent_thread_id

    def is_pending_child_thread(
        self,
        thread_id: str,
        parent_thread_id: str | None,
    ) -> bool:
        """
        Return whether a thread is an announced-but-unregistered child.

        :param thread_id: Codex thread id, e.g. ``"thread_child"``.
        :param parent_thread_id: Active parent thread id to match, e.g.
            ``"thread_parent"``.
        :returns: ``True`` when the thread was proven to be a child
            by ``source.subAgent.thread_spawn`` metadata but has no AP
            child session mapping yet, and the recorded parent matches.
        """
        recorded_parent_thread_id = self.pending_child_threads.get(thread_id)
        if recorded_parent_thread_id is None:
            return thread_id in self.pending_child_threads
        return recorded_parent_thread_id == parent_thread_id

    def needs_child_thread_backfill(self, thread_id: str) -> bool:
        """
        Return whether a child thread's backlog should be replayed.

        :param thread_id: Codex child thread id, e.g. ``"thread_child"``.
        :returns: ``True`` until the child has been subscribed this connection.
        """
        return thread_id not in self.subscribed_child_threads

    def note_child_thread_subscribed(self, thread_id: str) -> None:
        """
        Record that a child thread's backlog was replayed this connection.

        :param thread_id: Codex child thread id, e.g. ``"thread_child"``.
        :returns: None.
        """
        self.subscribed_child_threads.add(thread_id)

    def note_user_message_posted(self, turn_id: str) -> None:
        """
        Record that a turn's user message has been posted to AP.

        :param turn_id: Codex turn id, e.g. ``"turn_123"``.
        :returns: None.
        """
        self.posted_user_turns.add(turn_id)

    def has_posted_user_message(self, turn_id: str) -> bool:
        """
        Return whether a turn's user message was already posted to AP.

        :param turn_id: Codex turn id, e.g. ``"turn_123"``.
        :returns: ``True`` when the turn's user message has been posted.
        """
        return turn_id in self.posted_user_turns

    def record_partial_text_delta(
        self,
        *,
        turn_id: str,
        item_type: str,
        item_id: str | None,
        delta: str,
    ) -> None:
        """
        Remember one visible text delta for possible interrupted-turn durability.

        :param turn_id: Codex turn id, e.g. ``"turn_123"``.
        :param item_type: Codex item type, e.g. ``"agentMessage"``.
        :param item_id: Codex item id, e.g. ``"item_abc123"``, or
            ``None`` when omitted.
        :param delta: Text fragment, e.g. ``"hel"``.
        :returns: None.
        """
        buffers = self.partial_text_by_turn.setdefault(turn_id, [])
        for buffer in buffers:
            if buffer.item_type == item_type and buffer.item_id == item_id:
                buffer.append(delta)
                return
        buffer = _PartialTextBuffer(item_type=item_type, item_id=item_id)
        buffer.append(delta)
        buffers.append(buffer)

    def discard_partial_text_item(
        self,
        *,
        turn_id: str,
        item_type: str,
        item_id: str | None,
    ) -> None:
        """
        Drop buffered deltas for an item whose completed record was observed.

        :param turn_id: Codex turn id, e.g. ``"turn_123"``.
        :param item_type: Codex item type, e.g. ``"agentMessage"``.
        :param item_id: Codex item id, e.g. ``"item_abc123"``, or
            ``None`` when omitted.
        :returns: None.
        """
        buffers = self.partial_text_by_turn.get(turn_id)
        if not buffers:
            return
        remaining = [
            buffer
            for buffer in buffers
            if not (buffer.item_type == item_type and buffer.item_id == item_id)
        ]
        if remaining:
            self.partial_text_by_turn[turn_id] = remaining
        else:
            self.partial_text_by_turn.pop(turn_id, None)

    def consume_partial_text_for_turn(self, turn_id: str) -> list[_PartialTextBuffer]:
        """
        Remove and return buffered visible text for one turn.

        :param turn_id: Codex turn id, e.g. ``"turn_123"``.
        :returns: Ordered partial-text buffers for the turn.
        """
        return self.partial_text_by_turn.pop(turn_id, [])

    def claim_item_key(self, item_key: str) -> bool:
        """
        Claim a transcript item key for Omnigent posting.

        Returns ``True`` when the caller should post the item. Returns
        ``False`` when the key was already posted this connection, so the
        caller should skip it and avoid a duplicate write.

        :param item_key: Stable dedup key, e.g.
            ``"thread_c:turn_c:item-1"``.
        :returns: ``True`` when the item should be posted.
        """
        if item_key in self.synced_item_keys:
            _logger.info("Codex forwarder skipped duplicate item: key=%s", item_key)
            return False
        self.synced_item_keys.add(item_key)
        return True

    def peek_anon_item_key(self, thread_id: str, turn_id: str) -> str:
        """
        Return the current positional key for an anonymous (no-id) item.

        Reads but does NOT advance the counter. Use ``advance_anon_counter``
        after a successful ``claim_item_key`` to mark the slot consumed.
        Two calls without an intervening advance return the same key, which
        is what dedup requires: replay and live deliveries of the same
        anonymous item must produce the same key so the second delivery
        is correctly dropped.

        :param thread_id: Codex thread id, e.g. ``"thread_123"``.
        :param turn_id: Codex turn id, e.g. ``"turn_123"``.
        :returns: Positional dedup key, e.g.
            ``"thread_123:turn_123:anon-0"``.
        """
        scope = (thread_id, turn_id)
        idx = self._anon_item_counters.get(scope, 0)
        return f"{thread_id}:{turn_id}:anon-{idx}"

    def advance_anon_counter(self, thread_id: str, turn_id: str) -> None:
        """
        Advance the anonymous item counter for a (thread, turn) scope.

        Called after ``claim_item_key`` succeeds for an anonymous item so
        the next anonymous item in the same turn gets a fresh key.

        :param thread_id: Codex thread id, e.g. ``"thread_123"``.
        :param turn_id: Codex turn id, e.g. ``"turn_123"``.
        :returns: None.
        """
        scope = (thread_id, turn_id)
        self._anon_item_counters[scope] = self._anon_item_counters.get(scope, 0) + 1

    def _note_model_fields(self, payload: dict[str, Any]) -> None:
        """
        Record model from a Codex settings-like payload.

        :param payload: Payload with ``model``.
        :returns: None.
        """
        model = payload.get("model")
        if isinstance(model, str) and model:
            self.model = model

@dataclass(frozen=True)
class _CodexTurnStatusEdge:
    """
    Omnigent session-status edge derived from Codex turn lifecycle state.

    :param status: Omnigent session status, e.g. ``"running"`` or ``"idle"``.
    :param turn_id: Codex turn id that caused the edge, e.g.
        ``"turn_abc123"``.
    :param source: Lifecycle source that produced the edge, e.g.
        ``"turn/started"``.
    """

    status: str
    turn_id: str | None
    source: str

@dataclass(frozen=True)
class _DeltaChunk:
    """
    One queued text delta with optional stream identity.

    :param message_id: Stable native message stream id, e.g.
        ``"codex:thread_123:turn_123:agentMessage:item_agent"``, or
        ``None`` for generic unscoped deltas.
    :param delta: Text fragment, e.g. ``"hel"``.
    """

    message_id: str | None
    delta: str

@dataclass(frozen=True)
class _DeltaFlushBarrier:
    """
    Queue marker that asks the delta worker to flush buffered text.

    :param done: Future completed after all preceding buffered deltas
        have been posted to AP.
    """

    done: asyncio.Future[None]

@dataclass(frozen=True)
class _DeltaFlushStop:
    """
    Queue marker that asks the delta worker to flush and exit.

    :param done: Future completed after the worker has flushed all
        buffered deltas and stopped.
    """

    done: asyncio.Future[None]

class _SessionUsageCoalescer:
    """
    Coalesce Codex token-usage updates before posting to AP.

    Codex can emit ``thread/tokenUsage/updated`` while assistant text
    is still streaming. This coalescer records only the latest values
    (latest-only, deduped) so repeated frames collapse to one post. The
    caller flushes it per usage frame (so the web UI cost badge updates
    live mid-turn) and again at turn/session boundaries (a no-op when
    nothing changed).

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        model: str | None = None,
    ) -> None:
        """
        Initialize the usage coalescer.

        :param client: HTTP client for Omnigent event posts.
        :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
        :param model: Model name to attach to token posts, e.g. ``"gpt-5.5"``.
            Needed for child coalescers, created where ``forwarder_state`` is
            ``None`` and ``record()`` receives no model — without it the server
            cannot price the child's cumulative tokens. ``None`` for the parent
            coalescer, which learns its model via :meth:`record`.
        :returns: None.
        """
        self._client = client
        self._session_id = session_id
        self._pending: dict[str, int] = {}
        self._last_posted: dict[str, int] = {}
        self._model: str | None = model

    def record(self, params: dict[str, Any], model: str | None = None) -> None:
        """
        Record the latest usage values from one Codex notification.

        :param params: Codex ``thread/tokenUsage/updated`` params.
        :param model: Latest known Codex model for this thread, e.g.
            ``"gpt-5.1-codex"``. Retained so :meth:`flush` can attach it
            to every token post; the server needs it to price cumulative
            tokens into ``total_cost_usd``. ``None`` leaves the prior
            value unchanged (Codex sends usage and settings separately,
            so a usage frame on its own carries no model).
        :returns: None.
        """
        if model:
            self._model = model
        data = _session_usage_data_from_params(params)
        if data is None:
            return
        self._pending.update(data)

    async def flush(self) -> None:
        """
        Post changed pending usage values to AP.

        :returns: None after the pending usage update has been
            attempted.
        """
        if not self._pending:
            return
        data = {
            key: value
            for key, value in self._pending.items()
            if self._last_posted.get(key) != value
        }
        if not data:
            self._pending.clear()
            return
        # Attach the model to every token-bearing post (not via the
        # changed-keys dedup, so it rides along even when only token
        # counts changed) — the server reprices cumulative tokens into
        # ``total_cost_usd`` per turn and needs the model each time.
        payload: dict[str, Any] = dict(data)
        if self._model:
            payload["model"] = self._model
        response = await _post_session_event(
            self._client,
            self._session_id,
            event_type="external_session_usage",
            data=payload,
        )
        _log_failed_session_event_post("external_session_usage", response)
        if response is not None and response.status_code < 400:
            self._last_posted.update(data)
            self._pending.clear()

    async def close(self) -> None:
        """
        Flush pending usage updates.

        :returns: None after the final usage flush has been attempted.
        """
        await self.flush()

@dataclass(frozen=True)
class _PendingCodexElicitation:
    """
    Background Omnigent hook wait for one Codex server-to-client request.

    :param thread_id: Codex thread id from the request params, e.g.
        ``"thread_abc123"``. ``None`` when the request did not carry
        thread scope.
    :param turn_id: Codex turn id from the request params, e.g.
        ``"turn_abc123"``. ``None`` when the request did not carry turn
        scope.
    :param request_id: Codex JSON-RPC request id, e.g. ``12``.
    :param elicitation_id: Omnigent elicitation id, e.g.
        ``"elicit_codex_abc123"``.
    """

    thread_id: str | None
    turn_id: str | None
    request_id: int | str
    elicitation_id: str

class _CodexElicitationTaskTracker:
    """
    Run Codex elicitation hook waits off the event-drain path.

    A real Codex TUI can answer a server-to-client request before the
    Omnigent web/REPL hook does. If the forwarder awaits the Omnigent hook inline,
    it stops draining app-server events and the web UI sees a stuck
    approval card until the hook timeout. This tracker lets the hook
    wait in the background and resolves it once the app-server emits the
    exact ``serverRequest/resolved`` notification for the same request id.
    """

    def __init__(self) -> None:
        """
        Initialize an empty pending-task tracker.

        :returns: None.
        """
        self._pending: dict[asyncio.Task[None], _PendingCodexElicitation] = {}
        self._posted_resolutions: set[str] = set()

    def start(
        self,
        client: httpx.AsyncClient,
        codex_client: CodexAppServerClient,
        *,
        session_id: str,
        event: CodexMessage,
    ) -> None:
        """
        Start one Omnigent hook bridge in the background.

        :param client: HTTP client for Omnigent hook posts.
        :param codex_client: Connected Codex app-server client used
            to send JSON-RPC results.
        :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
        :param event: Codex JSON-RPC request envelope.
        :returns: None.
        """
        params = event.get("params")
        params = params if isinstance(params, dict) else {}
        method = event.get("method")
        request_id = event.get("id")
        if not isinstance(method, str) or not _is_codex_request_id(request_id):
            _logger.warning("Codex forwarder cannot track malformed elicitation request")
            return
        task = asyncio.create_task(
            self._run_one(
                client,
                codex_client,
                session_id=session_id,
                event=event,
            ),
            name="codex-native-elicitation-hook",
        )
        self._pending[task] = _PendingCodexElicitation(
            thread_id=_thread_id_from_params(params),
            turn_id=_turn_id_from_payload(params.get("turn")) or _turn_id_from_payload(params),
            request_id=request_id,
            elicitation_id=codex_elicitation_id(
                session_id,
                method,
                request_id,
            ),
        )
        task.add_done_callback(self._discard_done)

    async def resolve_by_server_notification(
        self,
        client: httpx.AsyncClient,
        *,
        session_id: str,
        params: dict[str, Any],
    ) -> None:
        """
        Mark the hook wait resolved by Codex's explicit notification.

        :param client: HTTP client for Omnigent event posts.
        :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
        :param params: ``serverRequest/resolved`` params, e.g.
            ``{"threadId": "thread_abc", "requestId": 12}``.
        :returns: None.
        """
        request_id = params.get("requestId")
        thread_id = _thread_id_from_params(params)
        for _task, pending in list(self._pending.items()):
            if _pending_elicitation_matches_resolution(
                pending,
                request_id=request_id,
                thread_id=thread_id,
            ):
                await self._post_resolved_once(client, session_id, pending)
                return

    async def resolve_by_terminal_turn_event(
        self,
        client: httpx.AsyncClient,
        *,
        session_id: str,
        params: dict[str, Any],
    ) -> None:
        """
        Clear pending hook waits after Codex accepts a terminal turn.

        This is a conservative fallback for a missed
        ``serverRequest/resolved`` notification. Codex documents
        ``turn/completed`` as the terminal lifecycle event, including
        interrupted and failed turns, and terminal cleanup implies the
        app-server no longer has live server-to-client requests for that
        turn.

        :param client: HTTP client for Omnigent event posts.
        :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
        :param params: Codex ``turn/completed`` params, e.g.
            ``{"threadId": "thread_abc", "turn": {"id": "turn_abc"}}``.
        :returns: None.
        """
        thread_id = _thread_id_from_params(params)
        turn_id = _turn_id_from_payload(params.get("turn")) or _turn_id_from_payload(params)
        for _task, pending in list(self._pending.items()):
            if _pending_elicitation_matches_terminal_turn(
                pending,
                thread_id=thread_id,
                turn_id=turn_id,
            ):
                await self._post_resolved_once(client, session_id, pending)

    async def drain(self) -> None:
        """
        Wait for currently pending hook waits without cancelling them.

        :returns: None after every task that was pending at entry has
            reached a terminal state.
        """
        if not self._pending:
            return
        await asyncio.gather(*list(self._pending), return_exceptions=True)

    async def close(self) -> None:
        """
        Cancel all pending hook waits and wait for their cleanup.

        :returns: None after all background hook tasks have finished.
        """
        if not self._pending:
            return
        tasks = list(self._pending)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()
        self._posted_resolutions.clear()

    async def _run_one(
        self,
        client: httpx.AsyncClient,
        codex_client: CodexAppServerClient,
        *,
        session_id: str,
        event: CodexMessage,
    ) -> None:
        """
        Run one hook bridge and log non-cancellation failures.

        :param client: HTTP client for Omnigent hook posts.
        :param codex_client: Connected Codex app-server client.
        :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
        :param event: Codex JSON-RPC request envelope.
        :returns: None.
        """
        try:
            await _handle_codex_elicitation_request(
                client,
                codex_client,
                session_id=session_id,
                event=event,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the long-lived forwarder alive.
            _logger.warning(
                "Codex forwarder elicitation hook task failed: method=%s",
                event.get("method"),
                exc_info=True,
            )

    def _discard_done(self, task: asyncio.Task[None]) -> None:
        """
        Remove a completed task and consume its terminal state.

        :param task: Completed hook task.
        :returns: None.
        """
        pending = self._pending.pop(task, None)
        if task.cancelled():
            if pending is not None:
                self._posted_resolutions.discard(pending.elicitation_id)
            return
        task.exception()
        if pending is not None:
            self._posted_resolutions.discard(pending.elicitation_id)

    async def _post_resolved_once(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        pending: _PendingCodexElicitation,
    ) -> None:
        """
        Post one Omnigent resolution signal, suppressing duplicates.

        :param client: HTTP client for Omnigent event posts.
        :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
        :param pending: Pending hook wait metadata to resolve.
        :returns: None.
        """
        if pending.elicitation_id in self._posted_resolutions:
            return
        posted = await _post_external_elicitation_resolved(
            client,
            session_id,
            elicitation_id=pending.elicitation_id,
        )
        if posted:
            self._posted_resolutions.add(pending.elicitation_id)

def _delta_recovery_status_edge(
    bridge_dir: Path,
    params: dict[str, Any],
    turn_id: str | None,
) -> _CodexTurnStatusEdge | None:
    """
    Recover a missed turn start from a scoped Codex delta.

    :param bridge_dir: Native Codex bridge directory.
    :param params: Codex delta notification params.
    :param turn_id: Turn id extracted from *params*, e.g.
        ``"turn_abc123"``.
    :returns: Running status edge when the delta adopts the turn, or
        ``None`` when the delta is stale or ambiguous.
    """
    if not _try_recover_active_turn_from_delta(bridge_dir, params, turn_id):
        return None
    return _CodexTurnStatusEdge(
        status="running",
        turn_id=turn_id,
        source="delta:recovered",
    )


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _collab as _sib_collab
    from . import _deltas as _sib_deltas
    from . import _elicitation as _sib_elicitation
    from . import _events as _sib_events
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
    for _key, _value in _sib_events.__dict__.items():
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
