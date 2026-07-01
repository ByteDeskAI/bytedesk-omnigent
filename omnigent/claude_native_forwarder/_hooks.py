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

def _is_fork_hook_record(record: ClaudeHookRecord) -> bool:
    """
    Return whether a hook record represents Claude ``/fork``.

    The stable signal comes from Claude's structured ``forkedFrom``
    transcript metadata or a recent local-command record, not from the
    human-facing session title. Hook-side annotations are still
    honored for idempotency when the synchronous hook has already
    completed the Omnigent fork.

    :param record: Claude hook record read from hooks.jsonl.
    :returns: ``True`` when the active Omnigent session should be forked.
    """
    if record.fork_detected or record.fork_rotated_to:
        return True
    if record.event_name != "SessionStart" or record.source != "resume":
        return False
    if record.transcript_path is None or record.claude_session_id is None:
        return False
    if record.recorded_at is None:
        return False
    if record.previous_claude_session_id is None:
        return False
    if record.claude_session_was_seen is not False:
        return False
    return transcript_has_forked_from_marker(
        record.transcript_path,
        claude_session_id=record.claude_session_id,
        source_claude_session_id=record.previous_claude_session_id,
    ) or transcript_has_recent_local_command(
        record.transcript_path,
        claude_session_id=record.claude_session_id,
        recorded_at=record.recorded_at,
        command_names=_FORK_COMMAND_NAMES,
    )

async def _ensure_hook_state(
    bridge_dir: Path,
    *,
    start_at_end: bool,
    session_id: str,
) -> HookForwardState:
    """
    Return the hook cursor state, seeding it on first use.

    :param bridge_dir: Native Claude bridge directory.
    :param start_at_end: When ``True`` and no prior cursor exists,
        start after the current complete hook records so prior records
        (e.g. an earlier ``Stop`` from a stale session) are not
        re-published on reattach while a partial trailing record can
        still complete and be read.
    :param session_id: Omnigent session/conversation id, e.g.
        ``"conv_abc123"``. Used for stale-cursor diagnostics.
    :returns: The cursor state to use for the next hook poll.
    """
    state = _read_hook_state(bridge_dir)
    if state is not None:
        return _validated_hook_state(bridge_dir, state, session_id=session_id)
    byte_offset = 0
    if start_at_end:
        byte_offset = await asyncio.to_thread(_hook_end_offset, bridge_dir)
    state = HookForwardState(
        event_cursor=0,
        byte_offset=byte_offset,
        cursor_fingerprint=_jsonl_cursor_fingerprint(bridge_dir / _HOOKS_FILE, byte_offset),
    )
    await _write_hook_state_async(bridge_dir, state)
    return state

def _read_hook_events_for_state(
    bridge_dir: Path,
    state: HookForwardState,
) -> HookReadResult:
    """
    Read hook events using the best cursor available in ``state``.

    :param bridge_dir: Native Claude bridge directory.
    :param state: Current hook forwarder state.
    :returns: Hook records and updated cursors. States without a
        byte offset are migrated by one line-cursor compatibility scan.
    """
    if state.byte_offset is None:
        return read_hook_events_since_with_position(bridge_dir, state.event_cursor)
    return read_hook_events_from_offset(
        bridge_dir,
        state.byte_offset,
        start_event_count=state.event_cursor,
    )

def _validated_hook_state(
    bridge_dir: Path,
    state: HookForwardState,
    *,
    session_id: str,
) -> HookForwardState:
    """
    Reset a hook cursor if its byte-offset fingerprint is stale.

    :param bridge_dir: Native Claude bridge directory.
    :param state: Hook cursor loaded from memory or disk.
    :param session_id: Omnigent session/conversation id, e.g.
        ``"conv_abc123"``. Used for diagnostics.
    :returns: ``state`` when its byte cursor still matches the file,
        otherwise a fresh cursor at the beginning of ``hooks.jsonl``.
    """
    if state.byte_offset is None:
        return state
    hooks_path = bridge_dir / _HOOKS_FILE
    current_fingerprint = _jsonl_cursor_fingerprint(hooks_path, state.byte_offset)
    if current_fingerprint is None:
        _logger.warning(
            "Claude hook JSONL cursor invalid; resetting cursor; "
            "session=%s bridge_dir=%s byte_offset=%s",
            session_id,
            bridge_dir,
            state.byte_offset,
        )
    elif state.cursor_fingerprint is None:
        _logger.warning(
            "Claude hook JSONL cursor missing fingerprint; resetting cursor; "
            "session=%s bridge_dir=%s byte_offset=%s",
            session_id,
            bridge_dir,
            state.byte_offset,
        )
    elif current_fingerprint == state.cursor_fingerprint:
        return state
    else:
        _logger.warning(
            "Claude hook JSONL cursor fingerprint changed; resetting cursor; "
            "session=%s bridge_dir=%s byte_offset=%s",
            session_id,
            bridge_dir,
            state.byte_offset,
        )
    return HookForwardState(
        event_cursor=0,
        byte_offset=0,
        cursor_fingerprint=_jsonl_cursor_fingerprint(hooks_path, 0),
    )

def _read_hook_state(bridge_dir: Path) -> HookForwardState | None:
    """
    Read the durable hook forwarder cursor from the bridge directory.

    :param bridge_dir: Native Claude bridge directory.
    :returns: Cursor state, or ``None`` if no usable state exists.
    """
    try:
        raw = json.loads((bridge_dir / _HOOK_STATE_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    event_cursor = raw.get("event_cursor")
    byte_offset = raw.get("byte_offset")
    cursor_fingerprint = raw.get("cursor_fingerprint")
    if not isinstance(event_cursor, int) or event_cursor < 0:
        return None
    if byte_offset is not None and (not isinstance(byte_offset, int) or byte_offset < 0):
        return None
    if cursor_fingerprint is not None and not isinstance(cursor_fingerprint, str):
        return None
    return HookForwardState(
        event_cursor=event_cursor,
        byte_offset=byte_offset,
        cursor_fingerprint=cursor_fingerprint,
    )

def _write_hook_state(bridge_dir: Path, state: HookForwardState) -> None:
    """
    Write the durable hook forwarder cursor to the bridge directory.

    :param bridge_dir: Native Claude bridge directory.
    :param state: Cursor state to persist.
    :returns: None.
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "event_cursor": state.event_cursor,
        "updated_at": time.time(),
    }
    if state.byte_offset is not None:
        payload["byte_offset"] = state.byte_offset
    if state.cursor_fingerprint is not None:
        payload["cursor_fingerprint"] = state.cursor_fingerprint
    _write_json_atomic(bridge_dir / _HOOK_STATE_FILE, payload)

async def _write_hook_state_async(bridge_dir: Path, state: HookForwardState) -> None:
    """
    Persist hook state without blocking the asyncio event loop.

    :param bridge_dir: Native Claude bridge directory.
    :param state: Cursor state to persist.
    :returns: None.
    """
    await asyncio.to_thread(_write_hook_state, bridge_dir, state)

def _hook_end_offset(bridge_dir: Path) -> int:
    """
    Return the byte offset after the last complete hook JSONL record.

    :param bridge_dir: Native Claude bridge directory.
    :returns: Offset after the last newline-terminated hook record, or
        ``0`` when no complete hook record exists yet.
    """
    return _complete_jsonl_end_offset(bridge_dir / _HOOKS_FILE)


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _cost as _sib_cost
    from . import _fwd_state as _sib_fwd_state
    from . import _helpers as _sib_helpers
    from . import _subagent as _sib_subagent
    from . import _supervisor as _sib_supervisor
    from . import _transcript as _sib_transcript
    for _key, _value in _sib_cost.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_fwd_state.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_helpers.__dict__.items():
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
