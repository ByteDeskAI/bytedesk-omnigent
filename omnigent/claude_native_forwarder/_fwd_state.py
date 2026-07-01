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

@dataclass(frozen=True)
class HookForwardState:
    """
    Durable cursor for the hooks-to-status forwarder.

    :param event_cursor: One-based hook record index already
        forwarded. ``0`` means no hook events have been forwarded yet.
    :param byte_offset: Byte offset already forwarded. ``None`` means
        the state was written by an older line-cursor-only forwarder
        and must be migrated with one compatibility scan.
    :param cursor_fingerprint: Hash of bytes immediately before
        ``byte_offset``. Used to detect truncation/replacement before
        seeking into a stale offset.
    """

    event_cursor: int
    byte_offset: int | None = None
    cursor_fingerprint: str | None = None

@dataclass(frozen=True)
class SubagentEntry:
    """
    Per-sub-agent forwarder cursor.

    One of these per Claude-side sub-agent. Tracks the Omnigent child
    Conversation id we minted (so subsequent items POST to the
    right session), the transcript file byte offset already
    forwarded, and the wall-clock timestamp of the last item we
    saw (for the idle-status heuristic).

    :param subagent_id: Stable Claude-side identifier, also the
        ``agent-<id>`` filename stem, e.g. ``"a5c7effac5a9a35ab"``.
    :param child_conversation_id: Omnigent child Conversation id minted
        by the server's ``external_subagent_start`` handler,
        e.g. ``"conv_child456"``.
    :param byte_offset: Bytes already forwarded from the sub-agent's
        ``.jsonl``. ``0`` means we haven't read anything yet (the
        common case when the sub-agent has just been created).
    :param seen_source_ids: Recently-posted transcript item source
        ids for this child. Preserved separately from ``byte_offset``
        so a failed later item can leave the cursor behind without
        re-posting earlier accepted items on the next poll.
    :param last_activity_ts: Unix timestamp of the most recent item
        observed in this sub-agent's transcript. Used by the idle
        heuristic — when ``now - last_activity_ts >
        _SUBAGENT_IDLE_QUIESCENCE_S`` we publish an
        ``external_session_status: idle`` event. ``None`` when no
        items have been seen yet (so the heuristic doesn't fire
        before there's anything to be quiescent about).
    :param last_status: Last status string POSTed for this
        sub-agent — used to dedupe so we don't spam ``running`` or
        ``idle`` events on every tick when nothing changed. ``None``
        means no status has been posted yet.
    """

    subagent_id: str
    child_conversation_id: str
    byte_offset: int = 0
    seen_source_ids: tuple[str, ...] = ()
    last_activity_ts: float | None = None
    last_status: str | None = None

@dataclass(frozen=True)
class SubagentForwardState:
    """
    Durable cursor map for the claude-native sub-agent forwarder.

    Persisted at ``{bridge_dir}/subagent_forwarder.json`` so a
    forwarder restart picks up where we left off — re-reading the
    on-disk ``subagents/`` directory and posting only items past
    each tracked sub-agent's ``byte_offset``.

    :param subagents: Map from Claude-side ``subagent_id`` to the
        per-sub-agent entry. New sub-agents discovered on disk are
        inserted here after the Omnigent server returns a child
        Conversation id.
    """

    subagents: dict[str, SubagentEntry]

@dataclass(frozen=True)
class TranscriptForwardState:
    """
    Durable cursor for a Claude transcript forwarder.

    :param transcript_path: Transcript JSONL file whose cursor was
        recorded.
    :param line_cursor: One-based line cursor already forwarded into
        AP. ``0`` means no lines from the current transcript have
        been forwarded yet.
    :param byte_offset: Transcript byte offset already forwarded.
        ``None`` means the state was written by an older
        line-cursor-only forwarder and must be migrated with one
        compatibility scan.
    :param current_response_id: Response id for a Claude assistant
        turn that spans multiple forwarder polls.
    :param seen_source_ids: Recently-posted transcript item source
        ids. This makes retries and restarts idempotent even if the
        line cursor was not advanced before a cancellation.
    :param cursor_fingerprint: Hash of bytes immediately before
        ``byte_offset``. Used to detect truncation/replacement before
        seeking into a stale offset.
    """

    transcript_path: Path
    line_cursor: int
    byte_offset: int | None = None
    current_response_id: str | None = None
    seen_source_ids: tuple[str, ...] = ()
    cursor_fingerprint: str | None = None

@dataclass(frozen=True)
class DeltaForwardState:
    """
    Durable cursor for the assistant-text delta forwarder.

    Tracks the byte offset already consumed from
    ``<bridge_dir>/message_deltas.jsonl``. Unlike the transcript cursor
    this is NOT tied to a transcript path and is NOT reset on
    ``/clear`` / ``/fork``: the deltas file belongs to the long-lived
    Claude process and keeps growing across Omnigent session rotations, so the
    offset stays monotonic and each new chunk is forwarded to whatever
    Omnigent session is active when it is read.

    :param byte_offset: Byte offset after the last forwarded chunk.
        ``0`` means nothing has been forwarded yet.
    """

    byte_offset: int = 0

@dataclass
class _ForwardDedupeState:
    """
    Last values the forwarder POSTed, kept to suppress duplicate
    ``external_*`` events when Claude rewrites the same block each poll.

    Mutated in place by :func:`_forward_available_items` so the run loop
    carries the dedupe baseline across polls without threading a
    positional tuple back out. Reset on ``/clear`` and ``/fork``
    rotations alongside the other per-session state.

    :param usage: Last ``message.usage`` snapshot POSTed via
        ``external_session_usage``, or ``None`` if none yet.
    :param context_window: Last context-window POSTed, or ``None``.
    :param observed_model: Last tier alias seen in the transcript,
        sticky across polls (the incremental window often carries no
        fresh ``message.model``), e.g. ``"opus"``. ``None`` until first
        seen.
    :param posted_model: Last tier alias POSTed via
        ``external_model_change``. Seeded from the first observation
        WITHOUT a POST so a passive spawn default never overwrites a
        pending silent model handoff; only a later in-TUI switch is
        mirrored. Left behind ``observed_model`` on a failed POST so the
        next poll retries. ``None`` until the first observation.
    :param posted_cost: Last DISPLAY cost (USD) POSTed as
        ``cumulative_cost_usd`` — the statusLine total ``S`` verbatim.
        ``None`` until the first cost post. Used to dedupe so a steady
        cost isn't re-POSTed every poll.
    :param posted_policy_cost: Last POLICY/budget cost (USD) POSTed as
        ``policy_cost_usd`` — ``max(S, transcript estimate)``, the
        real-time figure the cost-budget gate reads. Tracked separately
        from ``posted_cost`` because it advances mid-turn (with in-flight
        sub-agent spend) while ``S`` stays frozen. ``None`` until first
        post.
    """

    usage: dict[str, float] | None = None
    context_window: int | None = None
    observed_model: str | None = None
    posted_model: str | None = None
    # Last DISPLAY cost (USD) POSTed as ``cumulative_cost_usd`` — the
    # statusLine total ``S`` verbatim (matches /cost in the Claude TUI).
    # Kept to suppress duplicate posts when S hasn't advanced.
    posted_cost: float | None = None
    # Last POLICY/budget cost (USD) POSTed as ``policy_cost_usd`` —
    # ``max(S, forwarder transcript estimate)``, which reflects in-flight
    # sub-agent spend so the gate can block mid-turn. Separate baseline
    # because it can advance while ``posted_cost`` (S) is frozen.
    posted_policy_cost: float | None = None

@dataclass(frozen=True)
class _TranscriptCostCacheEntry:
    """
    Cached cumulative-cost computation for one transcript file.

    The cost is recomputed only when the file's byte size changes, so the
    forwarder doesn't re-parse an unchanged transcript on every (0.25s)
    poll. Append-only JSONL makes byte size a sound cache key.

    :param size: File size in bytes when ``cost_usd`` was computed,
        e.g. ``81920``.
    :param cost_usd: Cumulative USD cost computed from the transcript at
        that size, or ``None`` when nothing could be priced.
    """

    size: int
    cost_usd: float | None

@dataclass
class _PostRetryEntry:
    """
    In-memory retry state for one outbound Omnigent event.

    :param attempts: Number of failed post attempts observed.
    :param next_attempt_at: Monotonic timestamp before which the
        forwarder should not retry this event.
    """

    attempts: int = 0
    next_attempt_at: float = 0.0

@dataclass(frozen=True)
class _PostRetryDecision:
    """
    Result of recording one outbound Omnigent post failure.

    :param attempts: Number of failed attempts for this event after
        the current failure.
    :param delay_s: Seconds until the next retry should be attempted.
    :param exhausted: Whether a permanent failure exceeded the retry
        budget and the cursor should advance past the event.
    :param permanent: Whether the failure is classified as a
        permanent HTTP rejection.
    """

    attempts: int
    delay_s: float
    exhausted: bool
    permanent: bool

class _PostRetryTracker:
    """
    Track bounded retries and backoff for Omnigent event posts.

    Permanent 4xx-style HTTP rejections are retried a small number of
    times before the forwarder marks the item failed and advances the
    cursor. Transient HTTP/network failures keep retrying with
    backoff so Omnigent outages do not silently drop transcript data.

    This is not a :mod:`tenacity` wrapper because retry attempts must
    be interleaved with durable cursor writes and the forwarder's poll
    loop. Sleeping inside a decorator would block unrelated hook/item
    work behind one poisoned event.
    """

    def __init__(
        self,
        *,
        max_permanent_attempts: int = _HTTP_POST_MAX_PERMANENT_FAILURES,
        base_delay_s: float = _HTTP_POST_RETRY_BASE_DELAY_S,
        max_delay_s: float = _HTTP_POST_RETRY_MAX_DELAY_S,
    ) -> None:
        """
        Initialize an empty retry tracker.

        :param max_permanent_attempts: Attempts before a permanent
            failure is exhausted.
        :param base_delay_s: Initial retry delay in seconds.
        :param max_delay_s: Maximum retry delay in seconds.
        :returns: None.
        """
        self._max_permanent_attempts = max(1, max_permanent_attempts)
        self._base_delay_s = max(0.0, base_delay_s)
        self._max_delay_s = max(0.0, max_delay_s)
        self._entries: dict[str, _PostRetryEntry] = {}

    def retry_delay_s(self, key: str) -> float | None:
        """
        Return remaining delay for ``key`` if a retry is not due yet.

        :param key: Stable retry key, e.g. ``"item:source-1"``.
        :returns: Remaining seconds to wait, or ``None`` when the
            caller may attempt the post now.
        """
        entry = self._entries.get(key)
        if entry is None:
            return None
        remaining = entry.next_attempt_at - time.monotonic()
        if remaining <= 0:
            return None
        return remaining

    def clear(self, key: str) -> None:
        """
        Remove retry state for a successfully handled event.

        :param key: Stable retry key, e.g. ``"hook:2:idle"``.
        :returns: None.
        """
        self._entries.pop(key, None)

    def record_failure(self, key: str, exc: httpx.HTTPError) -> _PostRetryDecision:
        """
        Record one failed post and compute the next retry action.

        :param key: Stable retry key, e.g. ``"item:source-1"``.
        :param exc: HTTP exception raised while posting the event.
        :returns: Retry decision for this failure.
        """
        entry = self._entries.get(key)
        if entry is None:
            entry = _PostRetryEntry()
            self._entries[key] = entry
        entry.attempts += 1
        permanent = _is_permanent_http_error(exc)
        if permanent and entry.attempts >= self._max_permanent_attempts:
            self._entries.pop(key, None)
            return _PostRetryDecision(
                attempts=entry.attempts,
                delay_s=0.0,
                exhausted=True,
                permanent=True,
            )
        delay_s = min(
            self._base_delay_s * (2 ** max(0, entry.attempts - 1)),
            self._max_delay_s,
        )
        entry.next_attempt_at = time.monotonic() + delay_s
        return _PostRetryDecision(
            attempts=entry.attempts,
            delay_s=delay_s,
            exhausted=False,
            permanent=permanent,
        )


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _cost as _sib_cost
    from . import _helpers as _sib_helpers
    from . import _hooks as _sib_hooks
    from . import _subagent as _sib_subagent
    from . import _supervisor as _sib_supervisor
    from . import _transcript as _sib_transcript
    for _key, _value in _sib_cost.__dict__.items():
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
    for _key, _value in _sib_transcript.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
