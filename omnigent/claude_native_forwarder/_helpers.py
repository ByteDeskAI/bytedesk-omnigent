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

async def _fetch_session_snapshot(
    client: httpx.AsyncClient,
    session_id: str,
) -> dict[str, Any]:
    """
    Fetch one Omnigent session snapshot.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :returns: Parsed JSON snapshot.
    :raises httpx.HTTPError: If Omnigent returns a non-2xx status.
    :raises RuntimeError: If the response body is not a JSON object.
    """
    resp = await client.get(f"/v1/sessions/{url_component(session_id)}")
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"session {session_id!r} snapshot was not an object")
    return payload

async def _maybe_mirror_external_session_id(
    *,
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
) -> bool:
    """
    Mirror Claude's native session id onto the Omnigent conversation row.

    Reads the latest captured Claude-native session id from the
    bridge state file and, if present, PATCHes
    ``external_session_id`` on the Omnigent conversation. Best-effort: a
    transient HTTP failure logs a warning and returns ``False`` so
    the caller retries on the next poll. Once the PATCH succeeds we
    return ``True`` and the caller latches off — the value is
    durable server-side from that point on.

    A 4xx (e.g. the server rejects an attempted overwrite of an
    already-set different value) also latches off — the divergence
    is logged loudly but retrying would just hammer the server.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param bridge_dir: Native Claude bridge directory; the source of
        the captured Claude session id.
    :returns: ``True`` once mirroring is finished (or has been
        determined to be unrecoverable); ``False`` to retry next
        poll.
    """
    claude_sid = read_claude_session_id(bridge_dir)
    if claude_sid is None:
        return False
    try:
        await _patch_external_session_id(
            client,
            session_id=session_id,
            external_session_id=claude_sid,
        )
    except httpx.HTTPStatusError as exc:
        # 4xx means the server rejected the write outright (e.g.
        # overwrite conflict or schema validation). Retrying won't
        # help; latch off and let the operator see the log.
        if 400 <= exc.response.status_code < 500:
            _logger.warning(
                "AP rejected external_session_id PATCH (%s); session=%s claude_sid=%s",
                exc.response.status_code,
                session_id,
                claude_sid,
            )
            return True
        _logger.warning(
            "Transient Omnigent error PATCHing external_session_id (%s); session=%s — will retry",
            exc.response.status_code,
            session_id,
        )
        return False
    except httpx.HTTPError:
        _logger.warning(
            "Transient transport error PATCHing external_session_id; session=%s — will retry",
            session_id,
            exc_info=True,
        )
        return False
    return True

def _compaction_status_for_record(record: ClaudeHookRecord) -> str | None:
    """
    Map a hook record to a compaction-status value, if it is one.

    Claude Code brackets a compaction with two hooks the forwarder
    translates into ``external_compaction_status`` events:

    * ``PreCompact`` → ``"in_progress"`` — fires right before Claude
      compacts (manual ``/compact`` or automatic context overflow).
    * ``SessionStart`` with ``source == "compact"`` → ``"completed"``
      — fires when Claude resumes on the freshly-compacted context.
      (Claude Code has no dedicated post-compaction hook, so the
      ``source == "compact"`` SessionStart is the completion signal.)

    Other ``SessionStart`` sources (``startup`` / ``resume`` /
    ``clear``) are not compaction and return ``None``.

    :param record: One parsed hook JSONL record.
    :returns: ``"in_progress"``, ``"completed"``, or ``None`` when the
        record is not a compaction boundary.
    """
    if record.event_name == "PreCompact":
        return "in_progress"
    if record.event_name == "SessionStart" and record.source == "compact":
        return "completed"
    return None

async def _forward_available_status_events(
    *,
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
    state: HookForwardState,
    retry_tracker: _PostRetryTracker,
    task_subjects: dict[str, str],
    task_statuses: dict[str, str],
    task_order: list[str],
) -> HookForwardState:
    """
    Forward currently available hook events as ``session.status``.

    Maps ``Stop`` → ``idle`` and ``StopFailure`` → ``failed`` via
    ``POST /v1/sessions/{id}/events`` with type ``external_session_status``
    — the authoritative turn-end edges that drive sub-agent terminal
    delivery (see :data:`_HOOK_EVENT_TO_STATUS`). ``running`` stays
    PTY-derived (the pane-activity watcher drives the UI badge). Other hook
    event names advance the cursor without emitting (no status meaning).

    Also forwards native task state changes (``TaskCreated``,
    ``TaskCompleted``, ``PostToolUse``/``TaskUpdate``) and
    ``PostToolUse``/``TodoWrite`` todo updates as
    ``external_session_todos`` events. The ``task_subjects``,
    ``task_statuses``, and ``task_order`` dicts are mutated in-place
    to accumulate per-session task state across polls.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param bridge_dir: Native Claude bridge directory.
    :param state: Current hook cursor state.
    :param retry_tracker: In-memory retry/backoff tracker for hook
        status posts.
    :param task_subjects: Mutable map of task_id → subject text for the
        native task system, e.g. ``{"1": "Create folder 'abc'"}``.
        Updated in-place from ``TaskCreated`` hook events.
    :param task_statuses: Mutable map of task_id → status string for the
        native task system, e.g. ``{"1": "in_progress", "2": "pending"}``.
        Updated in-place from ``TaskCreated``, ``TaskCompleted``, and
        ``PostToolUse``/``TaskUpdate`` hook events.
    :param task_order: Mutable ordered list of task ids in creation order,
        e.g. ``["1", "2", "3"]``. Appended in-place from ``TaskCreated``
        events. Used to render the task list in a stable order.
    :returns: Updated state. On post failure, returns the last
        durable state so successfully-posted statuses are not
        retried and the failing event is retried later.
    """
    result = await asyncio.to_thread(_read_hook_events_for_state, bridge_dir, state)
    if not result.records:
        if result.event_cursor == state.event_cursor and result.byte_offset == (
            state.byte_offset or 0
        ):
            return state
        durable = HookForwardState(
            event_cursor=result.event_cursor,
            byte_offset=result.byte_offset,
            cursor_fingerprint=_jsonl_cursor_fingerprint(
                bridge_dir / _HOOKS_FILE, result.byte_offset
            ),
        )
        await _write_hook_state_async(bridge_dir, durable)
        return durable
    durable = state
    for record in result.records:
        status = _HOOK_EVENT_TO_STATUS.get(record.event_name or "")
        next_durable = HookForwardState(
            event_cursor=record.event_cursor,
            byte_offset=record.byte_offset,
            cursor_fingerprint=_jsonl_cursor_fingerprint(
                bridge_dir / _HOOKS_FILE, record.byte_offset
            ),
        )
        # Subagent lifecycle hooks land in the same hooks.jsonl as parent
        # events because subagent processes inherit the parent's hook
        # settings. With running/idle now PTY-derived, the only mapped
        # status left is ``StopFailure`` → ``failed``: a subagent's
        # failure must NOT flip the parent session to ``failed`` — the
        # parent turn is still running while it awaits the Agent tool
        # result.
        if status is not None and _is_subagent_hook_record(record):
            _logger.debug(
                "Skipping subagent hook status; session=%s event=%s status=%s transcript=%s",
                session_id,
                record.event_name,
                status,
                record.transcript_path,
            )
            durable = next_durable
            await _write_hook_state_async(bridge_dir, durable)
            continue
        if status is None:
            # Compaction boundary (PreCompact / SessionStart source=compact)
            # → forward as a compaction-status event so the web UI brackets
            # Claude's real terminal compaction with its spinner. Best-effort:
            # advance the cursor on failure so one failed post doesn't stall
            # the rest of the hook stream.
            compaction_status = _compaction_status_for_record(record)
            if compaction_status is not None:
                try:
                    await _post_external_compaction_status(
                        client,
                        session_id=session_id,
                        status=compaction_status,
                    )
                except httpx.HTTPError:
                    _logger.warning(
                        "Failed to forward Claude compaction status; "
                        "session=%s event_cursor=%s status=%s",
                        session_id,
                        record.event_cursor,
                        compaction_status,
                        exc_info=True,
                    )
                durable = next_durable
                await _write_hook_state_async(bridge_dir, durable)
                continue
            # Handle native task system events (TaskCreated, TaskCompleted,
            # PostToolUse/TaskUpdate). Mutate the caller-owned maps in-place
            # so task state accumulates across multiple polls within a session.
            native_todos_changed = False
            if record.event_name == "TaskCreated" and record.task_id is not None:
                if record.task_id not in task_subjects:
                    task_order.append(record.task_id)
                if record.task_subject is not None:
                    task_subjects[record.task_id] = record.task_subject
                task_statuses[record.task_id] = "pending"
                native_todos_changed = True
            elif record.event_name == "TaskCompleted" and record.task_id is not None:
                task_statuses[record.task_id] = "completed"
                native_todos_changed = True
            elif (
                record.event_name == "PostToolUse"
                and record.task_id is not None
                and record.task_status is not None
            ):
                # PostToolUse/TaskUpdate — update status only; subject
                # already in map from the TaskCreated event.
                task_statuses[record.task_id] = record.task_status
                native_todos_changed = True

            # Forward todo updates from PostToolUse/TodoWrite hook events.
            # Best-effort: log and advance the cursor on failure so a
            # single failed post doesn't stall hook processing.
            todos_to_post: list[dict[str, Any]] | None = None
            if record.todos is not None:
                todos_to_post = record.todos
            elif native_todos_changed and task_order:
                todos_to_post = [
                    {
                        "content": task_subjects.get(tid, tid),
                        "status": task_statuses.get(tid, "pending"),
                        # activeForm is the gerund form used by Claude's TodoWrite tool.
                        # Native task hooks don't provide it, so we intentionally
                        # reuse the content string here. TodoPanel reads activeForm
                        # for in-progress items when it differs from content, so
                        # keeping them equal suppresses duplicate rendering.
                        "activeForm": task_subjects.get(tid, tid),
                    }
                    for tid in task_order
                ]
            if todos_to_post is not None:
                try:
                    await _post_external_session_todos(
                        client,
                        session_id=session_id,
                        todos=todos_to_post,
                    )
                except httpx.HTTPError:
                    _logger.warning(
                        "Failed to forward Claude todos from hook; session=%s event_cursor=%s",
                        session_id,
                        record.event_cursor,
                        exc_info=True,
                    )
            durable = next_durable
            await _write_hook_state_async(bridge_dir, durable)
            continue
        retry_key = f"hook:{record.event_cursor}:{record.byte_offset}:{status}"
        if retry_tracker.retry_delay_s(retry_key) is not None:
            return durable
        try:
            await _post_external_session_status(
                client,
                session_id=session_id,
                status=status,
            )
        except httpx.HTTPError as exc:
            decision = retry_tracker.record_failure(retry_key, exc)
            if decision.exhausted:
                _logger.error(
                    "Dropping Claude hook status after permanent HTTP failures; "
                    "session=%s bridge_dir=%s event_cursor=%s status=%s "
                    "attempts=%s http_status=%s",
                    session_id,
                    bridge_dir,
                    record.event_cursor,
                    status,
                    decision.attempts,
                    _http_status_for_log(exc),
                )
                if status != "failed":
                    await _post_forwarder_failed_status(
                        client,
                        session_id=session_id,
                        bridge_dir=bridge_dir,
                        reason=f"hook status {status} rejected",
                    )
                durable = next_durable
                await _write_hook_state_async(bridge_dir, durable)
                continue
            _logger.warning(
                "Failed to forward Claude hook status; session=%s bridge_dir=%s "
                "event_cursor=%s status=%s attempt=%s permanent=%s "
                "next_retry_s=%.3f http_status=%s",
                session_id,
                bridge_dir,
                record.event_cursor,
                status,
                decision.attempts,
                decision.permanent,
                decision.delay_s,
                _http_status_for_log(exc),
                exc_info=True,
            )
            return durable
        retry_tracker.clear(retry_key)
        durable = next_durable
        await _write_hook_state_async(bridge_dir, durable)
    durable = HookForwardState(
        event_cursor=result.event_cursor,
        byte_offset=result.byte_offset,
        cursor_fingerprint=_jsonl_cursor_fingerprint(bridge_dir / _HOOKS_FILE, result.byte_offset),
    )
    await _write_hook_state_async(bridge_dir, durable)
    return durable

async def _forward_available_items(
    *,
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
    agent_name: str,
    state: TranscriptForwardState,
    retry_tracker: _PostRetryTracker,
    skip_user_messages: bool = False,
    dedupe: _ForwardDedupeState,
) -> TranscriptForwardState:
    """
    Forward currently available transcript items after ``state``.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param bridge_dir: Native Claude bridge directory.
    :param agent_name: Agent/model name to stamp on mirrored output.
    :param state: Current transcript cursor state.
    :param retry_tracker: In-memory retry/backoff tracker for
        transcript item posts.
    :param dedupe: Last usage / context-window / model values POSTed;
        mutated in place to suppress duplicate ``external_*`` events.
    :returns: The updated transcript cursor state. On post failure it
        is the last durable cursor so retries don't re-post successful
        items.
    """
    result = await asyncio.to_thread(_read_transcript_items_for_state, state, agent_name)
    items = result.items
    if not items:
        if result.line_cursor == state.line_cursor and result.byte_offset == (
            state.byte_offset or 0
        ):
            return state
    current_response_id = result.current_response_id
    seen_source_ids = list(state.seen_source_ids)
    seen = set(seen_source_ids)
    # NOTE: the old "re-assert running on resumed agent output" hack lived
    # here. It only existed to paper over the hook model's compaction
    # blind spot (``Stop`` → idle, then an ``isCompactSummary`` resume that
    # never fired ``UserPromptSubmit``). PTY-activity status makes it
    # obsolete: the pane keeps changing through a mid-turn compaction, so
    # the runner's watcher holds the session ``running`` directly.
    updated = state
    for item in items:
        if item.source_id in seen:
            continue
        if skip_user_messages and item.item_type == "message" and item.data.get("role") == "user":
            seen_source_ids.append(item.source_id)
            seen.add(item.source_id)
            continue
        retry_key = f"item:{item.source_id}"
        if retry_tracker.retry_delay_s(retry_key) is not None:
            return updated
        try:
            await _post_external_conversation_item(
                client,
                session_id=session_id,
                item=item,
            )
        except httpx.HTTPError as exc:
            decision = retry_tracker.record_failure(retry_key, exc)
            if decision.exhausted:
                _logger.error(
                    "Dropping Claude transcript item after permanent HTTP failures; "
                    "session=%s bridge_dir=%s source_id=%s item_type=%s "
                    "attempts=%s http_status=%s",
                    session_id,
                    bridge_dir,
                    item.source_id,
                    item.item_type,
                    decision.attempts,
                    _http_status_for_log(exc),
                )
                await _post_forwarder_failed_status(
                    client,
                    session_id=session_id,
                    bridge_dir=bridge_dir,
                    reason=f"transcript item {item.source_id} rejected",
                )
                seen.add(item.source_id)
                seen_source_ids.append(item.source_id)
                updated = TranscriptForwardState(
                    transcript_path=state.transcript_path,
                    line_cursor=state.line_cursor,
                    byte_offset=state.byte_offset,
                    current_response_id=current_response_id,
                    seen_source_ids=_bounded_seen_source_ids(seen_source_ids),
                    cursor_fingerprint=state.cursor_fingerprint,
                )
                await _write_forward_state_async(bridge_dir, updated)
                continue
            if post_may_have_been_delivered(exc):
                # Ambiguous failure: the server may have committed this
                # item before the response was lost. External items aren't
                # deduped, so a retry would duplicate the bubble —
                # skip it. At worst one item is lost on a flaky POST.
                _logger.warning(
                    "Skipping Claude transcript item after an ambiguous POST failure "
                    "(may already be committed); not retrying to avoid a duplicate; "
                    "session=%s bridge_dir=%s source_id=%s item_type=%s http_status=%s",
                    session_id,
                    bridge_dir,
                    item.source_id,
                    item.item_type,
                    _http_status_for_log(exc),
                    exc_info=True,
                )
                retry_tracker.clear(retry_key)
                seen.add(item.source_id)
                seen_source_ids.append(item.source_id)
                updated = TranscriptForwardState(
                    transcript_path=state.transcript_path,
                    line_cursor=state.line_cursor,
                    byte_offset=state.byte_offset,
                    current_response_id=current_response_id,
                    seen_source_ids=_bounded_seen_source_ids(seen_source_ids),
                    cursor_fingerprint=state.cursor_fingerprint,
                )
                await _write_forward_state_async(bridge_dir, updated)
                continue
            _logger.warning(
                "Failed to forward Claude transcript item; session=%s bridge_dir=%s "
                "source_id=%s item_type=%s attempt=%s permanent=%s "
                "next_retry_s=%.3f http_status=%s",
                session_id,
                bridge_dir,
                item.source_id,
                item.item_type,
                decision.attempts,
                decision.permanent,
                decision.delay_s,
                _http_status_for_log(exc),
                exc_info=True,
            )
            return updated
        retry_tracker.clear(retry_key)
        await _maybe_sync_effort_from_slash_command(client, session_id=session_id, item=item)
        seen.add(item.source_id)
        seen_source_ids.append(item.source_id)
        updated = TranscriptForwardState(
            transcript_path=state.transcript_path,
            line_cursor=state.line_cursor,
            byte_offset=state.byte_offset,
            current_response_id=current_response_id,
            seen_source_ids=_bounded_seen_source_ids(seen_source_ids),
            cursor_fingerprint=state.cursor_fingerprint,
        )
        await _write_forward_state_async(bridge_dir, updated)
    updated = TranscriptForwardState(
        transcript_path=state.transcript_path,
        line_cursor=result.line_cursor,
        byte_offset=result.byte_offset,
        current_response_id=current_response_id,
        seen_source_ids=_bounded_seen_source_ids(seen_source_ids),
        cursor_fingerprint=_jsonl_cursor_fingerprint(state.transcript_path, result.byte_offset),
    )
    await _write_forward_state_async(bridge_dir, updated)
    # POST usage AFTER items so the ring never leads the transcript.
    # Best-effort: a failed post is retried on the next poll.
    #
    # Authoritative source for both numerator and denominator is the
    # statusLine stdin captured by ``omnigent.claude_native_status``
    # — Claude Code knows the real context window for the active
    # model + beta tier. The JSONL ``message.usage`` is used as a
    # numerator fallback only when the statusLine hasn't fired yet
    # (e.g. cold-resume before the first render tick).
    status_state = await asyncio.to_thread(read_claude_context_state, bridge_dir)
    resolved_context_window = (
        status_state.get("context_window_size") if status_state is not None else None
    )
    usage_from_status = (
        _usage_from_status_state(status_state) if status_state is not None else None
    )
    posted_usage = usage_from_status if usage_from_status is not None else result.latest_usage
    # Cost (``cumulative_cost_usd``) is POSTed separately by
    # ``_forward_session_cost``, which reconciles the statusLine total with the
    # forwarder's real-time sub-agent transcript estimate via max(). Strip it
    # here so this token/context-window post and the cost post don't both SET
    # ``total_cost_usd`` with different values and flap it on alternating polls.
    if posted_usage is not None and "cumulative_cost_usd" in posted_usage:
        posted_usage = {
            key: value for key, value in posted_usage.items() if key != "cumulative_cost_usd"
        }
    usage_changed = posted_usage is not None and posted_usage != dedupe.usage
    window_changed = (
        resolved_context_window is not None and resolved_context_window != dedupe.context_window
    )
    if usage_changed or window_changed:
        try:
            await _post_external_session_usage(
                client,
                session_id=session_id,
                usage=posted_usage,
                context_window=resolved_context_window,
            )
            if usage_changed:
                dedupe.usage = posted_usage
            if window_changed:
                dedupe.context_window = resolved_context_window
        except httpx.HTTPError as exc:
            _logger.warning(
                "Failed to forward Claude transcript usage; session=%s bridge_dir=%s "
                "http_status=%s",
                session_id,
                bridge_dir,
                _http_status_for_log(exc),
                exc_info=True,
            )
    # Mirror a TUI-side `/model` switch to the web picker. The transcript
    # records the resolved concrete id (e.g. "claude-opus-4-8"); collapse
    # it to the picker's tier alias. This transcript-derived observation
    # only fires when a turn produces a fresh ``message.model``, so it lags
    # an in-pane switch by one turn — the per-poll statusLine sync
    # (:func:`_forward_model_from_status`) is the primary, low-latency
    # source; this stays as a fallback for cold-resume before the first
    # statusLine render. Both share ``dedupe`` so neither double-posts.
    await _post_model_change_if_new(
        client,
        session_id=session_id,
        dedupe=dedupe,
        alias=_model_alias_for(result.latest_model),
    )
    return updated

async def _post_external_conversation_item(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    item: ClaudeTranscriptItem,
) -> None:
    """
    Post one mirrored transcript item to the Sessions API.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param item: Transcript-derived conversation item.
    :returns: None.
    :raises httpx.HTTPError: If the Omnigent request fails or is rejected.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": item.item_type,
                "item_data": item.data,
                "response_id": item.response_id,
            },
        },
    )
    resp.raise_for_status()

async def _post_external_session_usage(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    usage: dict[str, float] | None,
    context_window: int | None = None,
) -> None:
    """
    Post one ``external_session_usage`` event to the Sessions API.

    At least one of ``usage`` / ``context_window`` must be set; a
    payload with neither is a no-op (the server would 400 it).

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param usage: ``message.usage`` snapshot, or ``None`` to skip.
    :param context_window: Resolved window in tokens, or ``None`` to
        leave the server's persisted value untouched.
    :raises httpx.HTTPError: If the Omnigent request fails or is rejected.
    """
    payload: dict[str, Any] = {}
    if usage is not None:
        payload.update(usage)
    if context_window is not None:
        payload["context_window"] = context_window
    if not payload:
        return
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_session_usage", "data": payload},
    )
    resp.raise_for_status()

def _model_alias_for(model: str | None) -> str | None:
    """
    Collapse a concrete Claude model id to the picker's tier alias.

    The web model picker speaks Claude Code's version-agnostic aliases
    (``"fable"`` / ``"opus"`` / ``"sonnet"`` / ``"haiku"``); the
    transcript records the resolved concrete id (e.g.
    ``"claude-opus-4-8"`` or ``"databricks-claude-sonnet-4-6"``).
    Mapping to the tier keeps the mirrored value in the picker's
    vocabulary and makes a web→TUI round-trip a no-op.

    :param model: Concrete model id from the transcript, e.g.
        ``"claude-opus-4-8"``; ``None`` when none observed yet.
    :returns: ``"fable"`` / ``"opus"`` / ``"sonnet"`` / ``"haiku"``
        when the id carries a known tier token, else ``None`` (the
        caller skips the post rather than surface an id the picker
        can't render).
    """
    if not model:
        return None
    lowered = model.lower()
    for tier in ("fable", "opus", "sonnet", "haiku"):
        if tier in lowered:
            return tier
    return None

async def _post_external_model_change(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    model: str,
) -> None:
    """
    Post one ``external_model_change`` event to the Sessions API.

    Lets the web model picker reflect a model switch made inside the
    Claude Code terminal (a ``/model`` command or the in-TUI picker).

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id, e.g.
        ``"conv_abc123"``.
    :param model: Tier alias the session is now on, e.g. ``"opus"``.
    :raises httpx.HTTPError: If the Omnigent request fails or is rejected.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_model_change", "data": {"model": model}},
    )
    resp.raise_for_status()

async def _post_model_change_if_new(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    dedupe: _ForwardDedupeState,
    alias: str | None,
) -> None:
    """
    Mirror an observed model tier alias to ``model_override``, deduped.

    Shared by the transcript-driven path (:func:`_forward_available_items`)
    and the statusLine-driven per-poll path
    (:func:`_forward_model_from_status`). The FIRST observation is the
    session's spawn default, not a switch, so it seeds the dedupe baseline
    WITHOUT posting (posting it could clobber a pending silent model
    handoff). Every later change posts ``external_model_change``. Both
    callers pass the same ``dedupe`` so whichever observes a switch first
    posts it and the other no-ops. Best-effort: a failed POST leaves
    ``posted_model`` behind ``observed_model`` so the next poll retries.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param dedupe: Shared per-session dedupe state; mutated in place.
    :param alias: Tier alias just observed (``"opus"`` / ``"sonnet"`` /
        …), or ``None`` when this source carried no recognizable model on
        this poll. ``observed_model`` is sticky across polls, so passing
        ``None`` does NOT clear it — it just means "no fresh observation,"
        and a previously-observed-but-unposted model is still reconciled
        (retried) here.
    """
    if alias is not None:
        dedupe.observed_model = alias
    if dedupe.observed_model is None or dedupe.observed_model == dedupe.posted_model:
        return
    if dedupe.posted_model is None:
        # First observation = the spawn default; seed the baseline without
        # posting so it can't clobber a pending silent model handoff.
        dedupe.posted_model = dedupe.observed_model
        return
    try:
        await _post_external_model_change(
            client,
            session_id=session_id,
            model=dedupe.observed_model,
        )
        dedupe.posted_model = dedupe.observed_model
    except httpx.HTTPError:
        # Leave posted_model behind observed_model so the next poll retries.
        _logger.warning(
            "Failed to mirror model change to Omnigent session=%s; model pill / "
            "cost-budget gate may lag until the next poll",
            session_id,
            exc_info=True,
        )

async def _forward_model_from_status(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    dedupe: _ForwardDedupeState,
) -> None:
    """
    Mirror the statusLine-reported active model to ``model_override`` each poll.

    Claude Code rewrites the statusLine stdin on every TUI render — including
    right after an in-pane ``/model`` switch, BEFORE the next turn runs. The
    wrapper (:mod:`omnigent.claude_native_status`) persists that model into
    ``context.json``. Reading it here, every poll and independently of new
    transcript items, is what lets a policy that gates on the active model
    (e.g. the session cost-budget hard cap, which only blocks expensive
    tiers) see the new model on the user's NEXT message — instead of one
    turn later, which is what happened when the model was derived solely
    from the next turn's transcript ``message.model``.

    Best-effort and idempotent: shares ``dedupe`` with the transcript path,
    so a no-op when the model is unchanged.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param bridge_dir: Native Claude bridge directory.
    :param dedupe: Shared per-session model dedupe state.
    """
    status_state = await asyncio.to_thread(read_claude_context_state, bridge_dir)
    if status_state is None:
        return
    model = status_state.get("model")
    alias = _model_alias_for(model if isinstance(model, str) else None)
    await _post_model_change_if_new(
        client,
        session_id=session_id,
        dedupe=dedupe,
        alias=alias,
    )

async def _post_external_session_status(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    status: str,
) -> None:
    """
    Post one ``external_session_status`` event to the Sessions API.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param status: Session status value, e.g. ``"idle"`` or
        ``"failed"``.
    :returns: None.
    :raises httpx.HTTPError: If the Omnigent request fails or is rejected.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_session_status",
            "data": {"status": status},
        },
    )
    resp.raise_for_status()

async def _post_external_compaction_status(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    status: str,
) -> None:
    """
    Post one ``external_compaction_status`` event to the Sessions API.

    Brackets Claude Code's own compaction so the web UI can show its
    "Compacting conversation…" spinner while Claude runs the real
    compaction in the terminal. ``"in_progress"`` is sent from the
    ``PreCompact`` hook and ``"completed"`` from the post-compaction
    ``SessionStart`` (``source == "compact"``) hook. The Omnigent server maps
    these to the ``response.compaction.in_progress`` /
    ``response.compaction.completed`` SSE events the web client already
    renders.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param status: Compaction status value, ``"in_progress"`` or
        ``"completed"``.
    :returns: None.
    :raises httpx.HTTPError: If the Omnigent request fails or is rejected.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_compaction_status",
            "data": {"status": status},
        },
    )
    resp.raise_for_status()

async def _patch_external_session_id(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    external_session_id: str,
) -> None:
    """
    PATCH the Omnigent conversation row with the Claude-native session id.

    The server's ``set_external_session_id`` store call is idempotent
    on same-value writes and rejects overwrite of an already-set
    different value with ``400 invalid_input``. Wrapper bridges should
    PATCH the value once when they first observe it from Claude.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id, e.g.
        ``"conv_abc123"``.
    :param external_session_id: Runtime-native session id captured
        from a Claude hook event,
        e.g. ``"a1b2c3d4-1234-5678-9abc-def012345678"``.
    :returns: None.
    :raises httpx.HTTPError: If the Omnigent request fails or is rejected.
    """
    resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"external_session_id": external_session_id},
    )
    resp.raise_for_status()

async def _maybe_sync_effort_from_slash_command(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    item: ClaudeTranscriptItem,
) -> None:
    """
    Mirror an in-pane ``/effort`` change onto the Omnigent session row.

    The pane changes the binary but doesn't touch AP; PATCH
    ``reasoning_effort`` (``silent=True`` to avoid re-injecting ``/effort``
    into the pane) so the pill tracks it. Best-effort — logged, not raised.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id, e.g. ``"conv_abc123"``.
    :param item: A just-forwarded item; only a ``slash_command`` named
        ``"effort"`` triggers a PATCH.
    :returns: None.
    """
    if item.item_type != "slash_command" or item.data.get("name") != "effort":
        return
    arguments = item.data.get("arguments")
    if not isinstance(arguments, str):
        return
    # Bare level (set) or clear alias changes state; bare /effort is a show no-op.
    level = arguments.strip().lower()
    if level not in CLAUDE_EFFORTS and level not in EFFORT_CLEAR_VALUES:
        return
    try:
        resp = await client.patch(
            f"/v1/sessions/{session_id}",
            json={"reasoning_effort": level, "silent": True},
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        _logger.warning(
            "Failed to mirror in-pane /effort=%s to Omnigent session=%s; "
            "effort pill may lag until the next change",
            level,
            session_id,
            exc_info=True,
        )

async def _post_forwarder_failed_status(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    reason: str,
) -> None:
    """
    Best-effort publish a failed status after dropping a poison event.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param bridge_dir: Native Claude bridge directory.
    :param reason: Diagnostic reason for the failure event, e.g.
        ``"transcript item item-1 rejected"``.
    :returns: None.
    """
    try:
        await _post_external_session_status(client, session_id=session_id, status="failed")
    except httpx.HTTPError:
        _logger.warning(
            "Failed to publish Claude forwarder failure status; "
            "session=%s bridge_dir=%s reason=%s",
            session_id,
            bridge_dir,
            reason,
            exc_info=True,
        )

async def _post_external_session_todos(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    todos: list[dict[str, Any]],
) -> None:
    """
    Post one ``external_session_todos`` event to the Sessions API.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id, e.g. ``"conv_abc123"``.
    :param todos: Current Claude todo list, e.g.
        ``[{"content": "Write tests", "status": "in_progress",
        "activeForm": "Writing tests"}]``.
    :returns: None.
    :raises httpx.HTTPError: If the Omnigent request fails or is rejected.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_session_todos", "data": {"todos": todos}},
    )
    resp.raise_for_status()

def _is_permanent_http_error(exc: httpx.HTTPError) -> bool:
    """
    Return whether ``exc`` is a permanent Omnigent rejection.

    :param exc: HTTP exception raised while posting an Omnigent event.
    :returns: ``True`` for non-transient 4xx status responses,
        otherwise ``False``.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    status_code = exc.response.status_code
    return 400 <= status_code < 500 and status_code not in _HTTP_TRANSIENT_STATUS_CODES

def _http_status_for_log(exc: httpx.HTTPError) -> int | None:
    """
    Extract an HTTP status code from ``exc`` when present.

    :param exc: HTTP exception raised while posting an Omnigent event.
    :returns: Numeric HTTP status code, or ``None`` for transport
        failures that did not receive a response.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    return None

def _usage_from_status_state(state: dict[str, Any]) -> dict[str, float] | None:
    """
    Convert statusLine ``current_usage`` (+ cost) into the Omnigent usage shape.

    Sums input + cache_creation + cache_read for ``context_tokens``
    (matches claude-hud's ``getTotalTokens``: only input-side tokens
    occupy the next prompt's budget). When the statusLine also captured
    Claude Code's cumulative ``total_cost_usd``, it's surfaced as
    ``cumulative_cost_usd`` so the server can persist native session cost
    (SET semantics). Returns ``None`` when the state has no usable
    ``current_usage`` so the caller falls back to the JSONL-derived value.

    :param state: Parsed ``context.json`` payload.
    :returns: Usage dict (token counts plus optional
        ``cumulative_cost_usd``), or ``None``.
    """
    usage = state.get("current_usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("input_tokens")
    if not isinstance(input_tokens, int):
        return None
    cc = usage.get("cache_creation_input_tokens")
    cr = usage.get("cache_read_input_tokens")
    output_tokens = usage.get("output_tokens")
    cc_i = cc if isinstance(cc, int) else 0
    cr_i = cr if isinstance(cr, int) else 0
    out_i = output_tokens if isinstance(output_tokens, int) else 0
    # Token counts stay ``int`` (the server validates context_tokens with
    # ``isinstance(int)``); only ``cumulative_cost_usd`` is a float. ``float``
    # annotation is fine — ``int`` is a subtype under the numeric tower.
    result: dict[str, float] = {
        "context_tokens": input_tokens + cc_i + cr_i,
        "input_tokens": input_tokens,
        "output_tokens": out_i,
    }
    total_cost = state.get("total_cost_usd")
    if (
        isinstance(total_cost, (int, float))
        and not isinstance(total_cost, bool)
        and total_cost >= 0
    ):
        result["cumulative_cost_usd"] = float(total_cost)
    return result

def _bounded_seen_source_ids(seen_source_ids: list[str]) -> tuple[str, ...]:
    """
    Return a bounded tuple of recently forwarded source ids.

    :param seen_source_ids: Source ids accumulated in observation
        order.
    :returns: Tuple capped to the most recent source ids. The cap
        prevents the state file from growing without bound while
        retaining enough idempotency history for retries.
    """
    return tuple(seen_source_ids[-_MAX_SEEN_SOURCE_IDS:])

def _read_forward_state(bridge_dir: Path) -> TranscriptForwardState | None:
    """
    Read the durable forwarder cursor from the bridge directory.

    :param bridge_dir: Native Claude bridge directory.
    :returns: Cursor state, or ``None`` if no usable state exists.
    """
    try:
        raw = json.loads((bridge_dir / _FORWARDER_STATE_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    transcript_path = raw.get("transcript_path")
    line_cursor = raw.get("line_cursor")
    byte_offset = raw.get("byte_offset")
    current_response_id = raw.get("current_response_id")
    cursor_fingerprint = raw.get("cursor_fingerprint")
    seen_source_ids = raw.get("seen_source_ids", [])
    if not isinstance(transcript_path, str) or not isinstance(line_cursor, int):
        return None
    if line_cursor < 0:
        return None
    if byte_offset is not None and (not isinstance(byte_offset, int) or byte_offset < 0):
        return None
    if current_response_id is not None and not isinstance(current_response_id, str):
        return None
    if cursor_fingerprint is not None and not isinstance(cursor_fingerprint, str):
        return None
    if not isinstance(seen_source_ids, list) or not all(
        isinstance(source_id, str) for source_id in seen_source_ids
    ):
        seen_source_ids = []
    return TranscriptForwardState(
        transcript_path=Path(transcript_path),
        line_cursor=line_cursor,
        byte_offset=byte_offset,
        current_response_id=current_response_id,
        seen_source_ids=tuple(seen_source_ids),
        cursor_fingerprint=cursor_fingerprint,
    )

def _write_forward_state(bridge_dir: Path, state: TranscriptForwardState) -> None:
    """
    Write the durable forwarder cursor to the bridge directory.

    :param bridge_dir: Native Claude bridge directory.
    :param state: Cursor state to persist.
    :returns: None.
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "transcript_path": str(state.transcript_path),
        "line_cursor": state.line_cursor,
        "current_response_id": state.current_response_id,
        "seen_source_ids": list(state.seen_source_ids),
        "updated_at": time.time(),
    }
    if state.byte_offset is not None:
        payload["byte_offset"] = state.byte_offset
    if state.cursor_fingerprint is not None:
        payload["cursor_fingerprint"] = state.cursor_fingerprint
    _write_json_atomic(bridge_dir / _FORWARDER_STATE_FILE, payload)

async def _write_forward_state_async(
    bridge_dir: Path,
    state: TranscriptForwardState,
) -> None:
    """
    Persist transcript state without blocking the asyncio event loop.

    :param bridge_dir: Native Claude bridge directory.
    :param state: Cursor state to persist.
    :returns: None.
    """
    await asyncio.to_thread(_write_forward_state, bridge_dir, state)

def _complete_jsonl_end_offset(path: Path) -> int:
    """
    Return the offset after the last newline-terminated JSONL record.

    :param path: JSONL file path.
    :returns: File size when it ends in ``"\\n"``, otherwise the byte
        offset immediately after the previous newline. Returns ``0``
        for missing files or a single partial first record.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size == 0:
                return 0
            handle.seek(size - 1)
            if handle.read(1) == b"\n":
                return size
            block_end = size
            while block_end > 0:
                block_start = max(0, block_end - 65_536)
                handle.seek(block_start)
                data = handle.read(block_end - block_start)
                newline_index = data.rfind(b"\n")
                if newline_index >= 0:
                    return block_start + newline_index + 1
                block_end = block_start
    except FileNotFoundError:
        return 0
    return 0

def _jsonl_cursor_fingerprint(path: Path, byte_offset: int) -> str | None:
    """
    Hash bytes immediately before a JSONL cursor for stale-cursor checks.

    :param path: JSONL file path.
    :param byte_offset: Cursor byte offset, e.g. ``4096``.
    :returns: SHA-256 digest for the bytes before the cursor, or
        ``None`` when the file does not exist or the offset is invalid.
    """
    if byte_offset < 0:
        return None
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if byte_offset > size:
                return None
            sample_start = max(0, byte_offset - _CURSOR_FINGERPRINT_BYTES)
            handle.seek(sample_start)
            sample = handle.read(byte_offset - sample_start)
    except FileNotFoundError:
        return None
    payload = byte_offset.to_bytes(8, "big", signed=False) + sample
    return hashlib.sha256(payload).hexdigest()

def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """
    Write JSON to *path* via a same-directory temporary file.

    :param path: Destination JSON file.
    :param payload: JSON-serializable payload.
    :returns: None.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(json.dumps(payload, separators=(",", ":")))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    finally:
        if tmp_path is not None:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _cost as _sib_cost
    from . import _fwd_state as _sib_fwd_state
    from . import _hooks as _sib_hooks
    from . import _subagent as _sib_subagent
    from . import _supervisor as _sib_supervisor
    from . import _transcript as _sib_transcript
    for _key, _value in _sib_cost.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_fwd_state.__dict__.items():
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
    for _key, _value in _sib_transcript.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
