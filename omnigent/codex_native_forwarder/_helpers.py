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

def _terminal_turn_status_edge(
    bridge_dir: Path,
    method: str,
    params: dict[str, Any],
) -> _CodexTurnStatusEdge | None:
    """
    Return the terminal Omnigent edge for a Codex terminal turn event.

    The edge is produced when the event clears the recorded active turn, or
    when it safely recovers a missed ``turn/started`` for the bridge's current
    thread. Stale or ambiguous terminal events return ``None``.

    :param bridge_dir: Native Codex bridge directory.
    :param method: Codex terminal method, e.g. ``"turn/completed"``.
    :param params: Codex turn event params.
    :returns: Terminal status edge, or ``None`` when the event is stale.
    """
    terminal_turn_id = _terminal_turn_id_from_params(params)
    if not clear_active_turn_id_if_matches(bridge_dir, terminal_turn_id):
        if not _terminal_turn_boundary_matches_idle_bridge(bridge_dir, params, terminal_turn_id):
            return None
        source = f"{method}:recovered"
    else:
        source = method
    return _CodexTurnStatusEdge(
        status="idle" if method == "turn/completed" else "failed",
        turn_id=terminal_turn_id,
        source=source,
    )

def _terminal_turn_id_from_params(params: dict[str, Any]) -> str | None:
    """
    Extract the terminal turn id from Codex turn-boundary params.

    :param params: Codex ``turn/completed`` / ``turn/failed`` params.
    :returns: Codex turn id, e.g. ``"turn_abc123"``, or ``None``.
    """
    turn = params.get("turn")
    return _turn_id_from_payload(turn) or _turn_id_from_payload(params)

def _terminal_turn_boundary_matches_idle_bridge(
    bridge_dir: Path,
    params: dict[str, Any],
    terminal_turn_id: str | None,
) -> bool:
    """
    Return whether a terminal boundary can close a missed-start turn.

    A Codex listener can miss ``turn/started`` while reconnecting. If no
    active turn is recorded, but a later ``turn/completed`` / ``turn/failed``
    event carries the current thread id, the forwarder may safely publish the
    terminal status edge. If another active turn is recorded, the event is
    stale or ambiguous and must stay ignored.

    :param bridge_dir: Native Codex bridge directory.
    :param params: Codex terminal turn event params.
    :param terminal_turn_id: Terminal turn id from the event, e.g.
        ``"turn_abc123"``.
    :returns: ``True`` when the event belongs to the bridge's current idle
        thread and can publish the terminal status edge.
    """
    if terminal_turn_id is None:
        return False
    state = read_bridge_state(bridge_dir)
    if state is None or state.active_turn_id is not None:
        return False
    return _thread_id_from_params(params) == state.thread_id

def _parent_session_id_from_forwarder_state(
    forwarder_state: _CodexForwarderState,
) -> str | None:
    """
    Return the parent Omnigent session id stored on the forwarder state.

    Set by ``supervise_forwarder`` when the loop starts. Returns ``None``
    when called from a context that did not set a parent session (e.g.
    direct handler tests that bypass ``supervise_forwarder``).

    :param forwarder_state: Mutable forwarder state.
    :returns: Parent session id, e.g. ``"conv_parent"``, or ``None``.
    """
    return forwarder_state.parent_session_id

def _session_usage_data_from_params(params: dict[str, Any]) -> dict[str, int] | None:
    """
    Extract Omnigent session-usage fields from a Codex usage notification.

    :param params: Codex ``thread/tokenUsage/updated`` params.
    :returns: A dict with any of ``context_tokens`` / ``context_window``
        (context ring), ``cumulative_input_tokens`` /
        ``cumulative_output_tokens`` /
        ``cumulative_cache_read_input_tokens`` (priced into session cost by
        the server), or ``None`` when the notification has no usable usage
        values.
    """
    token_usage = params.get("tokenUsage")
    if not isinstance(token_usage, dict):
        return None
    total = token_usage.get("total")
    if not isinstance(total, dict):
        return None
    cumulative_input_tokens = total.get("inputTokens")
    context_window = total.get("contextWindow")
    output_tokens = total.get("outputTokens")
    cached_input_tokens = total.get("cachedInputTokens")
    data: dict[str, int] = {}
    if isinstance(cumulative_input_tokens, int) and cumulative_input_tokens >= 0:
        # Codex's ``tokenUsage.total`` is CUMULATIVE across the whole thread
        # (the CLI subtracts prior totals to recover per-turn deltas), so
        # ``total.inputTokens`` / ``outputTokens`` are the session's cumulative
        # token counts. Forward them as the cumulative fields the server prices
        # into ``total_cost_usd`` (SET semantics) — codex-native produces no
        # ``response.completed``, so the Omnigent relay never accounts its cost.
        data["cumulative_input_tokens"] = cumulative_input_tokens
        # Codex's ``inputTokens`` is INCLUSIVE of cached tokens
        # (``non_cached_input = input_tokens - cached_input_tokens`` in
        # codex-rs ``protocol.rs``). Forward the cumulative cached count so the
        # server can price the cached portion at the (cheaper) cache-read rate
        # instead of billing the whole input at the full input rate. Same
        # cumulative (SET) semantics as ``cumulative_input_tokens``.
        if isinstance(cached_input_tokens, int) and cached_input_tokens >= 0:
            data["cumulative_cache_read_input_tokens"] = cached_input_tokens
    # ``context_tokens`` drives the context-window ring in the web UI. It
    # must reflect the CURRENT context occupancy (how much of the window
    # the latest turn consumed), NOT the cumulative total across all turns.
    # Codex's ``tokenUsage.last`` carries the per-turn breakdown; fall back
    # to ``total.inputTokens`` only when ``last`` is unavailable (first
    # frame before a turn completes).
    last = token_usage.get("last")
    last_input = last.get("inputTokens") if isinstance(last, dict) else None
    if isinstance(last_input, int) and last_input >= 0:
        data["context_tokens"] = last_input
    elif isinstance(cumulative_input_tokens, int) and cumulative_input_tokens >= 0:
        data["context_tokens"] = cumulative_input_tokens
    if isinstance(output_tokens, int) and output_tokens >= 0:
        data["cumulative_output_tokens"] = output_tokens
    if isinstance(context_window, int) and context_window > 0:
        data["context_window"] = context_window
    if not data:
        return None
    return data

def _is_final_post_attempt(attempt: int) -> bool:
    """
    Return whether an Omnigent event POST attempt is the final try.

    :param attempt: One-based attempt number, e.g. ``3``.
    :returns: ``True`` when no further retry is allowed.
    """
    return attempt >= _POST_MAX_ATTEMPTS


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _collab as _sib_collab
    from . import _deltas as _sib_deltas
    from . import _elicitation as _sib_elicitation
    from . import _events as _sib_events
    from . import _fwd_state as _sib_fwd_state
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
    for _key, _value in _sib_fwd_state.__dict__.items():
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
