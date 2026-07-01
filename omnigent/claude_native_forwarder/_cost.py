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

def _cumulative_cost_from_status_state(state: dict[str, Any] | None) -> float | None:
    """
    Extract Claude Code's cumulative session cost from a statusLine snapshot.

    :param state: Parsed ``context.json`` payload from
        :func:`read_claude_context_state`, or ``None`` when none captured
        yet.
    :returns: ``state["total_cost_usd"]`` as a non-negative float, or
        ``None`` when absent / malformed. This is the authoritative
        whole-session total — it includes Task sub-agent spend once Claude
        Code settles it — but lags while a sub-agent is still running.
    """
    if not isinstance(state, dict):
        return None
    raw = state.get("total_cost_usd")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if raw < 0:
        return None
    return float(raw)

def _transcript_cost_size_cached(
    transcript_path: Path,
    *,
    include_sidechains: bool,
    cache: dict[Path, _TranscriptCostCacheEntry],
) -> float | None:
    """
    Cumulative transcript cost, recomputed only when the file grows.

    Wraps :func:`compute_transcript_cumulative_cost` with a per-process
    size-keyed cache so an unchanged transcript isn't re-parsed every
    poll. On a forwarder restart the cache starts empty and the first
    call recomputes from the full file, so the estimate is correct across
    restarts (unlike an in-memory running sum, which would lose the
    pre-restart portion).

    :param transcript_path: Transcript JSONL path.
    :param include_sidechains: Forwarded to
        :func:`compute_transcript_cumulative_cost` — ``False`` for a
        parent transcript (sub-agent records are sidechains counted
        elsewhere), ``True`` for a sub-agent's own transcript.
    :param cache: Per-session cache mapping transcript path to its last
        computed :class:`_TranscriptCostCacheEntry`. Mutated in place.
    :returns: Cumulative USD cost, or ``None`` when nothing is priceable
        (missing file included).
    """
    try:
        size = transcript_path.stat().st_size
    except OSError:
        return None
    cached = cache.get(transcript_path)
    if cached is not None and cached.size == size:
        return cached.cost_usd
    cost = compute_transcript_cumulative_cost(
        transcript_path, include_sidechains=include_sidechains
    )
    cache[transcript_path] = _TranscriptCostCacheEntry(size=size, cost_usd=cost)
    return cost

def _session_cost_estimate(
    *,
    parent_transcript_path: Path,
    active_subagents: list[SubagentEntry],
    status_cost: float | None,
    cost_cache: dict[Path, _TranscriptCostCacheEntry],
) -> float | None:
    """
    Compute ``max(S, C)`` for the parent session's POLICY/budget cost.

    This is the value the cost-budget gate reads (``policy_cost_usd``),
    NOT the displayed cost — display uses ``S`` alone so the badge matches
    the Claude TUI ``/cost``. Synchronous (does transcript file I/O) —
    call via :func:`asyncio.to_thread`. ``C`` is the forwarder's real-time
    estimate: the parent transcript's own cost (sidechains excluded) plus
    the sum of each tracked sub-agent's own transcript cost (each priced
    once per ``requestId`` — see
    :func:`compute_transcript_cumulative_cost`). ``S`` is the statusLine
    total. See :func:`_forward_session_cost` for why the two are combined
    with ``max`` rather than added.

    :param parent_transcript_path: Parent transcript JSONL path; its
        sibling ``subagents/`` directory holds the sub-agent transcripts.
    :param active_subagents: Sub-agents with a minted child conversation
        (only these have an ``agent-<id>.jsonl`` on disk to price).
    :param status_cost: ``S`` — the statusLine total, or ``None`` when
        not captured yet.
    :param cost_cache: Per-session size-keyed transcript cost cache,
        mutated in place.
    :returns: ``max(S, C)`` in USD, or ``None`` when neither source
        yields a priceable cost.
    """
    subagents_dir = _subagents_dir_for_transcript(parent_transcript_path)
    estimate: float | None = _transcript_cost_size_cached(
        parent_transcript_path, include_sidechains=False, cache=cost_cache
    )
    for entry in active_subagents:
        jsonl_path = subagents_dir / f"agent-{entry.subagent_id}.jsonl"
        sub_cost = _transcript_cost_size_cached(
            jsonl_path, include_sidechains=True, cache=cost_cache
        )
        if sub_cost is not None:
            # Seed the accumulator from the parent cost, or 0.0 when the parent
            # had nothing priceable — so sub-agent cost still contributes to C.
            estimate = (estimate or 0.0) + sub_cost
    candidates = [cost for cost in (status_cost, estimate) if cost is not None]
    if not candidates:
        return None
    return max(candidates)

async def _forward_session_cost(
    *,
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
    parent_transcript_path: Path,
    subagent_state: SubagentForwardState,
    dedupe: _ForwardDedupeState,
    cost_cache: dict[Path, _TranscriptCostCacheEntry],
) -> None:
    """
    POST the parent session's cost as TWO values: display and policy.

    The parent session's cost-budget policy gates EVERY tool call in the
    Claude process — including a Task sub-agent's, whose ``PreToolUse``
    hook the runner evaluates against this parent session (the bridge has
    one active session id; there is no per-sub-agent policy routing). But
    Claude Code's statusLine ``total_cost_usd`` (``S``) is **frozen for
    the entire duration of a sub-agent run** — the statusLine isn't even
    invoked while a sub-agent runs; ``S`` jumps to the sub-agent-inclusive
    total only when the sub-agent returns (verified live). So a value
    based on ``S`` alone can't gate a runaway sub-agent mid-turn.

    Display and enforcement therefore need different numbers, posted as
    two separate fields the server persists independently:

    - ``cumulative_cost_usd`` = ``S`` verbatim — the DISPLAY cost. The
      parent badge then matches ``/cost`` in the Claude TUI exactly (``S``
      is Claude's own billing and already includes sub-agent spend once
      settled). It is frozen during a sub-agent run; that's acceptable
      for display.
    - ``policy_cost_usd`` = ``max(S, C)`` — the POLICY/budget cost. ``C``
      is the forwarder's real-time estimate (parent transcript own
      messages + each tracked sub-agent's transcript, each priced once
      per ``requestId``). ``C`` advances while ``S`` is frozen, so the
      gate sees in-flight sub-agent spend and can block mid-turn. With no
      sub-agent there is no lag, so it equals ``S``.

    The brief intra-turn divergence (badge shows frozen ``S`` while the
    gate uses the higher live ``C``) is intentional and reconciles at the
    turn boundary when ``S`` jumps; ``max`` keeps both monotonic.

    Best-effort, like the other forwarder posts: a failed POST is retried
    on the next poll (the ``dedupe`` baselines advance only on success).

    :param client: Omnigent HTTP client.
    :param session_id: Parent (claude-native) conversation id the cost is
        attributed to, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Claude bridge directory (holds the
        statusLine snapshot read for ``S``).
    :param parent_transcript_path: Parent transcript JSONL path — used for
        the ``C`` estimate and to locate the ``subagents/`` directory.
    :param subagent_state: Current sub-agent cursor map; its tracked
        sub-agents' transcripts contribute to ``C``.
    :param dedupe: Carries ``posted_cost`` (display ``S``) and
        ``posted_policy_cost`` (``max(S, C)``) so steady values aren't
        re-POSTed each poll; mutated in place on a successful post.
    :param cost_cache: Per-session size-keyed transcript cost cache,
        mutated in place.
    :returns: None.
    """
    status_state = await asyncio.to_thread(read_claude_context_state, bridge_dir)
    status_cost = _cumulative_cost_from_status_state(status_state)
    active_subagents = [
        entry for entry in subagent_state.subagents.values() if entry.child_conversation_id
    ]
    # Display cost: the statusLine total S verbatim (matches /cost).
    display_cost = status_cost
    # Policy/budget cost: with no sub-agent it equals S; with a sub-agent
    # running it is max(S, real-time transcript estimate) so the gate sees
    # in-flight spend while S is frozen.
    if not active_subagents:
        policy_cost = status_cost
    else:
        policy_cost = await asyncio.to_thread(
            _session_cost_estimate,
            parent_transcript_path=parent_transcript_path,
            active_subagents=active_subagents,
            status_cost=status_cost,
            cost_cache=cost_cache,
        )
    # Build the payload from whichever values are present AND have advanced.
    # Monotonic per field: never walk a total backwards — guards a transient
    # lower transcript read (e.g. just after a rotation) and suppresses
    # steady-state churn. The two fields advance independently (policy_cost
    # moves mid-turn while display_cost/S is frozen).
    payload: dict[str, float] = {}
    if display_cost is not None and (
        dedupe.posted_cost is None or display_cost > dedupe.posted_cost
    ):
        payload["cumulative_cost_usd"] = display_cost
    if policy_cost is not None and (
        dedupe.posted_policy_cost is None or policy_cost > dedupe.posted_policy_cost
    ):
        payload["policy_cost_usd"] = policy_cost
    if not payload:
        return
    try:
        await _post_external_session_usage(
            client,
            session_id=session_id,
            usage=payload,
        )
    except httpx.HTTPError as exc:
        _logger.warning(
            "Failed to forward Claude session cost; session=%s bridge_dir=%s http_status=%s",
            session_id,
            bridge_dir,
            _http_status_for_log(exc),
            exc_info=True,
        )
        return
    if "cumulative_cost_usd" in payload:
        dedupe.posted_cost = display_cost
    if "policy_cost_usd" in payload:
        dedupe.posted_policy_cost = policy_cost


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _fwd_state as _sib_fwd_state
    from . import _helpers as _sib_helpers
    from . import _hooks as _sib_hooks
    from . import _subagent as _sib_subagent
    from . import _supervisor as _sib_supervisor
    from . import _transcript as _sib_transcript
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
    for _key, _value in _sib_transcript.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
