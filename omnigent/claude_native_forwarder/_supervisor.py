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

async def _supervisor_sleep(seconds: float) -> None:
    """
    Sleep helper used between forwarder restarts.

    Exists as a private indirection so tests can stub the wait
    without monkeypatching the global ``asyncio.sleep`` (which would
    leak across the whole pytest process; see project test rule 14).

    :param seconds: Duration to sleep, e.g. ``1.0``.
    """
    await asyncio.sleep(seconds)

def _supervisor_monotonic() -> float:
    """
    Monotonic clock reading used to measure forwarder uptime.

    Exists as a private indirection so tests can drive the
    healthy-uptime branch deterministically without touching the
    global ``time.monotonic`` (same module-singleton hazard as
    ``asyncio.sleep``).

    :returns: Seconds from an unspecified monotonic epoch.
    """
    return time.monotonic()

async def supervise_forwarder(
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
    Run :func:`forward_claude_transcript_to_session` under a restart supervisor.

    The forwarder's own loop catches :class:`Exception` per iteration,
    but an error raised outside that catch (e.g. during the
    ``async with httpx.AsyncClient`` setup) or an unexpected normal
    return would otherwise kill the task silently and leave the chat
    view permanently desynced from the running terminal. This
    supervisor restarts the forwarder with bounded exponential
    backoff so a transient crash recovers without operator action.

    Cancellation is honored: :class:`asyncio.CancelledError` exits
    the loop cleanly so the parent's teardown sequence (terminal
    stop, bridge cleanup) runs as before. Other
    :class:`BaseException` subclasses (``KeyboardInterrupt``,
    ``SystemExit``, ``GeneratorExit``) also propagate — only
    :class:`Exception` subclasses trigger a restart, so process-
    shutdown signals are not swallowed.

    The on-disk cursor in ``bridge_dir`` is the durable source of
    truth for progress, so restarts resume exactly where the prior
    run left off — ``start_at_end`` is only consulted on a cold
    bridge with no persisted cursor.

    :param base_url: Omnigent server base URL, e.g.
        ``"http://localhost:6767"``.
    :param headers: Static HTTP headers for Omnigent requests. Authorization
        is normally supplied via ``auth`` instead so OAuth tokens are
        refreshed per request.
    :param session_id: Omnigent session/conversation id, e.g.
        ``"conv_abc123"``.
    :param bridge_dir: Native Claude bridge directory.
    :param agent_name: Agent/model name to stamp on mirrored output.
    :param start_at_end: When ``True`` and no prior forward cursor
        exists, start from the current transcript end.
    :param poll_interval_s: Seconds between transcript polls inside
        the forwarder loop. Forwarded verbatim.
    :param auth: Optional httpx Auth that mints a fresh bearer token
        per request, e.g. ``_server_auth(profile)``. Forwarded verbatim
        to :func:`forward_claude_transcript_to_session`.
    :returns: Never normally returns; cancel the task to stop it.
    """
    backoff_s = _SUPERVISOR_INITIAL_BACKOFF_S
    while True:
        run_started_at = _supervisor_monotonic()
        crash_exc: Exception | None = None
        try:
            await forward_claude_transcript_to_session(
                base_url=base_url,
                headers=headers,
                session_id=session_id,
                bridge_dir=bridge_dir,
                agent_name=agent_name,
                start_at_end=start_at_end,
                poll_interval_s=poll_interval_s,
                auth=auth,
                skip_user_messages=skip_user_messages,
            )
            # The forwarder loop is ``while True`` and is not expected
            # to return normally. Treat any normal return as a crash
            # and restart.
            _logger.warning(
                "Claude transcript forwarder returned unexpectedly; restarting; "
                "session=%s bridge_dir=%s",
                session_id,
                bridge_dir,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — supervisor restarts on any Exception
            crash_exc = exc
        run_duration_s = _supervisor_monotonic() - run_started_at
        if run_duration_s >= _SUPERVISOR_HEALTHY_UPTIME_S:
            backoff_s = _SUPERVISOR_INITIAL_BACKOFF_S
        if crash_exc is not None:
            # Log AFTER the healthy-uptime reset so the reported delay
            # matches the sleep that actually follows.
            _logger.error(
                "Claude transcript forwarder crashed; restarting in %.1fs; "
                "session=%s bridge_dir=%s",
                backoff_s,
                session_id,
                bridge_dir,
                exc_info=crash_exc,
            )
        await _supervisor_sleep(backoff_s)
        backoff_s = min(backoff_s * 2.0, _SUPERVISOR_MAX_BACKOFF_S)

async def _maybe_rotate_session_on_clear(
    *,
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
    state: HookForwardState,
) -> str | None:
    """
    Rotate the active Omnigent session when Claude reports ``/clear``.

    :param client: Omnigent HTTP client.
    :param session_id: Currently active Omnigent session id, e.g.
        ``"conv_old"``.
    :param bridge_dir: Native Claude bridge directory.
    :param state: Current hook cursor state.
    :returns: New active session id when rotation occurred, otherwise
        ``None``.
    :raises httpx.HTTPError: If Omnigent rejects the create, bind, transfer,
        or old-session clear calls.
    """
    result = await asyncio.to_thread(_read_hook_events_for_state, bridge_dir, state)
    clear_record = next(
        (
            record
            for record in result.records
            if record.event_name == "SessionStart" and record.source == "clear"
        ),
        None,
    )
    if clear_record is None:
        return None

    if clear_record.clear_rotated_to:
        new_session_id = clear_record.clear_rotated_to
    else:
        new_session_id = await _create_clear_replacement_session(
            client=client,
            old_session_id=session_id,
            bridge_dir=bridge_dir,
        )
    durable = HookForwardState(
        event_cursor=clear_record.event_cursor,
        byte_offset=clear_record.byte_offset,
        cursor_fingerprint=_jsonl_cursor_fingerprint(
            bridge_dir / _HOOKS_FILE,
            clear_record.byte_offset,
        ),
    )
    await _write_hook_state_async(bridge_dir, durable)
    reset_transcript_forward_state(bridge_dir, reset_hooks=False)
    return new_session_id

async def _seed_fork_transcript_forward_state(
    *,
    bridge_dir: Path,
    transcript_path: Path | None,
) -> None:
    """
    Seed transcript forwarding after Omnigent has forked history.

    Claude fork transcripts start with copied source-session records.
    The Omnigent fork endpoint has already copied those conversation items,
    so forwarding must begin at the current end of the new Claude
    transcript rather than replaying the copied prefix.

    :param bridge_dir: Native Claude bridge directory.
    :param transcript_path: New Claude fork transcript path, e.g.
        ``"/home/user/.claude/projects/x/session.jsonl"``. ``None``
        falls back to removing the stale cursor.
    :returns: None.
    """
    if transcript_path is None:
        reset_transcript_forward_state(bridge_dir, reset_hooks=False)
        return
    reset_transcript_forward_state(bridge_dir, reset_hooks=False)
    byte_offset = await asyncio.to_thread(_transcript_end_offset, transcript_path)
    state = TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=byte_offset,
        cursor_fingerprint=_jsonl_cursor_fingerprint(transcript_path, byte_offset),
    )
    await _write_forward_state_async(bridge_dir, state)

async def _create_clear_replacement_session(
    *,
    client: httpx.AsyncClient,
    old_session_id: str,
    bridge_dir: Path,
) -> str:
    """
    Create the fresh Omnigent session for a Claude ``/clear`` event.

    :param client: Omnigent HTTP client.
    :param old_session_id: Session being rotated away from, e.g.
        ``"conv_old"``.
    :param bridge_dir: Native Claude bridge directory.
    :returns: New Omnigent session id, e.g. ``"conv_new"``.
    :raises httpx.HTTPError: If Omnigent rejects session creation, new-session
        binding, or terminal transfer. Clearing the old runner binding is
        best-effort after the bridge has rotated.
    :raises RuntimeError: If the old session snapshot is malformed.
    """
    old = await _fetch_session_snapshot(client, old_session_id)
    agent_id = old.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        raise RuntimeError(f"session {old_session_id!r} has no agent_id")
    runner_id = old.get("runner_id")
    labels = old.get("labels") if isinstance(old.get("labels"), dict) else {}
    labels = {str(key): str(value) for key, value in labels.items()}
    labels.setdefault(BRIDGE_ID_LABEL_KEY, read_bridge_id(bridge_dir) or old_session_id)

    create_resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_id,
            "labels": labels,
        },
    )
    create_resp.raise_for_status()
    created = create_resp.json()
    new_session_id = created.get("id")
    if not isinstance(new_session_id, str) or not new_session_id:
        raise RuntimeError("clear replacement session response did not include id")

    if isinstance(runner_id, str) and runner_id:
        bind_resp = await client.patch(
            f"/v1/sessions/{url_component(new_session_id)}",
            json={"runner_id": runner_id},
        )
        bind_resp.raise_for_status()

    terminal_id = terminal_resource_id("claude", "main")
    transfer_resp = await client.post(
        (
            f"/v1/sessions/{url_component(old_session_id)}"
            f"/resources/terminals/{url_component(terminal_id)}/transfer"
        ),
        json={"target_session_id": new_session_id},
    )
    transfer_resp.raise_for_status()

    write_active_session_id(bridge_dir, new_session_id)
    clear_resp = await client.patch(
        f"/v1/sessions/{url_component(old_session_id)}",
        json={"runner_id": ""},
    )
    if clear_resp.status_code >= 400:
        _logger.warning(
            "Failed to clear old claude-native runner binding after /clear; "
            "old_session=%s new_session=%s status=%s body=%s",
            old_session_id,
            new_session_id,
            clear_resp.status_code,
            clear_resp.text,
        )
    return new_session_id

async def _maybe_rotate_session_on_fork(
    *,
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
    state: HookForwardState,
) -> str | None:
    """
    Fork the active Omnigent session when Claude reports ``/fork``/``/branch``.

    The hook annotates branch-created ``SessionStart source=resume``
    records before recording them. The forwarder consumes that
    annotation so it does not have to infer branch state after
    ``state.json`` already points at the new Claude session id.

    :param client: Omnigent HTTP client.
    :param session_id: Currently active Omnigent session id, e.g.
        ``"conv_old"``.
    :param bridge_dir: Native Claude bridge directory.
    :param state: Current hook cursor state.
    :returns: New active session id when fork rotation occurred,
        otherwise ``None``.
    :raises httpx.HTTPError: If Omnigent rejects the fork, bind, transfer,
        or old-session clear calls.
    """
    result = await asyncio.to_thread(_read_hook_events_for_state, bridge_dir, state)
    fork_record = next((record for record in result.records if _is_fork_hook_record(record)), None)
    if fork_record is None:
        return None

    if fork_record.fork_rotated_to:
        new_session_id = fork_record.fork_rotated_to
    else:
        new_session_id = await _create_fork_replacement_session(
            client=client,
            old_session_id=session_id,
            bridge_dir=bridge_dir,
        )
    durable = HookForwardState(
        event_cursor=fork_record.event_cursor,
        byte_offset=fork_record.byte_offset,
        cursor_fingerprint=_jsonl_cursor_fingerprint(
            bridge_dir / _HOOKS_FILE,
            fork_record.byte_offset,
        ),
    )
    await _write_hook_state_async(bridge_dir, durable)
    await _seed_fork_transcript_forward_state(
        bridge_dir=bridge_dir,
        transcript_path=fork_record.transcript_path,
    )
    return new_session_id

async def _create_fork_replacement_session(
    *,
    client: httpx.AsyncClient,
    old_session_id: str,
    bridge_dir: Path,
) -> str:
    """
    Create the forked Omnigent session for a Claude ``/fork``/``/branch``.

    :param client: Omnigent HTTP client.
    :param old_session_id: Session being forked away from, e.g.
        ``"conv_old"``.
    :param bridge_dir: Native Claude bridge directory.
    :returns: New Omnigent session id, e.g. ``"conv_fork"``.
    :raises httpx.HTTPError: If Omnigent rejects session fetch, fork,
        new-session binding, or terminal transfer. Clearing the old
        runner binding is best-effort after the bridge has rotated.
    :raises RuntimeError: If the Omnigent fork response is malformed.
    """
    old = await _fetch_session_snapshot(client, old_session_id)
    runner_id = old.get("runner_id")

    fork_resp = await client.post(
        f"/v1/sessions/{url_component(old_session_id)}/fork",
        json={},
    )
    fork_resp.raise_for_status()
    forked = fork_resp.json()
    new_session_id = forked.get("id")
    if not isinstance(new_session_id, str) or not new_session_id:
        raise RuntimeError("fork replacement session response did not include id")

    if isinstance(runner_id, str) and runner_id:
        bind_resp = await client.patch(
            f"/v1/sessions/{url_component(new_session_id)}",
            json={"runner_id": runner_id},
        )
        bind_resp.raise_for_status()

    terminal_id = terminal_resource_id("claude", "main")
    transfer_resp = await client.post(
        (
            f"/v1/sessions/{url_component(old_session_id)}"
            f"/resources/terminals/{url_component(terminal_id)}/transfer"
        ),
        json={"target_session_id": new_session_id},
    )
    transfer_resp.raise_for_status()

    write_active_session_id(bridge_dir, new_session_id)
    clear_resp = await client.patch(
        f"/v1/sessions/{url_component(old_session_id)}",
        json={"runner_id": ""},
    )
    if clear_resp.status_code >= 400:
        _logger.warning(
            "Failed to clear old claude-native runner binding after /fork; "
            "old_session=%s new_session=%s status=%s body=%s",
            old_session_id,
            new_session_id,
            clear_resp.status_code,
            clear_resp.text,
        )
    return new_session_id


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _cost as _sib_cost
    from . import _fwd_state as _sib_fwd_state
    from . import _helpers as _sib_helpers
    from . import _hooks as _sib_hooks
    from . import _subagent as _sib_subagent
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
    for _key, _value in _sib_hooks.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_subagent.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_transcript.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
