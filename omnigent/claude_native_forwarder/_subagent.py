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

def _subagents_dir_for_transcript(transcript_path: Path) -> Path:
    """
    Resolve the on-disk ``subagents/`` directory for a Claude session.

    Claude Code writes each Task-tool sub-agent's transcript to
    ``~/.claude/projects/<encoded>/<session>/subagents/agent-*.jsonl``
    where ``<session>`` matches the parent transcript's filename stem.
    The parent transcript itself lives at
    ``~/.claude/projects/<encoded>/<session>.jsonl`` alongside that
    directory.

    :param transcript_path: Parent's transcript JSONL,
        e.g. ``"~/.claude/projects/-Users-x-repo/85a2b8ac.jsonl"``.
    :returns: Path to the parent's ``subagents/`` directory (may not
        exist yet — caller is responsible for handling the "no
        sub-agents have been spawned yet" case).
    """
    return transcript_path.parent / transcript_path.stem / "subagents"

def _read_subagent_forward_state(bridge_dir: Path) -> SubagentForwardState:
    """
    Read the sub-agent forwarder's durable cursor map.

    Returns an empty state when no file has been persisted yet (the
    first time the watcher runs for this bridge directory). Malformed
    JSON / corrupt rows are treated as empty so a botched write can't
    permanently wedge the watcher.

    :param bridge_dir: Native Claude bridge directory.
    :returns: A :class:`SubagentForwardState`, possibly empty.
    """
    try:
        raw = json.loads((bridge_dir / _SUBAGENT_STATE_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return SubagentForwardState(subagents={})
    if not isinstance(raw, dict):
        return SubagentForwardState(subagents={})
    subagents_raw = raw.get("subagents", {})
    if not isinstance(subagents_raw, dict):
        return SubagentForwardState(subagents={})
    entries: dict[str, SubagentEntry] = {}
    for subagent_id, row in subagents_raw.items():
        if not isinstance(subagent_id, str) or not isinstance(row, dict):
            continue
        child_id = row.get("child_conversation_id")
        byte_offset = row.get("byte_offset", 0)
        seen_source_ids = row.get("seen_source_ids", [])
        last_activity_ts = row.get("last_activity_ts")
        last_status = row.get("last_status")
        # Empty string is a valid parked sentinel written by
        # ``_forward_available_subagents`` after the start POST exhausts
        # its permanent-failure budget. Preserving it across restarts is
        # what keeps the parked sub-agent from being retried.
        if not isinstance(child_id, str):
            continue
        if not isinstance(byte_offset, int) or byte_offset < 0:
            byte_offset = 0
        if not isinstance(seen_source_ids, list) or not all(
            isinstance(source_id, str) for source_id in seen_source_ids
        ):
            seen_source_ids = []
        if last_activity_ts is not None and not isinstance(last_activity_ts, (int, float)):
            last_activity_ts = None
        if last_status is not None and not isinstance(last_status, str):
            last_status = None
        entries[subagent_id] = SubagentEntry(
            subagent_id=subagent_id,
            child_conversation_id=child_id,
            byte_offset=byte_offset,
            seen_source_ids=tuple(seen_source_ids),
            last_activity_ts=last_activity_ts,
            last_status=last_status,
        )
    return SubagentForwardState(subagents=entries)

def _write_subagent_forward_state(bridge_dir: Path, state: SubagentForwardState) -> None:
    """
    Write the sub-agent forwarder's cursor map to disk atomically.

    :param bridge_dir: Native Claude bridge directory.
    :param state: Cursor map to persist.
    :returns: None.
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "subagents": {
            entry.subagent_id: {
                "child_conversation_id": entry.child_conversation_id,
                "byte_offset": entry.byte_offset,
                "seen_source_ids": list(entry.seen_source_ids),
                "last_activity_ts": entry.last_activity_ts,
                "last_status": entry.last_status,
            }
            for entry in state.subagents.values()
        },
        "updated_at": time.time(),
    }
    _write_json_atomic(bridge_dir / _SUBAGENT_STATE_FILE, payload)

async def _write_subagent_forward_state_async(
    bridge_dir: Path,
    state: SubagentForwardState,
) -> None:
    """
    Persist sub-agent state without blocking the asyncio event loop.

    :param bridge_dir: Native Claude bridge directory.
    :param state: Cursor map to persist.
    :returns: None.
    """
    await asyncio.to_thread(_write_subagent_forward_state, bridge_dir, state)

async def _post_external_subagent_start(
    client: httpx.AsyncClient,
    *,
    parent_session_id: str,
    subagent_id: str,
    agent_type: str,
    description: str,
    tool_use_id: str,
) -> str:
    """
    POST ``external_subagent_start`` to the Omnigent server and return the
    minted child Conversation id.

    :param client: Omnigent HTTP client.
    :param parent_session_id: Parent (claude-native) conversation id,
        e.g. ``"conv_parent987"``.
    :param subagent_id: Stable Claude-side identifier read from
        ``agent-<id>.meta.json``'s filename, e.g.
        ``"a5c7effac5a9a35ab"``.
    :param agent_type: Claude sub-agent type from the meta file,
        e.g. ``"Explore"``.
    :param description: Free-form description from the meta file,
        e.g. ``"Investigate web UI session data flow"``.
    :param tool_use_id: Parent transcript's ``Task`` tool-use block
        id this sub-agent was spawned from, e.g. ``"toolu_..."``.
    :returns: The Omnigent child conversation id, e.g. ``"conv_child456"``.
    :raises httpx.HTTPError: If the Omnigent request fails or is rejected.
    :raises KeyError: If the server response is missing
        ``child_session_id`` — indicates a server/forwarder version
        mismatch and is unrecoverable for this sub-agent.
    """
    resp = await client.post(
        f"/v1/sessions/{parent_session_id}/events",
        json={
            "type": "external_subagent_start",
            "data": {
                "subagent_id": subagent_id,
                "agent_type": agent_type,
                "description": description,
                "tool_use_id": tool_use_id,
            },
        },
    )
    resp.raise_for_status()
    body = resp.json()
    return body["child_session_id"]

def _read_subagent_meta(meta_path: Path) -> dict[str, str] | None:
    """
    Read a Claude sub-agent's ``.meta.json`` file, validating the
    fields the forwarder needs.

    Returns ``None`` (rather than raising) when the file is missing
    or malformed so the watcher can skip it gracefully and try again
    on the next tick.

    :param meta_path: Path to ``agent-<id>.meta.json``.
    :returns: A dict with string-typed ``agentType``, ``description``,
        and ``toolUseId``; or ``None`` when the file is missing /
        malformed / missing any required key.
    """
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    agent_type = raw.get("agentType")
    description = raw.get("description")
    tool_use_id = raw.get("toolUseId")
    if not isinstance(agent_type, str) or not agent_type:
        return None
    if not isinstance(description, str):
        return None
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return None
    return {
        "agentType": agent_type,
        "description": description,
        "toolUseId": tool_use_id,
    }

async def _forward_available_subagents(
    *,
    client: httpx.AsyncClient,
    parent_session_id: str,
    bridge_dir: Path,
    transcript_path: Path,
    state: SubagentForwardState,
    agent_name: str,
    start_retry_tracker: _PostRetryTracker,
    item_retry_tracker: _PostRetryTracker,
    status_retry_tracker: _PostRetryTracker,
) -> SubagentForwardState:
    """
    Discover new Claude Task-tool sub-agents on disk, mint Omnigent child
    conversations for them, tail their transcripts, and publish
    quiescence-based status.

    Idempotent across forwarder restarts: ``state`` (persisted to
    ``subagent_forwarder.json``) holds the Omnigent child id and byte
    offset for every sub-agent already seen. Sub-agents whose
    ``.meta.json`` appears for the first time are registered with AP
    via ``external_subagent_start``; sub-agents already in ``state``
    just have their ``.jsonl`` tailed forward.

    :param client: Omnigent HTTP client.
    :param parent_session_id: Parent (claude-native) conversation id.
    :param bridge_dir: Native Claude bridge directory.
    :param transcript_path: Parent's transcript JSONL — used to
        locate the sibling ``subagents/`` directory.
    :param state: Current sub-agent cursor map.
    :param agent_name: Agent/model name to stamp on mirrored items
        (mirrors the value used for the parent's transcript).
    :param start_retry_tracker: Backoff tracker for failed
        ``external_subagent_start`` POSTs (keyed by ``subagent_id``).
    :param item_retry_tracker: Backoff tracker for failed
        ``external_conversation_item`` POSTs (keyed by source id).
    :param status_retry_tracker: Backoff tracker for failed
        ``external_session_status`` POSTs (keyed by
        ``status:<child_id>``).
    :returns: Updated state with new sub-agents registered and
        existing sub-agents' cursors advanced.
    """
    subagents_dir = _subagents_dir_for_transcript(transcript_path)
    if not subagents_dir.is_dir():
        return state

    # ── Register newly-appeared sub-agents ──────────────
    # ``glob`` is sync; offload to a thread so we don't stat the
    # filesystem on the event loop.
    meta_paths = await asyncio.to_thread(lambda: sorted(subagents_dir.glob(_SUBAGENT_META_GLOB)))
    updated = state
    for meta_path in meta_paths:
        # ``agent-<id>.meta.json`` → ``<id>``
        subagent_id = meta_path.stem.removeprefix("agent-").removesuffix(".meta")
        if subagent_id in updated.subagents:
            continue
        retry_key = f"subagent_start:{subagent_id}"
        if start_retry_tracker.retry_delay_s(retry_key) is not None:
            continue
        meta = await asyncio.to_thread(_read_subagent_meta, meta_path)
        if meta is None:
            # File may be mid-write; try again on the next tick.
            continue
        try:
            child_id = await _post_external_subagent_start(
                client,
                parent_session_id=parent_session_id,
                subagent_id=subagent_id,
                agent_type=meta["agentType"],
                description=meta["description"],
                tool_use_id=meta["toolUseId"],
            )
        except httpx.HTTPError as exc:
            decision = start_retry_tracker.record_failure(retry_key, exc)
            if decision.exhausted:
                _logger.error(
                    "Dropping claude-native sub-agent after permanent HTTP failures; "
                    "parent_session=%s subagent_id=%s attempts=%s http_status=%s",
                    parent_session_id,
                    subagent_id,
                    decision.attempts,
                    _http_status_for_log(exc),
                )
                # Park this sub-agent: insert a sentinel entry so we
                # don't keep retrying. ``child_conversation_id=""``
                # is filtered out by the tail / status loops below.
                updated = SubagentForwardState(
                    subagents={
                        **updated.subagents,
                        subagent_id: SubagentEntry(
                            subagent_id=subagent_id,
                            child_conversation_id="",
                        ),
                    }
                )
                await _write_subagent_forward_state_async(bridge_dir, updated)
                continue
            _logger.warning(
                "Failed to register claude-native sub-agent; parent_session=%s "
                "subagent_id=%s attempt=%s permanent=%s next_retry_s=%.3f "
                "http_status=%s",
                parent_session_id,
                subagent_id,
                decision.attempts,
                decision.permanent,
                decision.delay_s,
                _http_status_for_log(exc),
                exc_info=True,
            )
            continue
        start_retry_tracker.clear(retry_key)
        updated = SubagentForwardState(
            subagents={
                **updated.subagents,
                subagent_id: SubagentEntry(
                    subagent_id=subagent_id,
                    child_conversation_id=child_id,
                ),
            }
        )
        await _write_subagent_forward_state_async(bridge_dir, updated)

    # ── Tail each tracked sub-agent's transcript ────────
    now = time.time()
    for subagent_id, entry in list(updated.subagents.items()):
        if not entry.child_conversation_id:
            # Parked after exhausted start retries — nothing to tail.
            continue
        jsonl_path = subagents_dir / f"agent-{subagent_id}.jsonl"
        if not jsonl_path.exists():
            continue
        # Reuse the parent-transcript parser, but pass
        # ``include_sidechains=True`` — every record in a sub-agent's
        # own ``agent-<id>.jsonl`` carries ``isSidechain: true``
        # (that's the whole point of the file's existence as a
        # separate transcript), and the parser's default ``False``
        # would strip every line and leave the child conversation
        # empty.
        result = await asyncio.to_thread(
            read_transcript_items_from_offset,
            jsonl_path,
            entry.byte_offset,
            start_line=0,
            agent_name=agent_name,
            current_response_id=None,
            include_sidechains=True,
        )
        new_entry = entry
        had_item = False
        items_failed = False
        seen_source_ids = list(entry.seen_source_ids)
        seen = set(seen_source_ids)
        for item in result.items:
            if item.source_id in seen:
                continue
            retry_key = f"subagent_item:{entry.child_conversation_id}:{item.source_id}"
            if item_retry_tracker.retry_delay_s(retry_key) is not None:
                # Try again on a later tick — leave the cursor where
                # it was so we re-read the same items.
                items_failed = True
                break
            try:
                await _post_external_conversation_item(
                    client,
                    session_id=entry.child_conversation_id,
                    item=item,
                )
            except httpx.HTTPError as exc:
                decision = item_retry_tracker.record_failure(retry_key, exc)
                if decision.exhausted:
                    _logger.error(
                        "Dropping claude-native sub-agent transcript item after "
                        "permanent HTTP failures; child=%s source_id=%s "
                        "attempts=%s http_status=%s",
                        entry.child_conversation_id,
                        item.source_id,
                        decision.attempts,
                        _http_status_for_log(exc),
                    )
                    # Skip this item and continue — alternative is to
                    # block the whole sub-agent forever on one poison
                    # record. The full transcript is still on disk if
                    # someone needs to recover it.
                    seen.add(item.source_id)
                    seen_source_ids.append(item.source_id)
                    new_entry = SubagentEntry(
                        subagent_id=entry.subagent_id,
                        child_conversation_id=entry.child_conversation_id,
                        byte_offset=entry.byte_offset,
                        seen_source_ids=_bounded_seen_source_ids(seen_source_ids),
                        last_activity_ts=new_entry.last_activity_ts,
                        last_status=new_entry.last_status,
                    )
                    updated = SubagentForwardState(
                        subagents={**updated.subagents, subagent_id: new_entry}
                    )
                    await _write_subagent_forward_state_async(bridge_dir, updated)
                    continue
                if post_may_have_been_delivered(exc):
                    # Ambiguous failure: the item may already be committed
                    # (no external-item dedup), so a retry would duplicate
                    # it. Skip rather than re-post.
                    _logger.warning(
                        "Skipping claude-native sub-agent item after an ambiguous POST "
                        "failure (may already be committed); not retrying to avoid a "
                        "duplicate; child=%s source_id=%s http_status=%s",
                        entry.child_conversation_id,
                        item.source_id,
                        _http_status_for_log(exc),
                        exc_info=True,
                    )
                    item_retry_tracker.clear(retry_key)
                    seen.add(item.source_id)
                    seen_source_ids.append(item.source_id)
                    new_entry = SubagentEntry(
                        subagent_id=entry.subagent_id,
                        child_conversation_id=entry.child_conversation_id,
                        byte_offset=entry.byte_offset,
                        seen_source_ids=_bounded_seen_source_ids(seen_source_ids),
                        last_activity_ts=new_entry.last_activity_ts,
                        last_status=new_entry.last_status,
                    )
                    updated = SubagentForwardState(
                        subagents={**updated.subagents, subagent_id: new_entry}
                    )
                    await _write_subagent_forward_state_async(bridge_dir, updated)
                    continue
                _logger.warning(
                    "Failed to forward claude-native sub-agent item; child=%s "
                    "source_id=%s attempt=%s permanent=%s next_retry_s=%.3f "
                    "http_status=%s",
                    entry.child_conversation_id,
                    item.source_id,
                    decision.attempts,
                    decision.permanent,
                    decision.delay_s,
                    _http_status_for_log(exc),
                    exc_info=True,
                )
                # Hold byte_offset where it was so the next tick
                # re-reads the failed item (and everything after).
                # ``seen_source_ids`` suppresses successfully-posted
                # earlier items locally so retry safety does not
                # depend on AP-side item-id idempotency.
                items_failed = True
                break
            item_retry_tracker.clear(retry_key)
            had_item = True
            seen.add(item.source_id)
            seen_source_ids.append(item.source_id)
            new_entry = SubagentEntry(
                subagent_id=entry.subagent_id,
                child_conversation_id=entry.child_conversation_id,
                byte_offset=entry.byte_offset,
                seen_source_ids=_bounded_seen_source_ids(seen_source_ids),
                last_activity_ts=now,
                last_status=new_entry.last_status,
            )
            updated = SubagentForwardState(subagents={**updated.subagents, subagent_id: new_entry})
            await _write_subagent_forward_state_async(bridge_dir, updated)
        # Only advance the cursor when every item this tick was
        # posted successfully (or there were no items at all).
        # Advancing past a failed item permanently skips it.
        if not items_failed and (result.byte_offset != entry.byte_offset or had_item):
            new_entry = SubagentEntry(
                subagent_id=entry.subagent_id,
                child_conversation_id=entry.child_conversation_id,
                byte_offset=result.byte_offset,
                seen_source_ids=_bounded_seen_source_ids(seen_source_ids),
                last_activity_ts=now if had_item else entry.last_activity_ts,
                last_status=entry.last_status,
            )
        elif had_item:
            # Items DID flow but a later post failed — still record
            # the activity timestamp so the status badge advances,
            # but leave byte_offset at the previous tick's value so
            # the failed items get retried.
            new_entry = SubagentEntry(
                subagent_id=entry.subagent_id,
                child_conversation_id=entry.child_conversation_id,
                byte_offset=entry.byte_offset,
                seen_source_ids=_bounded_seen_source_ids(seen_source_ids),
                last_activity_ts=now,
                last_status=entry.last_status,
            )

        # Quiescence-based status. Sub-agent transcripts don't carry
        # an explicit "done" record (Claude doesn't expose one), so
        # we infer "running" from item flow and "idle" from quiet
        # time. The dedupe on ``last_status`` avoids spamming the
        # cache on every tick when nothing changed.
        desired_status: str | None = None
        if had_item:
            desired_status = "running"
        elif (
            new_entry.last_activity_ts is not None
            and now - new_entry.last_activity_ts > _SUBAGENT_IDLE_QUIESCENCE_S
            and new_entry.last_status != "idle"
        ):
            desired_status = "idle"
        if desired_status is not None and desired_status != new_entry.last_status:
            retry_key = f"subagent_status:{entry.child_conversation_id}"
            if status_retry_tracker.retry_delay_s(retry_key) is None:
                try:
                    await _post_external_session_status(
                        client,
                        session_id=entry.child_conversation_id,
                        status=desired_status,
                    )
                except httpx.HTTPError as exc:
                    decision = status_retry_tracker.record_failure(retry_key, exc)
                    _logger.warning(
                        "Failed to forward claude-native sub-agent status; "
                        "child=%s status=%s attempt=%s next_retry_s=%.3f "
                        "http_status=%s",
                        entry.child_conversation_id,
                        desired_status,
                        decision.attempts,
                        decision.delay_s,
                        _http_status_for_log(exc),
                        exc_info=True,
                    )
                else:
                    status_retry_tracker.clear(retry_key)
                    new_entry = SubagentEntry(
                        subagent_id=new_entry.subagent_id,
                        child_conversation_id=new_entry.child_conversation_id,
                        byte_offset=new_entry.byte_offset,
                        seen_source_ids=new_entry.seen_source_ids,
                        last_activity_ts=new_entry.last_activity_ts,
                        last_status=desired_status,
                    )

        if new_entry is not entry:
            updated = SubagentForwardState(subagents={**updated.subagents, subagent_id: new_entry})
            await _write_subagent_forward_state_async(bridge_dir, updated)

    return updated

def _is_subagent_hook_record(record: ClaudeHookRecord) -> bool:
    """
    Return whether a hook record originated from a Claude subagent.

    Claude Code subagent transcripts live under a ``subagents/``
    subdirectory (e.g.
    ``~/.claude/projects/<encoded>/<session>/subagents/agent-<id>.jsonl``).
    When a subagent fires a lifecycle hook (``Stop``,
    ``UserPromptSubmit``), its ``transcript_path`` contains that
    ``subagents`` component. The parent process's transcript lives
    one level up (``<session>.jsonl``) and never contains it.

    :param record: Claude hook record read from ``hooks.jsonl``.
    :returns: ``True`` when the record's transcript path indicates a
        subagent, ``False`` otherwise (including when no transcript
        path is available — conservative default so parent events
        are never accidentally dropped).
    """
    if record.transcript_path is None:
        return False
    return "subagents" in record.transcript_path.parts


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _cost as _sib_cost
    from . import _fwd_state as _sib_fwd_state
    from . import _helpers as _sib_helpers
    from . import _hooks as _sib_hooks
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
    for _key, _value in _sib_hooks.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_supervisor.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_transcript.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
