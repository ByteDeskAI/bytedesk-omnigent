"""Pydantic models for the API layer — request/response shapes AND
SSE stream events.

This module is split into two sections, separated by a clearly marked
delineator further down:

1. Request and response body schemas for the JSON endpoints.
2. SSE event payload models — the discriminated union that every
   event the server emits over its SSE endpoints validates against.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnigent.entities import ConversationItem

# ── Shared ──────────────────────────────────────────────────────


def _import_api_bindings() -> None:
    from . import _api as _pkg_api
    g = globals()
    for _key, _value in _pkg_api.__dict__.items():
        if not _key.startswith("__"):
            g[_key] = _value


_import_api_bindings()

# STREAM EVENTS — typed Pydantic union for SSE event boundary
# ─────────────────────────────────────────────────────────────────────
#
# Single source of truth for the omnigent SSE event stream. Every
# event the server emits over its two SSE endpoints is modeled below
# as a Pydantic class with a ``type: Literal[...]`` discriminator, and
# the ``ServerStreamEvent`` annotated union routes raw event dicts to
# the right concrete model. Server, runtime, REPL/TUI, and SDK all
# import these names from this module so wire-name renames and
# payload changes are a one-edit change.
#
# The SSE endpoint is:
#
# * ``GET /v1/sessions/{id}/stream`` — session live-tail (multiplexes
#   the underlying response stream and surfaces queue/interrupt
#   semantics).
#
# Two event families coexist:
#
# * ``session.*`` — session-scoped lifecycle events
#   (:class:`SessionStatusEvent`, :class:`SessionInputConsumedEvent`,
#   :class:`SessionInterruptedEvent`, :class:`SessionCreatedEvent`).
# * ``response.*`` — pass-through Responses-API events emitted by the
#   executor; the session stream multiplexes them unchanged.
#
# Channel split (per ``designs/session_rearchitecture.md`` §3 "Two
# channels"). Each event variant is conceptually either *transient*
# or *persistent*:
#
# * Transient (SSE-only) — text/reasoning deltas, turn lifecycle
#   events, ``session.*`` lifecycle events, retry/heartbeat/error
#   signals, ``approval_required``. Fire-and-forget on the SSE
#   stream — NOT persisted.
# * Persistent (POST + SSE replay) — assistant messages, tool calls,
#   tool results, and compaction summaries. Persist-then-publish is
#   enforced inside ``_persist_and_stream``.
#
# Wire-shape note: the server today emits some events with a flat
# shape (``{"type": ..., <fields>}``) and others with a nested
# ``{"type": ..., "data": {...}}`` envelope. The Pydantic models
# below match the wire shapes verbatim — see each model's docstring
# for the emit site reference.
# ─────────────────────────────────────────────────────────────────────


# ── Module-level constants (rule 34) ──────────────────────────────

# Forward-compatibility note: every event model uses ``ConfigDict(
# extra="ignore")`` so harnesses (or AP) can add new fields to an
# event in a future contract revision without breaking older
# parsers — see ``designs/SERVER_HARNESS_CONTRACT.md`` §Validation
# discipline (loose by default).


class _SSEEventBase(BaseModel):
    """
    Common base for every SSE event payload model.

    All events share two ambient fields:

    - ``type``: the event-type discriminator literal (defined per
      subclass so :data:`ServerStreamEvent` can dispatch).
    - ``sequence_number``: monotonic per-stream sequence number
      assigned by the SSE serializer at emit time
      (``_format_sse`` in ``omnigent/server/routes/sessions.py``).
      Producers leave it ``None``; the route serializer populates
      it on the wire. ``None`` on session-scoped events emitted
      directly by the runtime (the session stream does not number
      events).

    Subclasses MUST declare ``type`` as ``Literal[...]`` so the
    discriminated-union machinery can route incoming dicts. The
    ``model_config`` is forward-compatible — see the module
    docstring for the rationale.

    :param sequence_number: Per-stream monotonic counter assigned
        by the SSE serializer, e.g. ``42``. ``None`` on the
        producer side (before serialization) and on session-scoped
        events that the runtime publishes directly without
        sequencing.
    """

    sequence_number: int | None = None

    model_config = ConfigDict(extra="ignore")


# ── Session-scoped events (session.*) ──────────────────────────────


class SessionStatusEvent(_SSEEventBase):
    """
    Session lifecycle status transition.

    Emitted by the runtime / session route handler at every
    transition between ``launching`` / ``running`` / ``waiting`` /
    ``idle`` / ``failed``. Wire shape is
    FLAT (not enveloped): ``{"type": "session.status",
    "conversation_id": "...", "status": "...",
    "sequence_number": null}``.

    The ``waiting`` value is emitted by the runtime's parent agent
    loop when it parks on the ``async_work_complete`` drain
    (``_drain_async_completions(block_for_one=True)`` in
    ``omnigent/runtime/workflow.py``) — i.e. while the parent
    turn is suspended waiting for background tools or sub-agents
    to complete. Per the session-rearchitecture spec §3
    ("Event types and direction"), ``waiting`` is the
    session-status companion of the spec's ``turn.waiting``
    transient — clients should render the session as actively
    blocked-on-async-work, distinct from ``running``. When the
    drain wakes (a child completed), the runtime emits a follow-up
    ``running`` to resume.

    :param type: Always ``"session.status"``.
    :param conversation_id: The conversation/session identifier
        whose status changed, e.g. ``"conv_abc123"``.
    :param status: New session status. ``"launching"`` (session or
        child task created, but no concrete harness start observed),
        ``"idle"`` (no loop running), ``"running"`` (loop executing),
        ``"waiting"`` (parent turn parked on the async-work drain), or
        ``"failed"`` (terminal failure).
    :param response_id: Optional active response id for terminal-backed
        integrations, e.g. ``"codex_turn_abc123"``. Clients use it to
        associate coarse session status edges with the assistant bubble
        they describe. ``None`` for ordinary in-process runtime edges.
    :param error: Machine-readable failure detail, present only
        when ``status == "failed"``. Carries the message the
        runner attached when a turn died — most importantly a
        SETUP-phase failure (spec resolution, spawn-env build)
        that ends the turn before any ``response.failed`` event
        is emitted. ``None`` for every non-failed transition.
        Clients render ``error.message`` as the terminal error
        line; without it a setup failure shows as a silent end.

    Category: **transient** (SSE-only). Status is rederived on
    reconnect from the cached last-relayed turn lifecycle event
    or by re-querying the runner; not persisted by the runtime.
    """

    type: Literal["session.status"]
    conversation_id: str
    status: Literal["idle", "launching", "running", "waiting", "failed"]
    response_id: str | None = None
    error: ErrorDetail | None = None


class SessionUsageEvent(_SSEEventBase):
    """
    Token-usage update from a terminal-backed integration.

    Emitted after an ``external_session_usage`` POST from an
    out-of-AP runtime (e.g. the ``omnigent claude`` transcript
    forwarder). Either field may be absent; clients should leave
    cached values untouched for missing fields.

    :param type: Always ``"session.usage"``.
    :param conversation_id: Session identifier.
    :param context_tokens: ``input + cache_creation + cache_read``
        from the latest assistant ``message.usage``. ``None`` on a
        window-only broadcast.
    :param context_window: Resolved window in tokens (e.g. 200_000
        normally, 1_000_000 with ``opus[1m]`` / ``sonnet[1m]``).
        ``None`` on a tokens-only broadcast.
    :param total_cost_usd: Cumulative session spend in USD after this
        update, e.g. ``0.42`` — the server-computed total the
        cost-budget policy gates on. Present **only when the session
        is priced**; omitted (``None``, stripped by ``exclude_none``)
        when unpriced or on a broadcast that carries no cost change,
        so the client keeps its prior value (the snapshot seeds the
        initial "—" for an unpriced session). Once a session is priced
        the total only grows, so it never reverts to unpriced.
    :param usage_by_model: Per-model breakdown of the same subtree usage
        after this update, keyed by raw harness model id, e.g.
        ``{"claude-sonnet-4-6": ModelUsage(input_tokens=12000, ...)}``.
        ``None`` (stripped by ``exclude_none``) on a broadcast that carries
        no per-model change, so the client keeps its cached map.

    Category: **transient** (SSE-only). On reconnect, clients seed
    the ring from the session snapshot's ``last_total_tokens`` and
    ``context_window``, the cost indicator from ``total_cost_usd``,
    and the per-model token breakdown from ``usage_by_model``.
    """

    type: Literal["session.usage"]
    conversation_id: str
    context_tokens: int | None = None
    context_window: int | None = None
    total_cost_usd: float | None = None
    usage_by_model: dict[str, ModelUsage] | None = None


class SessionModelEvent(_SSEEventBase):
    """
    Active-model update from a terminal-backed integration.

    Emitted after an ``external_model_change`` POST from the
    ``omnigent claude`` transcript forwarder when the model is
    switched inside the Claude Code terminal (a ``/model`` command or
    the in-TUI picker). Lets the web model picker reflect a TUI-side
    switch without a reload.

    :param type: Always ``"session.model"``.
    :param conversation_id: Session identifier, e.g. ``"conv_abc123"``.
    :param model: Tier alias the session is now on, e.g. ``"opus"`` —
        Claude Code's version-agnostic alias, matching the picker's
        vocabulary (not a pinned ``"claude-opus-4-8"`` id).

    Category: **transient** (SSE-only). The server also writes
    ``model_override`` on the conversation, so on reconnect clients
    restore the selection from the snapshot's ``model_override`` rather
    than from a replayed event.
    """

    type: Literal["session.model"]
    conversation_id: str
    model: str


class SessionAgentChangedEvent(_SSEEventBase):
    """
    Bound-agent change on a live session.

    Emitted by the switch-agent route after the session's agent binding
    is rewritten in place. Connected clients re-derive their cached
    session state (harness presentation labels, bound agent id/name)
    from a fresh snapshot — the chat UI's native-vs-SDK message
    lifecycle depends on those labels, so a stale cache drops the first
    post-switch message (it reappears only when the transcript
    round-trip lands).

    :param type: Always ``"session.agent_changed"``.
    :param conversation_id: Session identifier, e.g. ``"conv_abc123"``.
    :param agent_id: The session-scoped clone now bound to the session,
        e.g. ``"ag_abc123"``.
    :param agent_name: Display name of the agent the session now runs,
        e.g. ``"claude-native-ui"``. Deliberately the clean target-agent
        name — not the clone row's ``"… (switch ag_…)"`` disambiguation
        name — because clients render it verbatim.

    Category: **transient** (SSE-only). The switch is persisted on the
    conversation row, so on reconnect clients read the new binding from
    the session snapshot rather than from a replayed event.
    """

    type: Literal["session.agent_changed"]
    conversation_id: str
    agent_id: str
    agent_name: str


class SessionTodosEvent(_SSEEventBase):
    """
    Todo-list update from a Claude Code terminal-backed session.

    Emitted after an ``external_session_todos`` POST from the
    ``omnigent claude`` transcript forwarder, which captures todo
    updates via ``PostToolUse``/``TodoWrite`` hook events from Claude
    Code and forwards them to the Omnigent server. Lets ap-web render a
    live todo panel in the right column without polling.

    :param type: Always ``"session.todos"``.
    :param conversation_id: Session identifier,
        e.g. ``"conv_abc123"``.
    :param todos: Current todo items read from Claude's todo file.
        Each entry is a raw dict with ``content`` (str),
        ``status`` (``"pending"`` | ``"in_progress"`` |
        ``"completed"``), and ``activeForm`` (str, the gerund form)
        keys, e.g. ``[{"content": "Fix the bug", "status":
        "in_progress", "activeForm": "Fixing the bug"}]``.

    Category: **transient** (SSE-only). On reconnect, clients seed
    the panel from the session snapshot's ``todos`` field, which is
    populated by ``_session_todos_cache`` at snapshot build time.
    """

    type: Literal["session.todos"]
    conversation_id: str
    todos: list[dict[str, Any]]


class SessionTerminalPendingEvent(_SSEEventBase):
    """
    Terminal spin-up status for a terminal-first session.

    Two sources emit this event:

    1. The Omnigent server at ``POST /v1/sessions`` for host-launched
       terminal-first sessions — the earliest possible point, before
       the runner even starts, so the spinner appears immediately on
       session create rather than after the runner boots.
    2. The Omnigent relay when the runner's ``session.terminal_pending`` frame
       arrives — covers non-host-launched sessions (e.g. server-dispatched
       sub-agents) and carries the authoritative ``pending=False`` clear
       emitted by the runner's ``finally`` block.

    Together they allow ap-web to show a spinner on the Terminal pill
    while the backend boots the terminal instead of a silent greyed-out
    button, and to distinguish "still starting up" from "no terminal"
    (killed or never created).

    :param type: Always ``"session.terminal_pending"``.
    :param conversation_id: Session identifier,
        e.g. ``"conv_abc123"``.
    :param pending: ``True`` while the terminal is being created;
        ``False`` once it lands or auto-create fails.

    Category: **transient** (SSE-only). On reconnect, clients seed the
    spinner from the session snapshot's ``terminal_pending`` field,
    which is populated by ``_session_terminal_pending_cache`` at
    snapshot build time.
    """

    type: Literal["session.terminal_pending"]
    conversation_id: str
    pending: bool


class SessionSandboxStatusEvent(_SSEEventBase):
    """
    Managed-sandbox launch progress for a ``host_type="managed"`` session.

    A managed create returns before its sandbox exists; the Omnigent
    server emits this event as the background launch pipeline advances
    so the Web UI can show live provisioning progress on the session
    page instead of a silent dead chat: sandbox provision → repository
    clone → host startup → runner connect → ready, or a terminal
    failure with the reason.

    :param type: Always ``"session.sandbox_status"``.
    :param conversation_id: Session identifier,
        e.g. ``"conv_abc123"``.
    :param stage: The launch stage just entered, e.g.
        ``"provisioning"`` — see :class:`SandboxStatus` for the full
        pipeline order.
    :param error: Failure detail when ``stage == "failed"``, e.g.
        ``"managed sandbox launch failed: spend limit reached"``.
        ``None`` otherwise.

    Category: **transient** (SSE-only). On reconnect, clients seed the
    progress indicator from the session snapshot's ``sandbox_status``
    field, which is populated by ``_session_sandbox_status_cache`` at
    snapshot build time.
    """

    type: Literal["session.sandbox_status"]
    conversation_id: str
    stage: SandboxLaunchStage
    error: str | None = None


class SessionSkillsEvent(_SSEEventBase):
    """
    Signal that a session's runner-owned skills have resolved.

    Skills are discovered against the bound runner's filesystem and
    fetched off the session-snapshot hot path: the snapshot kicks a
    single background fetch (``_load_runner_skills`` in
    ``omnigent/server/routes/sessions.py``) and serves ``[]`` until
    it lands. This event fires the moment that background fetch
    populates the per-session skills cache, so a connected web client
    can re-read the snapshot and fill its slash-command menu instead
    of waiting for the next bind.

    Carries no payload beyond the conversation id — it is a "skills
    are ready, re-read the snapshot" nudge, mirroring the
    invalidate-then-refetch shape used by
    :class:`SessionChangedFilesInvalidatedEvent`. The snapshot's
    ``skills`` field (now cache-backed) stays the source of truth.

    :param type: Always ``"session.skills"``.
    :param conversation_id: Session identifier,
        e.g. ``"conv_abc123"``.

    Category: **transient** (SSE-only). On reconnect, clients seed
    the menu from the session snapshot's ``skills`` field, which is
    populated by the runner-skills cache at snapshot build time.
    """

    type: Literal["session.skills"]
    conversation_id: str


class SessionInputConsumedPayload(BaseModel):
    """
    Inner payload of a :class:`SessionInputConsumedEvent`.

    Emitted by the sessions route handler at the moment a client
    input is persisted into ``conversation_items``. Carries the
    persisted-item shape so clients can render the input (e.g.
    the user's message bubble) at the moment of acceptance.

    :param item_id: Stable identifier of the conversation item
        just persisted, e.g. ``"item_abc123"``.
    :param type: The item type discriminator — ``"message"`` for
        user messages, ``"function_call_output"`` for tool
        results, etc. Mirrors
        :class:`omnigent.server.schemas.SessionEventInput`'s
        ``type`` field.
    :param data: Decoded item payload, e.g.
        ``{"role": "user", "content": [{"type": "input_text",
        "text": "Hello"}]}``. Heterogeneous and ``type``-specific.
    :param created_by: Email of the human actor who posted the item,
        e.g. ``"alice@example.com"``. ``None`` for agent/tool/system
        items and single-user mode. Mirrors
        :meth:`ConversationItem.to_api_dict` for live attribution.
    :param cleared_pending_id: When this consumed message drains a
        :mod:`omnigent.runtime.pending_inputs` entry (a native-
        terminal web message round-tripping back from the transcript),
        the drained entry's id, e.g. ``"pending_a1b2c3"``. Lets a
        client drop the matching optimistic bubble by id instead of
        by position. ``None`` for non-native messages and for messages
        that matched no pending entry (e.g. typed directly in the TUI).
    """

    item_id: str
    type: str
    # Heterogeneous payload — concrete shape varies by ``type``
    # (matches :class:`SessionEventInput.data`).
    data: dict[str, Any]
    created_by: str | None = None
    cleared_pending_id: str | None = None

    model_config = ConfigDict(extra="ignore")


class SessionInputConsumedEvent(_SSEEventBase):
    """
    A queued input item was materialized into conversation history.

    Emitted by ``POST /v1/sessions/{id}/events`` once per accepted
    input item at the moment it is persisted into conversation
    history (either onto a steered active turn or as the seed item
    of a freshly-started one). Wire shape uses the NESTED envelope:
    ``{"type": "session.input.consumed", "data":
    <:class:`SessionInputConsumedPayload`>, "sequence_number":
    null}``.

    The event name is **provisional** — it may be renamed in a
    future revision. Consumers should reference
    :data:`SessionInputConsumedEvent` (or its ``type`` literal)
    rather than hardcoding the wire string.

    :param type: Always ``"session.input.consumed"``.
    :param data: The decoded queued-item payload — see
        :class:`SessionInputConsumedPayload`.
    """

    type: Literal["session.input.consumed"]
    data: SessionInputConsumedPayload


class SessionInterruptedPayload(BaseModel):
    """
    Inner payload of a :class:`SessionInterruptedEvent`.

    Built by ``_publish_interrupted`` in
    ``omnigent/server/routes/sessions.py``.

    :param requested_at: Unix epoch seconds when the interrupt
        request reached the server, e.g. ``1704067200``.
    :param response_id: Optional active response id for terminal-backed
        integrations, e.g. ``"codex_turn_abc123"``.
    """

    requested_at: int
    response_id: str | None = None

    model_config = ConfigDict(extra="ignore")


class SessionInterruptedEvent(_SSEEventBase):
    """
    User-triggered cancel reached the loop.

    Emitted by ``_publish_interrupted`` in
    ``omnigent/server/routes/sessions.py`` when a client posts
    a ``{"type": "interrupt"}`` to ``POST
    /v1/sessions/{id}/events``. Co-emitted with
    :class:`IncompleteEvent` (with the underlying response carrying
    ``incomplete_details.reason == "user_interrupt"``) so off-the-
    shelf Responses parsers still close cleanly. Wire shape uses
    the NESTED envelope verbatim from the existing emit site.

    :param type: Always ``"session.interrupted"``.
    :param data: The interrupt metadata — see
        :class:`SessionInterruptedPayload`.
    """

    type: Literal["session.interrupted"]
    data: SessionInterruptedPayload


class SessionCreatedEvent(_SSEEventBase):
    """
    A child (sub-agent) session was spawned from this session.

    Emitted by ``omnigent/tools/builtins/spawn.py:_spawn_one``
    onto the **parent** session's conversation stream after the
    child conversation row is created and the child task has been
    started. Per the session-rearchitecture spec §3 ("Event types
    and direction") and §7 ("Flow: client interacts with
    sub-agent"), this lets clients watching the parent session's
    SSE subscribe directly to the child's stream without polling
    history for the tunneled ``function_call`` item.

    The wire shape is FLAT (not enveloped):
    ``{"type": "session.created", "conversation_id": <parent>,
    "child_session_id": <child>, "agent_id": <agent or None>,
    "parent_session_id": <parent>, "sequence_number": null}``.

    The existing tunneled ``function_call`` ConversationItem
    (carried inside :class:`OutputItemDoneEvent`) is retained
    for compatibility — clients that don't yet implement the
    "subscribe to child stream" pattern can keep rendering sub-
    agent calls from the parent's persistent history.

    :param type: Always ``"session.created"``.
    :param conversation_id: The PARENT session/conversation id —
        this event rides the parent's stream, e.g.
        ``"conv_parent123"``.
    :param child_session_id: The newly-created child session id,
        e.g. ``"conv_child456"``. Same as ``conversation_id`` on
        the child's own stream when consumers pivot to it.
    :param agent_id: Registered agent id the child runs as,
        e.g. ``"agent_xyz"``. ``None`` is permitted only for
        legacy spawn paths that did not record an agent id;
        new code MUST set it.
    :param parent_session_id: Echo of ``conversation_id`` for
        consumers that key on a dedicated "parent" field rather
        than the carrier ``conversation_id``. Always equal to
        ``conversation_id``; included for forward-compat with
        clients that may relay these events across stream
        boundaries.

    Category: **transient** (SSE-only). The corresponding durable
    record of "a child session exists" lives in the conversation
    store as the child conversation row itself
    (``parent_conversation_id`` foreign key) and the parent's
    tunneled ``function_call`` item — reconnecting clients
    discover children by walking the parent's persisted history,
    not by replaying this event.
    """

    type: Literal["session.created"]
    conversation_id: str
    child_session_id: str
    agent_id: str | None = None
    parent_session_id: str | None = None


# ── Response pass-through events (response.*) ──────────────────────


class OutputTextDeltaEvent(_SSEEventBase):
    """
    Incremental assistant-text token emitted during streaming.

    Wire shape matches the existing raw-dict emit at
    ``omnigent/runtime/workflow.py:1352-1356``.

    :param type: Always ``"response.output_text.delta"``.
    :param delta: The text fragment for this chunk, e.g.
        ``"Hello"``.
    :param message_id: For terminal-observed streaming (claude-native),
        the vendor's stable per-message id, e.g.
        ``"2ca51d97-2f0f-493a-aed7-85a5b56c5747"``. Lets the web UI scope
        an in-flight buffer to one assistant message and reconcile it
        against the final item. ``None`` for ordinary in-process task
        streaming, where deltas already group by the active response.
    :param index: 0-based chunk order within the message, e.g. ``3``.
        ``None`` when not terminal-observed streaming.
    :param final: ``True`` on the last chunk of a terminal-observed
        message; ``None`` otherwise. Signals the web UI that no further
        chunks for ``message_id`` will arrive.
    """

    type: Literal["response.output_text.delta"]
    delta: str
    message_id: str | None = None
    index: int | None = None
    final: bool | None = None


class ReasoningStartedEvent(_SSEEventBase):
    """
    Marker emitted once when a reasoning block begins.

    Fired even when the reasoning content itself is encrypted /
    redacted (so no delta events follow), letting clients render
    a "thinking…" indicator regardless of provider verification
    status. Wire shape matches ``omnigent/runtime/workflow.py:1350``.

    :param type: Always ``"response.reasoning.started"``.
    """

    type: Literal["response.reasoning.started"]


class ReasoningTextDeltaEvent(_SSEEventBase):
    """
    Incremental reasoning-text token (full chain-of-thought).

    Only emitted by providers that surface reasoning content
    (e.g. OpenAI o-series with appropriate verification). Wire
    shape matches ``omnigent/runtime/workflow.py:1358-1364``.

    :param type: Always ``"response.reasoning_text.delta"``.
    :param delta: The reasoning text fragment, e.g.
        ``"Considering the user's intent..."``.
    """

    type: Literal["response.reasoning_text.delta"]
    delta: str


class ReasoningSummaryTextDeltaEvent(_SSEEventBase):
    """
    Incremental reasoning-summary token.

    Emitted when ``reasoning.summary`` is configured on the
    request. Wire shape matches
    ``omnigent/runtime/workflow.py:1370-1373``.

    :param type: Always ``"response.reasoning_summary_text.delta"``.
    :param delta: The summary text fragment, e.g. ``"Will use
        the search tool to gather context."``.
    """

    type: Literal["response.reasoning_summary_text.delta"]
    delta: str


class OutputItemDoneEvent(_SSEEventBase):
    """
    A conversation output item completed during the turn.

    Carries any item type the conversation persists (message,
    function_call, function_call_output, reasoning, compaction,
    native_tool, …). The ``item`` payload's wire shape merges
    common fields (``id``, ``type``, ``status``) with the
    type-specific data fields — it is NOT nested as
    ``{type, data}``.

    :param type: Always ``"response.output_item.done"``.
    :param item: The completed item dict. Heterogeneous and
        item-type-specific; see
        ``omnigent/entities/conversation.py`` for the
        per-type ``*Data`` shapes that drive serialization.
        Example for a function_call item: ``{"id": "fc_abc123",
        "type": "function_call", "status": "action_required",
        "name": "search.web", "arguments": "{\\"q\\": \\"foo\\"}",
        "call_id": "call_xyz"}``.
    """

    type: Literal["response.output_item.done"]
    # ``dict[str, Any]`` because items are heterogeneous and
    # type-specific (their per-type ``*Data`` shapes already
    # live in entities/conversation.py via ITEM_TYPE_TO_DATA_CLS).
    # Modeling each variant here would duplicate that mapping;
    # consumers that need typed item data parse via
    # ``parse_item_data(item["type"], item)``.
    item: dict[str, Any]


class InjectionConsumedEvent(_SSEEventBase):
    """
    Runner-internal marker: a mid-turn injection was consumed.

    Emitted by the executor adapter (``_watch_injections``) once the
    inner executor accepts a live mid-turn injection into the running
    turn. It rides the harness→runner turn stream and is intercepted by
    the runner's proxy_stream relay: the runner drops the buffered copy
    of the matching message so it is NOT re-delivered as a continuation
    turn (RUNNER_MESSAGE_INGEST.md Part B). This event is **never**
    published to the client session stream or relayed upstream — it is
    purely a runner-internal exactly-once handshake.

    :param type: Always ``"injection.consumed"``.
    :param injection_id: Correlation id the runner stamped on the
        forwarded injection, e.g. ``"inj_ab12cd34ef56"``. Matches the
        ``injection_id`` on the buffered message the runner drops.
    """

    type: Literal["injection.consumed"]
    injection_id: str


class OutputFileDoneEvent(_SSEEventBase):
    """
    A streamed file output completed materializing.

    Emitted by ``_emit_file_annotation_events`` in
    ``omnigent/runtime/workflow.py`` once per file annotation in
    the assistant's output. ``filename`` and ``content_type`` are
    only populated when the originating annotation carried them.

    :param type: Always ``"response.output_file.done"``.
    :param file_id: Identifier of the materialized file,
        e.g. ``"file_abc123"``.
    :param filename: Original filename if the annotation supplied
        one, e.g. ``"report.pdf"``. ``None`` otherwise.
    :param content_type: MIME content type if the annotation
        supplied one, e.g. ``"application/pdf"``. ``None``
        otherwise.
    """

    type: Literal["response.output_file.done"]
    file_id: str
    filename: str | None = None
    content_type: str | None = None


class HeartbeatEvent(_SSEEventBase):
    """
    Keepalive event emitted on a fixed cadence during streaming.

    Lets consumers detect stalled producers via missed-interval
    timing. Cadence is set by ``_HEARTBEAT_INTERVAL_S`` in
    ``omnigent/runtime/workflow.py`` (15 seconds at the time of
    writing). Wire shape matches the existing emit at
    ``omnigent/runtime/workflow.py:4636-4639``.

    Per ``designs/SERVER_HARNESS_CONTRACT.md`` §Heartbeats, the
    event MAY carry timing metadata so consumers can do richer
    dead-detection than "did anything arrive":

    - ``server_time`` is the producer's wall-clock at emission,
      letting consumers detect clock drift between producer and
      consumer.
    - ``last_event_seq`` is the ``sequence_number`` of the most
      recent NON-heartbeat event (or ``None`` when this is the
      first heartbeat before any user-visible event), letting
      consumers detect dropped events on reconnect.

    Both fields are optional on the wire (``None`` round-trips as
    omitted) so older AP→harness pairs that pre-date the field
    addition still parse cleanly.

    :param type: Always ``"response.heartbeat"``.
    :param server_time: ISO 8601 UTC timestamp at emission, e.g.
        ``"2026-04-27T15:30:00Z"``. ``None`` when the producer
        chose not to populate it (legacy emitters).
    :param last_event_seq: Sequence number of the last non-
        heartbeat event seen on the same stream, e.g. ``42``.
        ``None`` before any user-visible event has fired (first
        heartbeat of the turn, before deltas land), or when the
        producer chose not to populate it.
    """

    type: Literal["response.heartbeat"]
    server_time: str | None = None
    last_event_seq: int | None = None


class SessionHeartbeatEvent(_SSEEventBase):
    """
    Idle-stream keepalive on ``GET /v1/sessions/{id}/stream``.

    Emitted by the session-stream route on a fixed cadence whenever
    the underlying publish queue has been quiet (no turn in flight,
    no resource events). Distinct from :class:`HeartbeatEvent`
    (``response.heartbeat``), which is per-turn and is driven by
    the runtime workflow while a response is producing output.

    Why this exists: the session stream stays open across many turns
    and through idle periods (waiting for the user to type). Without
    a periodic emit, intermediate proxies, OS-level sockets, and the
    client's SSE read-timeout can leave a half-open stream
    undetected for minutes after a network event (laptop sleep,
    Wi-Fi handoff). The heartbeat puts a regular byte on the wire
    so the client's read-timeout and the server's
    ``request.is_disconnected()`` check both fire promptly.

    Consumers MAY ignore the payload entirely (the bytes crossing
    the wire are sufficient). The optional ``server_time`` mirrors
    :class:`HeartbeatEvent` for symmetry and debugging.

    :param type: Always ``"session.heartbeat"``.
    :param server_time: ISO 8601 UTC timestamp at emission, e.g.
        ``"2026-05-25T10:30:00Z"``. ``None`` when the producer
        chose not to populate it.
    """

    type: Literal["session.heartbeat"]
    server_time: str | None = None


class PresenceViewer(BaseModel):
    """
    One user currently viewing a session (holding its SSE stream open).

    :param user_id: The viewer's authenticated identity,
        e.g. ``"alice@example.com"``. Never the reserved single-user
        ``"local"`` sentinel — presence only tracks distinct human
        actors (see ``attribution_user``).
    :param joined_at: ISO 8601 UTC timestamp of when the user joined,
        e.g. ``"2026-06-10T17:00:00Z"``. Stable across reconnects
        within the server's leave-grace window.
    :param idle: Whether every stream the user holds reports an idle
        (backgrounded) tab. The web greys idle viewers' avatars.
    """

    user_id: str
    joined_at: str
    idle: bool = False


class SessionPresenceEvent(_SSEEventBase):
    """
    The session's viewer list changed — full state, not a delta.

    Emitted on ``GET /v1/sessions/{id}/stream`` whenever a user
    joins, leaves (after the server-side grace window absorbs
    reconnect churn), or flips their idle aggregate, and once to
    each newly-connected stream as a snapshot-on-connect. Every
    event carries the COMPLETE viewer list so clients replace their
    state wholesale — missed events self-heal on the next event or
    reconnect. Viewers are scoped to the session *tree* (the root
    conversation and every sub-agent conversation under it), so a
    user on a sub-agent page and a user on the root page appear in
    each other's lists. See ``omnigent/server/presence.py`` and
    ``designs/UI/PRESENCE.md``.

    :param type: Always ``"session.presence"``.
    :param conversation_id: The conversation whose stream delivered
        this event — the root or a sub-agent conversation, e.g.
        ``"conv_abc123"``. Matches the streamed conversation (not
        necessarily the tree's root) so clients can guard events by
        the conversation they are viewing.
    :param viewers: All users currently viewing any conversation in
        the session tree (including the receiving user — the web
        filters self out for display), ordered by join time.
    """

    type: Literal["session.presence"]
    conversation_id: str
    viewers: list[PresenceViewer]


class ElicitationRequestParams(BaseModel):
    """
    Inner ``params`` block of a :class:`ElicitationRequestEvent`.

    The standard fields (``mode``, ``message``, ``requestedSchema``,
    ``url``) mirror MCP's ``ElicitRequestFormParams`` /
    ``ElicitRequestUrlParams`` byte-for-byte (Principle 8 — adopt
    MCP's wire shape verbatim where it overlaps). The
    AP-specific extensions (``phase``, ``policy_name``,
    ``content_preview``, ``target_session_id``) carry policy-engine
    context and mirrored-child routing for the consumer's renderer;
    MCP's ``extra="allow"`` config permits them under the same params
    block. Wire shape matches
    ``omnigent/runtime/policies/approval.py:175``.

    :param mode: MCP-standard discriminator. ``"form"`` collects
        structured input via ``requestedSchema``; ``"url"``
        directs upstream to an external URL for OAuth /
        out-of-band interaction.
    :param message: Human-readable prompt the consumer renders,
        e.g. ``"Approve running 'rm -rf /tmp/cache'?"``.
    :param requestedSchema: JSON-Schema dict for form mode (or
        ``None`` for url mode). camelCase preserved per MCP
        spec, e.g.
        ``{"type": "object", "properties": {"approve":
        {"type": "boolean"}}}``.
    :param url: External URL for url mode (or ``None`` for form
        mode), e.g. ``"https://oauth.example.com/authorize?..."``.
    :param phase: Omnigent policy-engine phase the elicitation
        belongs to, e.g. ``"pre_tool_use"``.
    :param policy_name: Omnigent policy that triggered the
        elicitation, e.g. ``"approve_shell_commands"``.
    :param content_preview: Truncated preview of the underlying
        request payload (≤1024 chars in current AP), for the
        consumer's renderer.
    :param target_session_id: AP session whose resolve endpoint owns
        this elicitation, e.g. ``"conv_child123"``. Present when a
        child/sub-agent prompt is mirrored into an ancestor stream;
        ``None`` means resolve against the current session.
    """

    mode: Literal["form", "url"] = "form"
    message: str
    requestedSchema: dict[str, Any] | None = None
    url: str | None = None
    # AP-specific extensions — allowed under MCP's
    # ``extra="allow"`` policy on the inner params object. Strict
    # MCP clients ignore unknown fields here.
    phase: str | None = None
    policy_name: str | None = None
    content_preview: str | None = None
    target_session_id: str | None = None

    # MCP's ElicitRequestParams uses ``extra="allow"``; mirror
    # that here so MCP-shaped passthrough (an MCP server's
    # ``elicitation/create`` traversing harness → Omnigent → client)
    # preserves any fields the MCP server added.
    model_config = ConfigDict(extra="allow")


class ElicitationRequestEvent(_SSEEventBase):
    """
    Synchronous request for a decision from upstream.

    Emitted by Omnigent (or, under the new contract, by a harness)
    when the LLM / a tool / a policy needs a verdict before
    proceeding. The consumer replies via
    ``POST /v1/sessions/{session_id}/events`` with
    ``type == "approval"`` and
    :class:`omnigent.server.schemas.ElicitationResult` fields in
    ``data``. This preserves MCP request/reply correlation by id
    without threading elicitations through PATCH.

    Wire shape matches the existing emit at
    ``omnigent/runtime/policies/approval.py:175``.

    :param type: Always ``"response.elicitation_request"``.
    :param elicitation_id: Unique correlation id for this
        request — appears in the consumer's approval event payload,
        e.g. ``"elicit_abc123"``.
    :param method: MCP method literal — always
        ``"elicitation/create"`` (the value of
        ``_MCP_ELICITATION_METHOD`` in
        ``omnigent/runtime/policies/approval.py``).
    :param params: The MCP-shaped params block carrying the
        prompt and (form-mode only) the requested schema.
    """

    type: Literal["response.elicitation_request"]
    elicitation_id: str
    # MCP method constant — kept as Literal so the discriminator
    # accepts only the MCP-standard value; harnesses that emit a
    # different method literal will fail validation loudly.
    method: Literal["elicitation/create"] = "elicitation/create"
    params: ElicitationRequestParams


class ElicitationResolvedEvent(_SSEEventBase):
    """
    Signal that a previously-published elicitation is no longer
    outstanding, even though no UI ``approval`` verdict was
    delivered through ``POST /v1/sessions/{id}/events``.

    Emitted by the runner when its own ``_pending_approvals``
    Future is popped without a verdict (the runner's wait timed
    out, the turn was cancelled, the harness exited) so the AP
    server's :mod:`omnigent.runtime.pending_elicitations`
    index can decrement the sidebar badge in lockstep with the
    underlying awaiter's lifecycle. Without this signal, the AP
    server has no way to learn that the prompt is dead and the
    badge stays stuck.

    Idempotent on the consumer side: the Omnigent server's index
    decrement is a no-op when the id isn't tracked, so the
    runner can fire-and-forget on every Future cleanup.

    :param type: Always ``"response.elicitation_resolved"``.
    :param elicitation_id: Correlation id of the elicitation
        being cleared, e.g. ``"elicit_abc123"``. Must match the
        id of a prior :class:`ElicitationRequestEvent`.
    """

    type: Literal["response.elicitation_resolved"]
    elicitation_id: str


class CreatedEvent(_SSEEventBase):
    """
    Initial event emitted at the start of every streaming response.

    Carries the freshly-allocated
    :class:`omnigent.server.schemas.ResponseObject` (status will
    be ``"queued"`` or ``"in_progress"`` depending on whether the
    task started immediately).

    :param type: Always ``"response.created"``.
    :param response: The newly-allocated response object.
    """

    type: Literal["response.created"]
    response: ResponseObject


class QueuedEvent(_SSEEventBase):
    """
    Optional event emitted between ``created`` and ``in_progress``
    for background tasks that are queued before they start.

    Foreground streaming responses skip this event.

    :param type: Always ``"response.queued"``.
    :param response: The response object with
        ``status="queued"``.
    """

    type: Literal["response.queued"]
    response: ResponseObject


class InProgressEvent(_SSEEventBase):
    """
    Event emitted once the task transitions to in-progress.

    Always follows ``response.created`` (and ``response.queued``
    for background tasks).

    :param type: Always ``"response.in_progress"``.
    :param response: The response object with
        ``status="in_progress"``.
    """

    type: Literal["response.in_progress"]
    response: ResponseObject


class CompletedEvent(_SSEEventBase):
    """
    Terminal event for a successfully completed turn.

    Carries the final
    :class:`omnigent.server.schemas.ResponseObject`.

    :param type: Always ``"response.completed"``.
    :param response: The final response object with
        ``status="completed"``.
    """

    type: Literal["response.completed"]
    response: ResponseObject


class FailedEvent(_SSEEventBase):
    """
    Terminal event for a turn that ended with an error.

    Carries the final
    :class:`omnigent.server.schemas.ResponseObject` whose
    ``error`` field describes the failure.

    :param type: Always ``"response.failed"``.
    :param response: The final response object with
        ``status="failed"`` and ``error`` populated.
    """

    type: Literal["response.failed"]
    response: ResponseObject


class CancelledEvent(_SSEEventBase):
    """
    Terminal event for a turn cancelled before completion.

    :param type: Always ``"response.cancelled"``.
    :param response: The final response object with
        ``status="cancelled"``.
    """

    type: Literal["response.cancelled"]
    response: ResponseObject


class IncompleteEvent(_SSEEventBase):
    """
    Terminal event for a turn that ended without completing
    (e.g. hit the iteration cap or token budget).

    :param type: Always ``"response.incomplete"``.
    :param response: The final response object with
        ``status="incomplete"`` and ``incomplete_details``
        populated describing the reason.
    """

    type: Literal["response.incomplete"]
    response: ResponseObject


class RetryErrorDetail(BaseModel):
    """
    Error block carried by :class:`RetryEvent` and :class:`ErrorEvent`.

    Mirrors the shape that ``llm_retry.py`` and ``tool_retry.py``
    emit today — flat ``code`` / ``message`` plus an optional
    ``detail`` for provider-specific structured fields.

    :param code: Stable error classifier, e.g. ``"timeout"``,
        ``"rate_limit"``.
    :param message: Human-readable summary, e.g.
        ``"Connection timed out after 30s"``.
    :param detail: Optional provider-specific structured fields
        (e.g. ``{"status_code": 429, "retry_after": 5}``);
        ``None`` when the classifier had no extra context.
    """

    code: str
    message: str
    detail: dict[str, Any] | None = None

    model_config = ConfigDict(extra="ignore")


class RetryEvent(_SSEEventBase):
    """
    A retryable failure was caught and a retry is scheduled.

    Emitted by ``omnigent/runtime/llm_retry.py`` (LLM calls)
    and ``omnigent/runtime/tool_retry.py`` (tool calls) before
    sleeping for the backoff delay. Wire shape matches
    ``llm_retry.py:329-340`` and ``tool_retry.py:168-180``.

    :param type: Always ``"response.retry"``.
    :param source: Origin of the retried failure — ``"llm"`` for
        LLM-call retries, ``"tool"`` for tool-call retries.
    :param tool_name: Tool identifier when ``source == "tool"``,
        e.g. ``"search.web"``. ``None`` for LLM retries.
    :param attempt: 1-based count of the upcoming attempt
        (i.e. attempt that will run AFTER this delay), e.g.
        ``2`` for the first retry.
    :param max_attempts: Total tries allowed by the retry policy,
        e.g. ``3``. Lets clients render "attempt 2 of 3".
    :param delay_seconds: Seconds the producer will sleep before
        retrying, rounded to two decimals, e.g. ``1.5``.
    :param error: Classified error description for the failure
        being retried.
    """

    type: Literal["response.retry"]
    source: Literal["llm", "tool"]
    tool_name: str | None = None
    attempt: int
    max_attempts: int
    delay_seconds: float
    error: RetryErrorDetail


class ErrorEvent(_SSEEventBase):
    """
    Non-recoverable error reported during the turn.

    Emitted from multiple sites in
    ``omnigent/runtime/workflow.py`` — terminal LLM failures
    (``_emit_llm_error_event``), execution timeouts
    (``_handle_execution_timeout``), and the agent-loop catch-all
    (``except Exception``). Wire shape matches those emits.

    :param type: Always ``"response.error"``.
    :param source: Origin of the error — ``"llm"`` for LLM-call
        failures, ``"execution"`` for timeouts, ``"tool"`` for
        tool failures (currently emitted by retry exhaustion paths).
    :param tool_name: Tool identifier when ``source == "tool"``;
        ``None`` for the other sources.
    :param error: Classified error description.
    """

    type: Literal["response.error"]
    source: Literal["llm", "execution", "tool"]
    tool_name: str | None = None
    error: RetryErrorDetail


class CompactionInProgressEvent(_SSEEventBase):
    """
    Conversation history is being compacted.

    Emitted by ``omnigent/runtime/compaction.py`` while a
    compaction step runs so clients can render a "summarizing
    history…" indicator. Wire shape matches ``compaction.py:765``.

    :param type: Always ``"response.compaction.in_progress"``.
    """

    type: Literal["response.compaction.in_progress"]


class CompactionCompletedEvent(_SSEEventBase):
    """
    Conversation history compaction has finished.

    Emitted by ``omnigent/server/routes/sessions.py`` after
    ``compact_conversation_now()`` returns successfully. Clients
    that rendered a "Compacting…" spinner on
    :class:`CompactionInProgressEvent` should upgrade it to the
    permanent "Conversation compacted" marker on this event.

    :param type: Always ``"response.compaction.completed"``.
    :param total_tokens: Tiktoken estimate of the post-compaction
        message context size, e.g. ``8421``. Used by clients to
        update the context-ring immediately without waiting for the
        next ``response.completed`` usage report. ``None`` when
        token counting is unavailable.
    """

    type: Literal["response.compaction.completed"]
    total_tokens: int | None = None


class CompactionFailedEvent(_SSEEventBase):
    """
    Conversation history compaction failed.

    Emitted by ``omnigent/server/routes/sessions.py`` when
    ``compact_conversation_now()`` raises. Clients that rendered a
    "Compacting…" spinner on :class:`CompactionInProgressEvent`
    should dismiss it without leaving a permanent marker, since the
    conversation history was not modified.

    :param type: Always ``"response.compaction.failed"``.
    """

    type: Literal["response.compaction.failed"]


class ClientTaskCancelEvent(_SSEEventBase):
    """
    Server-side request that the client cancel a tunneled tool call.

    Emitted by ``omnigent/runtime/workflow.py`` when a parent
    cancellation needs to propagate to a long-running async client
    tool. Wire shape matches ``workflow.py:4258-4266``.

    :param type: Always ``"response.client_task.cancel"``.
    :param task_id: Identifier of the client-side task being
        cancelled, e.g. ``"resp_async_abc"``.
    :param call_id: Synthetic ``call_id`` the SDK uses to
        reconcile the local task; ``None`` when no pending tool
        call row exists for the task.
    """

    type: Literal["response.client_task.cancel"]
    task_id: str
    call_id: str | None = None


# ── Session resource lifecycle events (Phase 1d) ─────────────────────


class SessionResourceCreatedEvent(_SSEEventBase):
    """
    A session resource was created.

    Emitted when a terminal is launched, a file is uploaded, or
    any other resource is materialized under a session. Wire shape
    is FLAT: ``{"type": "session.resource.created",
    "resource": <SessionResourceObject-like dict>}``.

    :param type: Always ``"session.resource.created"``.
    :param resource: The newly created resource object.
    """

    type: Literal["session.resource.created"]
    resource: dict[str, Any]


class SessionResourceDeletedEvent(_SSEEventBase):
    """
    A session resource was deleted.

    Emitted when a terminal is closed, a file is deleted, or
    any other resource is removed from a session.

    :param type: Always ``"session.resource.deleted"``.
    :param resource_id: Opaque id of the deleted resource.
    :param resource_type: Type of the deleted resource,
        e.g. ``"terminal"``, ``"file"``.
    :param session_id: Owning session/conversation id.
    """

    type: Literal["session.resource.deleted"]
    resource_id: str
    resource_type: str
    session_id: str


class SessionChildSessionUpdatedEvent(_SSEEventBase):
    """
    A child (sub-agent) session's status changed — pushed to the PARENT.

    Lets the parent's resource rail update a child's status without
    polling ``GET …/child_sessions``. Carries the full
    :class:`ChildSessionSummary` so the web patches its cache directly.

    :param type: Always ``"session.child_session.updated"``.
    :param conversation_id: The PARENT (carrier) session id.
    :param child_session_id: The child session id, e.g.
        ``"conv_child_abc123"``.
    :param child: A PARTIAL :class:`ChildSessionSummary` — the
        snapshot-on-connect sends the full summary, while live runner
        deltas carry only the fields that changed (a status delta omits
        ``last_message_preview``; a preview delta carries only it). The
        web merges present fields over the cached row, so the payload is
        an open dict rather than the strict model.
    """

    type: Literal["session.child_session.updated"]
    conversation_id: str
    child_session_id: str
    child: dict[str, Any]


class SessionChangedFilesInvalidatedEvent(_SSEEventBase):
    """
    The session's changed-files list may have changed — refetch it.

    A coarse "something changed" signal (per-file events aren't available
    for git-mode workspaces) emitted by the runner after a file-mutating
    tool. The web treats it as a refetch trigger for the changed-files
    panel; transient (not persisted — the REST list is source of truth).

    :param type: Always ``"session.changed_files.invalidated"``.
    :param session_id: Owning session/conversation id.
    :param environment_id: Environment whose changes were invalidated,
        e.g. ``"default"``.
    """

    type: Literal["session.changed_files.invalidated"]
    session_id: str
    # "default" is the canonical primary-environment id
    # (DEFAULT_ENVIRONMENT_ID); the changed-files panel only tracks that
    # environment, so it's the sole expected value, not an invented one.
    environment_id: str = "default"


class SessionTerminalActivityEvent(_SSEEventBase):
    """
    A terminal's pane produced output (runner-determined activity pulse).

    Powers the web "active" badge for any terminal without a client PTY
    attach — the runner's per-terminal pane watcher emits this when the
    pane content changes. Transient (a live pulse; not persisted, not in
    the connect snapshot).

    :param type: Always ``"session.terminal.activity"``.
    :param session_id: Owning session/conversation id.
    :param terminal_id: Opaque terminal resource id, e.g.
        ``"terminal_zsh_s1"``.
    """

    type: Literal["session.terminal.activity"]
    session_id: str
    terminal_id: str


class TurnStartedEvent(_SSEEventBase):
    """
    Emitted when the runner starts a new turn for a session.

    :param type: Fixed literal ``"turn.started"``.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    """

    type: Literal["turn.started"]
    session_id: str


class TurnCompletedEvent(_SSEEventBase):
    """
    Emitted when a turn finishes successfully with no pending work.

    :param type: Fixed literal ``"turn.completed"``.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    """

    type: Literal["turn.completed"]
    session_id: str


class TurnFailedEvent(_SSEEventBase):
    """
    Emitted when a turn fails due to an LLM error, timeout, or crash.

    :param type: Fixed literal ``"turn.failed"``.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param error: Error details, e.g.
        ``{"message": "LLM timeout", "type": "TimeoutError"}``.
    """

    type: Literal["turn.failed"]
    session_id: str
    error: dict[str, Any] = Field(default_factory=dict)


class TurnCancelledEvent(_SSEEventBase):
    """
    Emitted when a turn is interrupted by the user or system.

    :param type: Fixed literal ``"turn.cancelled"``.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    """

    type: Literal["turn.cancelled"]
    session_id: str


# ── Discriminated union ─────────────────────────────────────────────


# ServerStreamEvent: every event a stream consumer (AP-as-harness-
# client OR an external client of AP) may receive on either of
# AP's two SSE endpoints. Pydantic dispatches on the ``type``
# field via ``Field(discriminator="type")``; each variant's
# ``Literal[...]`` pins the correct branch.
#
# Usage:
#     from pydantic import TypeAdapter
#     from omnigent.server.schemas import ServerStreamEvent
#     adapter = TypeAdapter(ServerStreamEvent)
#     event = adapter.validate_python(raw_dict)
#     # ``event`` is now the concrete typed model.
#
# Renamed from ``ResponseStreamEvent`` to disambiguate from the
# OpenAI SDK's identically-named type used inside
# ``omnigent.llms.types``.
ServerStreamEvent = Annotated[
    # ── Transient (SSE-only) — session.* lifecycle ─────────────
    SessionStatusEvent
    | SessionUsageEvent
    | SessionModelEvent
    | SessionAgentChangedEvent
    | SessionTodosEvent
    | SessionTerminalPendingEvent
    | SessionSandboxStatusEvent
    | SessionSkillsEvent
    | SessionInputConsumedEvent
    | SessionInterruptedEvent
    | SessionCreatedEvent
    | SessionPresenceEvent
    # ── Transient (SSE-only) — session resource lifecycle ─────
    | SessionResourceCreatedEvent
    | SessionResourceDeletedEvent
    | SessionChildSessionUpdatedEvent
    | SessionChangedFilesInvalidatedEvent
    | SessionTerminalActivityEvent
    # ── Transient (SSE-only) — incremental token deltas ────────
    | OutputTextDeltaEvent
    | ReasoningStartedEvent
    | ReasoningTextDeltaEvent
    | ReasoningSummaryTextDeltaEvent
    # ── Persistent (POST + SSE replay) — wraps conv-store items
    | OutputItemDoneEvent
    # ── Transient (SSE-only) — file annotations / keepalive ────
    | OutputFileDoneEvent
    | HeartbeatEvent
    | SessionHeartbeatEvent
    # ── Transient (SSE-only) — synchronous decision request ────
    | ElicitationRequestEvent
    | ElicitationResolvedEvent
    # ── Transient (SSE-only) — Responses-API turn lifecycle ────
    | CreatedEvent
    | QueuedEvent
    | InProgressEvent
    | CompletedEvent
    | FailedEvent
    | CancelledEvent
    | IncompleteEvent
    # ── Transient (SSE-only) — operational signals ─────────────
    | RetryEvent
    | ErrorEvent
    | CompactionInProgressEvent
    | CompactionCompletedEvent
    | CompactionFailedEvent
    | ClientTaskCancelEvent
    | TurnStartedEvent
    | TurnCompletedEvent
    | TurnFailedEvent
    | TurnCancelledEvent,
    Field(discriminator="type"),
]


# Frozen set of every wire ``type`` literal across the union.
# Derived from :data:`ServerStreamEvent` so adding a new event variant
# to the union automatically updates the drift-detection set — there
# is no second list to keep in sync.
#
# ``ServerStreamEvent`` is ``Annotated[A | B | ..., Field(...)]``;
# ``get_args`` returns ``(A | B | ..., FieldInfo)``. The first element
# is the union, whose own ``get_args`` yields the variant classes.
_KNOWN_EVENT_TYPES: frozenset[str] = frozenset(
    # ``model_fields["type"].annotation`` is the ``Literal[...]``
    # carried by each variant; ``.__args__[0]`` extracts the
    # single string literal.
    cls.model_fields["type"].annotation.__args__[0]
    for cls in get_args(get_args(ServerStreamEvent)[0])
)


def is_known_event(name: str) -> bool:
    """
    Return whether ``name`` is a wire ``type`` literal in the union.

    Used by the drift-detection test
    (``tests/server/test_stream_events.py``): integration tests
    patch :func:`omnigent.runtime.session_stream.publish` to
    call this on every emitted ``event["type"]``; any string not
    in the union fails the test, catching new emissions that
    bypassed the source of truth.

    :param name: Candidate wire name to check, e.g.
        ``"response.output_text.delta"``.
    :returns: ``True`` if ``name`` is the ``type`` literal of a
        :data:`ServerStreamEvent` variant, ``False`` otherwise.
    """
    return name in _KNOWN_EVENT_TYPES


# Events the harness may emit on its per-turn SSE stream that are
# runner-internal: the runner intercepts and consumes them (matching by
# ``type`` on the raw frame, see ``omnigent/runner/app.py`` proxy_stream)
# and never relays them to clients. They are deliberately NOT part of the
# public :data:`ServerStreamEvent` union / openapi. This alias types the
# scaffold's per-turn event queue, which carries both the public events and
# these internal markers. See ``designs/RUNNER_MESSAGE_INGEST.md`` Part B.


class PolicyEvaluationRequestEvent(_SSEEventBase):
    """
    Runner-internal marker: harness requests policy evaluation.

    Emitted by the executor adapter before or after an LLM call so
    the runner can evaluate ``LLM_REQUEST`` / ``LLM_RESPONSE``
    policies on the Omnigent server. The runner intercepts this event in
    ``proxy_stream``, calls the Omnigent server's
    ``POST /sessions/{id}/policies/evaluate`` endpoint, and posts
    the verdict back to the harness as a ``policy_verdict`` inbound
    event. This event is **never** relayed to external clients —
    it is purely a runner↔harness handshake.

    :param type: Always ``"policy_evaluation.requested"``.
    :param evaluation_id: Unique correlation id for this
        evaluation, e.g. ``"poleval_abc123"``. The runner echoes
        it back in the ``policy_verdict`` inbound event so the
        scaffold can resolve the correct parked Future.
    :param phase: Proto-style phase string, e.g.
        ``"PHASE_LLM_REQUEST"`` or ``"PHASE_LLM_RESPONSE"``.
    :param data: Event data dict passed to the Omnigent server's
        policy evaluate endpoint, e.g.
        ``{"model": "gpt-4o", "messages_count": 42}``.
    """

    type: Literal["policy_evaluation.requested"]
    evaluation_id: str
    phase: str
    data: dict[str, Any]


HarnessStreamEvent = ServerStreamEvent | InjectionConsumedEvent | PolicyEvaluationRequestEvent
