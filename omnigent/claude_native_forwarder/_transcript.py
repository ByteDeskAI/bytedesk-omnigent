"""Background transcript forwarding for native Claude Code sessions."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from omnigent._native_post_delivery import post_may_have_been_delivered
from omnigent.claude_native_bridge import (
    BRIDGE_ID_LABEL_KEY,
    ClaudeHookRecord,
    ClaudeMessageDelta,
    ClaudeTranscriptItem,
    HookReadResult,
    TranscriptReadResult,
    compute_transcript_cumulative_cost,
    read_active_session_id,
    read_bridge_id,
    read_claude_context_state,
    read_claude_session_id,
    read_hook_events_from_offset,
    read_hook_events_since_with_position,
    read_message_deltas_from_offset,
    read_transcript_items_from_offset,
    read_transcript_items_since_with_position,
    read_transcript_path,
    transcript_has_forked_from_marker,
    transcript_has_recent_local_command,
    url_component,
    write_active_session_id,
)
from omnigent.claude_native_message_display_hook import MESSAGE_DELTAS_FILE
from omnigent.entities.session_resources import terminal_resource_id
from omnigent.reasoning_effort import CLAUDE_EFFORTS, EFFORT_CLEAR_VALUES

_FORWARDER_STATE_FILE = "transcript_forwarder.json"
_HOOK_STATE_FILE = "hook_forwarder.json"
_SUBAGENT_STATE_FILE = "subagent_forwarder.json"
_DELTA_STATE_FILE = "message_deltas_forwarder.json"
_HOOKS_FILE = "hooks.jsonl"

# Cap on the in-memory ``(message_id, index)`` dedupe ring for streamed
# deltas. The byte offset already prevents re-reading on the normal
# path; this guards the rare truncation/rewind case where the deltas
# file is reset and the reader restarts from 0. Generous because one
# prose answer can be hundreds of chunks.
_MAX_SEEN_DELTA_KEYS = 5000

# Seconds of transcript inactivity after which we publish ``idle`` for
# a sub-agent. The transcript is the only signal we have for sub-agent
# completion in Phase A (no SubagentStop hook is subscribed); 5s is the
# shortest window that comfortably absorbs a stalled tool call without
# flickering the badge. Phase B will replace this with an authoritative
# hook signal and drop the heuristic.
_SUBAGENT_IDLE_QUIESCENCE_S = 5.0

# Meta-file glob inside ``~/.claude/projects/<encoded>/<session>/subagents/``.
# One per Claude Task-tool subagent; appears alongside the matching
# ``agent-<id>.jsonl`` transcript.
_SUBAGENT_META_GLOB = "agent-*.meta.json"
_DEFAULT_POLL_INTERVAL_S = 0.25
_POST_TIMEOUT_S = 10.0
_MAX_SEEN_SOURCE_IDS = 2000
_CURSOR_FINGERPRINT_BYTES = 256
_FORK_COMMAND_NAMES = frozenset({"/branch", "/fork"})
_HTTP_POST_MAX_PERMANENT_FAILURES = 3
_HTTP_POST_RETRY_BASE_DELAY_S = 1.0
_HTTP_POST_RETRY_MAX_DELAY_S = 30.0
_HTTP_TRANSIENT_STATUS_CODES = {408, 409, 425, 429}
_SUPERVISOR_INITIAL_BACKOFF_S = 1.0
_SUPERVISOR_MAX_BACKOFF_S = 30.0
_SUPERVISOR_HEALTHY_UPTIME_S = 60.0

# Claude Code hook event names → Omnigent session-status values
# published on the per-conversation SSE stream. Unmapped events emit
# no status.
#
# ``Stop`` → idle and ``StopFailure`` → failed are the authoritative
# turn-end edges (each fires once when Claude finishes / errors a turn);
# they drive sub-agent terminal delivery via the codex-shared
# ``external_session_status`` path (→ parent inbox + wake). The
# PTY-activity ``idle`` cannot: it is a ~1s-quiescence heuristic that
# oscillates on every mid-turn lull, so delivering on it fired a
# premature completion and idempotently locked out the real one.
# ``UserPromptSubmit`` → running stays PTY-derived — the pane watcher
# drives the UI running/idle badge and catches what ``Stop`` misses
# (interrupts, compaction failures, TUI edits). ``_publish_status``
# keeps ``failed`` sticky against the trailing PTY idle.
_HOOK_EVENT_TO_STATUS: dict[str, str] = {
    "Stop": "idle",
    "StopFailure": "failed",
}

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

async def forward_claude_transcript_to_session(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    agent_name: str,
    start_at_end: bool,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    auth: httpx.Auth | None = None,
    skip_user_messages: bool = False,
) -> None:
    """
    Tail Claude's JSONL transcript and mirror semantic items into AP.

    This loop is intentionally independent of Claude Channels. It
    runs while the native terminal is attached, watches the transcript
    path reported by Claude hooks, and posts new user text,
    assistant text, tool calls, and tool results as external AP
    conversation items.

    :param base_url: Omnigent server base URL.
    :param headers: Static HTTP headers for Omnigent requests. Authorization
        is normally supplied via ``auth`` instead so OAuth tokens are
        refreshed per request; any ``Authorization`` value here is
        overridden by ``auth`` when both are set.
    :param session_id: Omnigent session/conversation id.
    :param bridge_dir: Native Claude bridge directory.
    :param agent_name: Agent/model name to stamp on mirrored output.
    :param start_at_end: When ``True`` and no prior forward cursor
        exists, start from the current transcript end. This is used
        for reattach so old transcript lines are not duplicated.
    :param poll_interval_s: Seconds between transcript polls.
    :param auth: Optional httpx Auth that mints a fresh bearer token
        per request, e.g. ``_server_auth(profile)`` for a Databricks
        Apps deployment. ``None`` for local servers that don't need
        auth. Required for long-lived remote sessions — Databricks
        OAuth tokens expire after ~1 hour and a static header captured
        at startup would stop authenticating mid-session.
    :returns: Never normally returns; cancel the task to stop it.
    """
    state = _read_forward_state(bridge_dir)
    hook_state: HookForwardState | None = None
    subagent_state = _read_subagent_forward_state(bridge_dir)
    # Live assistant-text streaming. The delta cursor is independent of
    # the transcript/subagent cursors and survives /clear and /fork
    # (the deltas file belongs to the long-lived Claude process). The
    # dedupe ring is per-process and not persisted: the byte offset
    # prevents re-reads on the normal path.
    delta_state = _read_delta_forward_state(bridge_dir)
    seen_delta_keys: dict[tuple[str, int], None] = {}
    item_retries = _PostRetryTracker()
    status_retries = _PostRetryTracker()
    subagent_start_retries = _PostRetryTracker()
    subagent_item_retries = _PostRetryTracker()
    subagent_status_retries = _PostRetryTracker()
    # Dedupe: Claude rewrites the same usage block every poll until
    # the next assistant entry; only POST on real change. Mutated in
    # place by ``_forward_available_items`` and carried across polls.
    dedupe = _ForwardDedupeState()
    # Size-keyed transcript cost cache for ``_forward_session_cost`` — keeps
    # the per-poll cost reconciliation from re-parsing unchanged transcripts.
    # Reset on /clear and /fork rotations alongside ``dedupe``.
    cost_cache: dict[Path, _TranscriptCostCacheEntry] = {}
    # Per-process latch: once we PATCH the conversation with the
    # Claude-native session id, never PATCH again. Persists for the
    # lifetime of the forwarder task; the server's idempotence handles
    # the rare case where two forwarder processes race the same conv.
    external_session_id_mirrored = False
    # Native task system state: maps and ordered list accumulated from
    # TaskCreated / TaskCompleted / PostToolUse/TaskUpdate hook events.
    # Reset on /clear and /fork rotations alongside other session state.
    task_subjects: dict[str, str] = {}
    task_statuses: dict[str, str] = {}
    task_order: list[str] = []
    timeout = httpx.Timeout(_POST_TIMEOUT_S)
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, auth=auth, timeout=timeout
    ) as client:
        while True:
            try:
                current_session_id = read_active_session_id(bridge_dir) or session_id
                if hook_state is None:
                    hook_state = await _ensure_hook_state(
                        bridge_dir,
                        start_at_end=start_at_end,
                        session_id=current_session_id,
                    )
                rotation = await _maybe_rotate_session_on_clear(
                    client=client,
                    session_id=current_session_id,
                    bridge_dir=bridge_dir,
                    state=hook_state,
                )
                if rotation is not None:
                    session_id = rotation
                    state = None
                    hook_state = None
                    # After a /clear or /fork the parent now resolves
                    # to a new ``<session_uuid>/subagents/`` directory
                    # on disk, so old sub-agent entries are dead. Drop
                    # them; the watcher will rediscover any new ones
                    # under the rotated session's dir.
                    subagent_state = SubagentForwardState(subagents={})
                    await _write_subagent_forward_state_async(bridge_dir, subagent_state)
                    item_retries = _PostRetryTracker()
                    status_retries = _PostRetryTracker()
                    subagent_start_retries = _PostRetryTracker()
                    subagent_item_retries = _PostRetryTracker()
                    subagent_status_retries = _PostRetryTracker()
                    external_session_id_mirrored = False
                    task_subjects = {}
                    task_statuses = {}
                    task_order = []
                    # A rotated session is a fresh dedupe context — reseed
                    # so the new session's first model observation doesn't
                    # post against the prior session's baseline.
                    dedupe = _ForwardDedupeState()
                    # The rotated session resolves to a new transcript +
                    # subagents/ dir, so prior cost entries are dead; drop
                    # them so cost is recomputed fresh for the new session.
                    cost_cache = {}
                    await asyncio.sleep(poll_interval_s)
                    continue
                rotation = await _maybe_rotate_session_on_fork(
                    client=client,
                    session_id=current_session_id,
                    bridge_dir=bridge_dir,
                    state=hook_state,
                )
                if rotation is not None:
                    session_id = rotation
                    state = None
                    hook_state = None
                    # After a /clear or /fork the parent now resolves
                    # to a new ``<session_uuid>/subagents/`` directory
                    # on disk, so old sub-agent entries are dead. Drop
                    # them; the watcher will rediscover any new ones
                    # under the rotated session's dir.
                    subagent_state = SubagentForwardState(subagents={})
                    await _write_subagent_forward_state_async(bridge_dir, subagent_state)
                    item_retries = _PostRetryTracker()
                    status_retries = _PostRetryTracker()
                    subagent_start_retries = _PostRetryTracker()
                    subagent_item_retries = _PostRetryTracker()
                    subagent_status_retries = _PostRetryTracker()
                    external_session_id_mirrored = False
                    task_subjects = {}
                    task_statuses = {}
                    task_order = []
                    # A rotated session is a fresh dedupe context — reseed
                    # so the new session's first model observation doesn't
                    # post against the prior session's baseline.
                    dedupe = _ForwardDedupeState()
                    # The rotated session resolves to a new transcript +
                    # subagents/ dir, so prior cost entries are dead; drop
                    # them so cost is recomputed fresh for the new session.
                    cost_cache = {}
                    await asyncio.sleep(poll_interval_s)
                    continue
                if not external_session_id_mirrored:
                    external_session_id_mirrored = await _maybe_mirror_external_session_id(
                        client=client,
                        session_id=current_session_id,
                        bridge_dir=bridge_dir,
                    )
                transcript_path = read_transcript_path(bridge_dir)
                if transcript_path is not None:
                    state = await _ensure_state_for_transcript(
                        bridge_dir=bridge_dir,
                        state=state,
                        transcript_path=transcript_path,
                        start_at_end=start_at_end,
                        session_id=current_session_id,
                    )
                    # Forward streamed deltas BEFORE the transcript items so a
                    # message's live chunks (incl. its ``final`` chunk) always
                    # precede its own authoritative ``output_item.done``. If
                    # items led, a message's final chunk — written to the
                    # deltas file moments before the transcript record flushed
                    # — would land just AFTER its done event and re-create the
                    # already-finalized preview on the client (duplicate bubble
                    # + a stale trailing preview). See the web reconciler.
                    delta_state = await _forward_available_deltas(
                        client=client,
                        session_id=current_session_id,
                        bridge_dir=bridge_dir,
                        state=delta_state,
                        seen_keys=seen_delta_keys,
                    )
                    state = await _forward_available_items(
                        client=client,
                        session_id=current_session_id,
                        bridge_dir=bridge_dir,
                        agent_name=agent_name,
                        state=state,
                        retry_tracker=item_retries,
                        skip_user_messages=skip_user_messages,
                        dedupe=dedupe,
                    )
                    hook_state = await _forward_available_status_events(
                        client=client,
                        session_id=current_session_id,
                        bridge_dir=bridge_dir,
                        state=hook_state,
                        retry_tracker=status_retries,
                        task_subjects=task_subjects,
                        task_statuses=task_statuses,
                        task_order=task_order,
                    )
                    subagent_state = await _forward_available_subagents(
                        client=client,
                        parent_session_id=current_session_id,
                        bridge_dir=bridge_dir,
                        transcript_path=transcript_path,
                        state=subagent_state,
                        agent_name=agent_name,
                        start_retry_tracker=subagent_start_retries,
                        item_retry_tracker=subagent_item_retries,
                        status_retry_tracker=subagent_status_retries,
                    )
                    # Reconcile + POST cumulative cost AFTER sub-agents are
                    # forwarded so the estimate sees this poll's sub-agent
                    # transcript growth. This is what lets the parent's
                    # cost-budget policy block a sub-agent's tool calls
                    # mid-turn (the statusLine total alone lags until the
                    # sub-agent finishes).
                    await _forward_session_cost(
                        client=client,
                        session_id=current_session_id,
                        bridge_dir=bridge_dir,
                        parent_transcript_path=transcript_path,
                        subagent_state=subagent_state,
                        dedupe=dedupe,
                        cost_cache=cost_cache,
                    )
                    # Mirror the live statusLine model EVERY poll (not just
                    # when a turn produced new transcript items, which
                    # _forward_available_items early-returns without). This
                    # propagates an in-pane /model switch to model_override
                    # before the user's next message, so model-gated policies
                    # (cost-budget hard cap) no longer lag a switch by one turn.
                    await _forward_model_from_status(
                        client=client,
                        session_id=current_session_id,
                        bridge_dir=bridge_dir,
                        dedupe=dedupe,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "Claude transcript forwarder loop failed; session=%s bridge_dir=%s",
                    session_id,
                    bridge_dir,
                )
            await asyncio.sleep(poll_interval_s)

def reset_transcript_forward_state(bridge_dir: Path, *, reset_hooks: bool = True) -> None:
    """
    Remove the durable transcript-forward cursor for a fresh launch.

    :param bridge_dir: Native Claude bridge directory.
    :param reset_hooks: Whether to also remove the hook cursor. Keep
        ``False`` after consuming a ``/clear`` hook so the same clear
        record is not processed again.
    :returns: None.
    """
    filenames = [
        _FORWARDER_STATE_FILE,
        "transcript_forwarder.pause.json",
    ]
    if reset_hooks:
        filenames.append(_HOOK_STATE_FILE)
    for filename in filenames:
        with contextlib.suppress(FileNotFoundError):
            (bridge_dir / filename).unlink()

async def _ensure_state_for_transcript(
    *,
    bridge_dir: Path,
    state: TranscriptForwardState | None,
    transcript_path: Path,
    start_at_end: bool,
    session_id: str,
) -> TranscriptForwardState:
    """
    Return a cursor state compatible with the observed transcript.

    :param bridge_dir: Native Claude bridge directory.
    :param state: Existing cursor state, or ``None``.
    :param transcript_path: Current transcript path from hooks.
    :param start_at_end: Whether a missing cursor should skip the
        transcript's existing lines.
    :param session_id: Omnigent session/conversation id, e.g.
        ``"conv_abc123"``. Used for stale-cursor diagnostics.
    :returns: Cursor state for ``transcript_path``.
    """
    if state is not None and state.transcript_path == transcript_path:
        validated = _validated_transcript_state(
            state,
            bridge_dir=bridge_dir,
            session_id=session_id,
        )
        if validated != state:
            await _write_forward_state_async(bridge_dir, validated)
        return validated
    disk_state = _read_forward_state(bridge_dir)
    if disk_state is not None and disk_state.transcript_path == transcript_path:
        validated = _validated_transcript_state(
            disk_state,
            bridge_dir=bridge_dir,
            session_id=session_id,
        )
        if validated != disk_state:
            await _write_forward_state_async(bridge_dir, validated)
        return validated
    byte_offset = 0
    if start_at_end:
        byte_offset = await asyncio.to_thread(_transcript_end_offset, transcript_path)
    state = TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=byte_offset,
        cursor_fingerprint=_jsonl_cursor_fingerprint(transcript_path, byte_offset),
    )
    await _write_forward_state_async(bridge_dir, state)
    return state

def _read_transcript_items_for_state(
    state: TranscriptForwardState,
    agent_name: str,
) -> TranscriptReadResult:
    """
    Read transcript items using the best cursor available in ``state``.

    :param state: Current transcript forwarder state.
    :param agent_name: Agent/model name to stamp on mirrored output.
    :returns: Transcript items and updated cursors. States without a
        byte offset are migrated by one line-cursor compatibility scan.
    """
    if state.byte_offset is None:
        return read_transcript_items_since_with_position(
            state.transcript_path,
            state.line_cursor,
            agent_name=agent_name,
            current_response_id=state.current_response_id,
        )
    return read_transcript_items_from_offset(
        state.transcript_path,
        state.byte_offset,
        start_line=state.line_cursor,
        agent_name=agent_name,
        current_response_id=state.current_response_id,
    )

def _validated_transcript_state(
    state: TranscriptForwardState,
    *,
    bridge_dir: Path,
    session_id: str,
) -> TranscriptForwardState:
    """
    Reset a transcript cursor if its byte-offset fingerprint is stale.

    :param state: Transcript cursor loaded from memory or disk.
    :param bridge_dir: Native Claude bridge directory.
    :param session_id: Omnigent session/conversation id, e.g.
        ``"conv_abc123"``. Used for diagnostics.
    :returns: ``state`` unchanged when its byte cursor still matches
        the file; ``state`` with an adopted fingerprint (no reset)
        when the cursor is at byte 0 / line 0 and the file just
        appeared; otherwise a cursor skipped to end-of-file with
        ``seen_source_ids`` preserved so already-forwarded items
        are not re-posted.
    """
    if state.byte_offset is None:
        return state
    current_fingerprint = _jsonl_cursor_fingerprint(state.transcript_path, state.byte_offset)
    if current_fingerprint is None:
        if not state.transcript_path.exists():
            return state
        _logger.warning(
            "Claude transcript cursor invalid; skipping to end of transcript; "
            "session=%s bridge_dir=%s transcript=%s byte_offset=%s",
            session_id,
            bridge_dir,
            state.transcript_path,
            state.byte_offset,
        )
    elif state.cursor_fingerprint is None:
        if state.byte_offset == 0 and state.line_cursor == 0:
            # State was written before the transcript file existed (fingerprint
            # was None because the file was missing). The file now exists and
            # the cursor is still at the start — adopt the computed fingerprint
            # without resetting seen_source_ids.
            return TranscriptForwardState(
                transcript_path=state.transcript_path,
                line_cursor=state.line_cursor,
                byte_offset=state.byte_offset,
                current_response_id=state.current_response_id,
                seen_source_ids=state.seen_source_ids,
                cursor_fingerprint=current_fingerprint,
            )
        _logger.warning(
            "Claude transcript cursor missing fingerprint; skipping to end of transcript; "
            "session=%s bridge_dir=%s transcript=%s byte_offset=%s",
            session_id,
            bridge_dir,
            state.transcript_path,
            state.byte_offset,
        )
    elif current_fingerprint == state.cursor_fingerprint:
        return state
    else:
        _logger.warning(
            "Claude transcript cursor fingerprint changed; skipping to end of transcript; "
            "session=%s bridge_dir=%s transcript=%s byte_offset=%s",
            session_id,
            bridge_dir,
            state.transcript_path,
            state.byte_offset,
        )
    end_offset = _transcript_end_offset(state.transcript_path)
    return TranscriptForwardState(
        transcript_path=state.transcript_path,
        line_cursor=0,
        byte_offset=end_offset,
        cursor_fingerprint=_jsonl_cursor_fingerprint(state.transcript_path, end_offset),
        seen_source_ids=state.seen_source_ids,
    )

async def _post_external_output_text_delta(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    delta: ClaudeMessageDelta,
) -> None:
    """
    Post one streamed assistant-text chunk to the Sessions API.

    Published as a transient ``response.output_text.delta`` SSE event
    (no persistence). ``message_id``/``index``/``final`` let the web UI
    scope an in-flight buffer per message, order chunks, and know when
    the live stream for a message ends; the authoritative final text
    still arrives separately via ``external_conversation_item``.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param delta: Parsed streamed chunk.
    :returns: None.
    :raises httpx.HTTPError: If the Omnigent request fails or is rejected.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_output_text_delta",
            "data": {
                "delta": delta.delta,
                "message_id": delta.message_id,
                "index": delta.index,
                "final": delta.final,
            },
        },
    )
    resp.raise_for_status()

async def _forward_available_deltas(
    *,
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
    state: DeltaForwardState,
    seen_keys: dict[tuple[str, int], None],
) -> DeltaForwardState:
    """
    Forward newly appended assistant-text deltas to the active session.

    Reads complete records appended to ``message_deltas.jsonl`` after
    the current byte offset and publishes each as a transient
    ``external_output_text_delta``. Deltas are best-effort live preview:
    a per-chunk POST failure is logged and dropped (the authoritative
    final message still arrives via ``external_conversation_item``)
    rather than retried, so a transient blip can never wedge the tail.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id deltas are forwarded
        to — the currently active session, so chunks streamed after a
        ``/clear`` land on the rotated session.
    :param bridge_dir: Native Claude bridge directory.
    :param state: Current delta cursor state.
    :param seen_keys: In-memory ``(message_id, index)`` dedupe ring,
        mutated in place. Guards the rare file-truncation rewind where
        the reader restarts from offset ``0``.
    :returns: The updated delta cursor state (offset advanced past the
        records just read).
    """
    # The deltas file only exists once the MessageDisplay hook has fired
    # for this Claude process. Skip the worker-thread read until then so
    # idle / non-streaming polls don't churn the thread pool (this loop
    # polls every ~0.25s). A bare ``exists()`` is a cheap stat consistent
    # with the other sync reads this loop already does each poll.
    if not (bridge_dir / MESSAGE_DELTAS_FILE).exists():
        return state
    result = await asyncio.to_thread(
        read_message_deltas_from_offset, bridge_dir, state.byte_offset
    )
    if result.byte_offset == state.byte_offset and not result.deltas:
        return state
    for delta in result.deltas:
        key = (delta.message_id, delta.index)
        if key in seen_keys:
            continue
        seen_keys[key] = None
        # Bound the dedupe ring by evicting the oldest key (dicts are
        # insertion-ordered) so a very long session can't grow it without
        # limit.
        while len(seen_keys) > _MAX_SEEN_DELTA_KEYS:
            del seen_keys[next(iter(seen_keys))]
        try:
            await _post_external_output_text_delta(client, session_id=session_id, delta=delta)
        except httpx.HTTPError as exc:
            _logger.debug(
                "Dropping Claude streamed delta after HTTP failure; session=%s "
                "bridge_dir=%s message_id=%s index=%s http_status=%s",
                session_id,
                bridge_dir,
                delta.message_id,
                delta.index,
                _http_status_for_log(exc),
            )
    updated = DeltaForwardState(byte_offset=result.byte_offset)
    await _write_delta_forward_state_async(bridge_dir, updated)
    return updated

def _read_delta_forward_state(bridge_dir: Path) -> DeltaForwardState:
    """
    Read the durable delta-forwarder cursor from the bridge directory.

    :param bridge_dir: Native Claude bridge directory.
    :returns: Persisted cursor, or a fresh ``byte_offset=0`` state when
        none exists or it is unusable. Starting from ``0`` re-reads the
        deltas file; the ``(message_id, index)`` dedupe ring and the
        frontend's own provisional buffer absorb any re-sent chunks.
    """
    try:
        raw = json.loads((bridge_dir / _DELTA_STATE_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return DeltaForwardState()
    if not isinstance(raw, dict):
        return DeltaForwardState()
    byte_offset = raw.get("byte_offset")
    if not isinstance(byte_offset, int) or byte_offset < 0:
        return DeltaForwardState()
    return DeltaForwardState(byte_offset=byte_offset)

def _write_delta_forward_state(bridge_dir: Path, state: DeltaForwardState) -> None:
    """
    Write the durable delta-forwarder cursor to the bridge directory.

    :param bridge_dir: Native Claude bridge directory.
    :param state: Cursor state to persist.
    :returns: None.
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_json_atomic(
        bridge_dir / _DELTA_STATE_FILE,
        {"byte_offset": state.byte_offset, "updated_at": time.time()},
    )

async def _write_delta_forward_state_async(
    bridge_dir: Path,
    state: DeltaForwardState,
) -> None:
    """
    Persist delta state without blocking the asyncio event loop.

    :param bridge_dir: Native Claude bridge directory.
    :param state: Cursor state to persist.
    :returns: None.
    """
    await asyncio.to_thread(_write_delta_forward_state, bridge_dir, state)

def _transcript_end_offset(transcript_path: Path) -> int:
    """
    Return the byte offset after the last complete transcript record.

    :param transcript_path: Claude transcript path.
    :returns: Offset after the last newline-terminated record, or
        ``0`` when the transcript does not exist or has only a
        partial first record.
    """
    return _complete_jsonl_end_offset(transcript_path)


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _cost as _sib_cost
    from . import _fwd_state as _sib_fwd_state
    from . import _helpers as _sib_helpers
    from . import _hooks as _sib_hooks
    from . import _subagent as _sib_subagent
    from . import _supervisor as _sib_supervisor
    for _key, _value in _sib_cost.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_fwd_state.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_helpers.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_hooks.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_subagent.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_supervisor.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
