"""Bridge utilities for the native Claude Code wrapper.

The native wrapper has two live processes that need to rendezvous:

- Claude Code, running in the user's terminal resource.
- The Omnigent harness turn, running when the web UI submits a
  message to the session agent.

This module owns the small filesystem rendezvous directory plus two
helper surfaces:

- An MCP stdio server (``serve-mcp`` subcommand) that Claude Code
  launches as a child process. It advertises Omnigent tools to
  Claude (workspace ``sys_os_*`` tools outside an active turn,
  active-turn Omnigent tools via a per-turn relay).
- A tmux send-keys path. Web UI messages are delivered to Claude by
  typing them into the same tmux pane the user is attached to;
  Claude treats them as ordinary user input. The runner advertises
  the pane's socket + target in ``tmux.json`` after launching the
  ``claude/main`` terminal.

Claude's experimental Channels MCP capability was the original input
path but is blocked at the org policy layer, so this bridge does not
use it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import queue
import re
import secrets
import shlex
import stat
import sys
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib import error, request

from omnigent.claude_native_message_display_hook import MESSAGE_DELTAS_FILE

if TYPE_CHECKING:
    from omnigent.llms.context_window import ModelPricing

from omnigent.inner.bundle_skills import claude_native_skill_args
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import OSEnvironment, create_os_environment
from omnigent.reasoning_effort import CLAUDE_EFFORTS
from omnigent.tools.base import Tool, ToolContext
from omnigent.tools.builtins.os_env import build_os_env_tools

BRIDGE_DIR_ENV_VAR = "HARNESS_CLAUDE_NATIVE_BRIDGE_DIR"
REQUEST_SESSION_ID_ENV_VAR = "HARNESS_CLAUDE_NATIVE_REQUEST_SESSION_ID"
BRIDGE_ID_LABEL_KEY = "omnigent.claude_native.bridge_id"

# Root for the per-process Claude bridge tree. Namespaced by uid so
# other Unix users on the same host cannot read the bearer token or
# pre-create the parent as a symlink to redirect the bridge tree. The
# trusted parent (`/tmp`) is shared; everything under
# `_BRIDGE_ROOT_PARENT` must be owned by the current uid and not be a
# symlink — see :func:`_ensure_secure_dir`.
_TRUSTED_PARENT = Path("/tmp")
_BRIDGE_ROOT_PARENT = _TRUSTED_PARENT / f"omnigent-{os.getuid()}"
_BRIDGE_ROOT = _BRIDGE_ROOT_PARENT / "claude-native"
_CONFIG_FILE = "bridge.json"
_SERVER_FILE = "server.json"
_STATE_FILE = "state.json"
_HOOKS_FILE = "hooks.jsonl"
_RECENT_LOCAL_COMMAND_LINE_LIMIT = 200
_RECENT_LOCAL_COMMAND_WINDOW_S = 10.0
_FORKED_FROM_LINE_LIMIT = 200
_TOOL_RELAY_FILE = "tool_relay.json"
_TMUX_FILE = "tmux.json"
_PERMISSION_HOOK_FILE = "permission_hook.json"
_CONTEXT_FILE = "context.json"
_USER_CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
_MCP_SERVER_NAME = "omnigent"
_MCP_PROTOCOL_VERSION = "2024-11-05"
# Tools-changed: harness POSTs to the bridge MCP server's localhost
# control endpoint, which emits ``notifications/tools/list_changed``
# on its MCP stdout. Standard MCP notification — unrelated to the
# experimental Claude Channels feature that this module no longer
# uses.
_TOOLS_CHANGED_READY_TIMEOUT_S = 30.0
_TOOLS_CHANGED_POST_TIMEOUT_S = 10.0
# Ceiling the relay HTTP handler (``_run_relay_tool``) waits for a single
# tool dispatch to complete on the harness event loop.
_TOOL_CALL_TIMEOUT_S = 300.0
# Timeout for the bridge's POST to the active-turn relay server
# (``_call_relay_tool``). This is the OUTER hop: it waits for the relay
# handler's entire ``_TOOL_CALL_TIMEOUT_S`` dispatch, which itself fans out
# to the Omnigent policy server and back. It MUST exceed ``_TOOL_CALL_TIMEOUT_S``
# so the inner handler times out first and returns a clean MCP error over
# HTTP 200 — rather than the outer ``urlopen`` raising and tearing down the
# stdio MCP server (see ``_stdio_jsonrpc_loop``). The previous flat 10s sat
# below the real round-trip latency under load, so slow-but-healthy calls
# (session history reads, shell) tripped it and crashed the bridge.
_TOOL_RELAY_POST_TIMEOUT_S = _TOOL_CALL_TIMEOUT_S + 30.0
# Web-UI → Claude input now flows through tmux send-keys, not
# Claude's experimental Channels MCP capability. The runner writes
# ``tmux.json`` after the Claude terminal launches; the harness
# tails it and shells out to tmux.
_TMUX_READY_TIMEOUT_S = 30.0
_TMUX_SEND_TIMEOUT_S = 5.0
# Claude Code renders this prompt glyph in its input box once the TUI
# is interactive. We poll ``capture-pane`` for it before injecting the
# first message so keystrokes typed during Claude's boot aren't dropped.
# The glyph persists while Claude is busy responding, so its presence
# means "input box mounted" (not "idle"), which is what injection needs.
_CLAUDE_PROMPT_GLYPH = "❯"
# How many trailing non-empty lines to scan for the prompt glyph. The
# input box sits near the bottom of the pane; scanning only the tail
# avoids false positives from the glyph appearing in scrollback output.
# The window has to clear the footer rendered below the box — some
# people's statuslines run ~3 lines — so the ``❯`` row isn't the last
# non-empty line.
_PROMPT_SCAN_TAIL_LINES = 5
_CLAUDE_READY_POLL_INTERVAL_S = 0.15
_PASTE_SETTLE_S = 0.1  # let the TUI commit a paste before the separate submit Enter
# How long to wait for the pasted draft to visibly land in Claude's
# input box before sending the submit Enter. Claude Code coalesces
# rapid stdin bursts into a paste, so an Enter sent while the TUI is
# still consuming the paste gets folded in as a newline instead of
# submitting — the draft then sits unsent. Polling for the draft makes
# the handoff deterministic where the old fixed sleep raced it.
_PASTE_COMMIT_TIMEOUT_S = 5.0
# After the submit Enter, how long to keep checking that the draft
# actually left the input box (re-sending Enter while it hasn't)
# before failing loud.
_SUBMIT_VERIFY_TIMEOUT_S = 10.0
# Minimum spacing between repeated submit Enters during verification.
# Long enough for the TUI to clear the box after a successful submit
# (so a slow-but-successful first Enter isn't double-tapped), short
# enough that a swallowed Enter is retried promptly.
_SUBMIT_RETRY_INTERVAL_S = 1.0
# Claude Code collapses large pastes into this placeholder in the
# input box instead of rendering the text itself.
_PASTED_PLACEHOLDER_PREFIX = "[Pasted text"
# How many characters of the draft's first line to use when checking
# whether the draft is rendered in the input box. Short enough to fit
# on the prompt row of a default 80-column detached pane.
_DRAFT_NEEDLE_MAX_CHARS = 24

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[Any]]


def _import_package_bindings() -> None:
    from . import _constants as _pkg_constants
    from . import _state as _pkg_state
    g = globals()
    for _mod in (_pkg_constants, _pkg_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_package_bindings()

def _transcript_model_pricing(model: str) -> ModelPricing | None:
    """
    Look up per-token pricing for *model*, memoizing successful results.

    :param model: API model id from a transcript ``message.model``,
        e.g. ``"claude-opus-4-8"`` or ``"databricks-claude-sonnet-4-6"``.
    :returns: The model's :class:`ModelPricing`, or ``None`` when pricing
        is unavailable (network error / model absent from the catalog),
        so the caller skips that message's cost.
    """
    cached = _TRANSCRIPT_PRICING_CACHE.get(model)
    if cached is not None:
        return cached
    from omnigent.llms.context_window import fetch_model_pricing

    pricing = fetch_model_pricing(model)
    if pricing is not None:
        _TRANSCRIPT_PRICING_CACHE[model] = pricing
    return pricing

def compute_transcript_cumulative_cost(
    transcript_path: Path,
    *,
    include_sidechains: bool,
) -> float | None:
    """
    Sum the USD cost of every assistant message in a Claude transcript.

    Reads the whole transcript and prices each assistant record's
    ``message.usage`` by that record's ``message.model`` (so a
    mid-session ``/model`` switch is billed at the right rate), summing
    the per-message costs. This is the forwarder's *real-time* cost
    estimate for a transcript whose authoritative cumulative cost lags —
    specifically a Task sub-agent's own ``agent-<id>.jsonl``, which has
    no statusLine of its own, so its spend is otherwise invisible to the
    cost-budget policy until the sub-agent finishes.

    Cost is linear in token counts, so summing per-message costs equals
    pricing the token totals — but per-message pricing also stays correct
    across a model switch within one transcript.

    **Deduplicated by ``requestId``.** Claude writes more than one
    transcript record for a single API response (a streamed partial plus
    the final record, retries, etc.), and those records share one
    ``requestId`` while each carries that response's full ``message.usage``
    (not an increment). Summing every record would bill the same response
    two-plus times — observed ~2x inflation, with the parent badge and the
    cost-budget gate both reading the doubled figure. So records are keyed
    by ``requestId`` (last priceable record per id wins, as its usage is
    the authoritative final figure) and each billed response is counted
    exactly once. A record with no ``requestId`` (rare non-API assistant
    entry) gets a per-record unique key so it is never collapsed with
    another.

    :param transcript_path: Path to a Claude transcript JSONL, e.g.
        ``".../<session>.jsonl"`` (parent) or
        ``".../subagents/agent-<id>.jsonl"`` (sub-agent).
    :param include_sidechains: ``False`` for a parent transcript — its
        sub-agent records are inlined as ``isSidechain: true`` and are
        skipped here (they are counted via the sub-agent's own
        transcript) to avoid double-billing. ``True`` for a sub-agent's
        own ``agent-<id>.jsonl``, where every record is a sidechain.
    :returns: Total USD cost across priced assistant messages, or
        ``None`` when the transcript has no assistant message that could
        be priced (missing/empty file, no usage, or pricing unavailable
        for every model present) — distinct from ``0.0``, which means
        priced messages summed to zero.
    """
    read_result = _read_complete_jsonl_records(
        transcript_path,
        byte_offset=0,
        start_line=0,
    )
    from omnigent.llms.context_window import compute_llm_cost

    # Per-``requestId`` cost (USD); last priceable record per id wins so a
    # response written across multiple transcript records is counted once.
    cost_by_request: dict[str, float] = {}
    # Counter minting unique keys for records lacking a ``requestId`` so
    # they each count once instead of collapsing onto a shared key.
    no_request_id_index = 0
    for record in read_result.records:
        if record.text is None:
            continue
        try:
            entry = json.loads(record.text)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if not include_sidechains and entry.get("isSidechain") is True:
            continue
        usage = _usage_from_transcript_entry(entry)
        if usage is None:
            continue
        model = _model_from_transcript_entry(entry)
        if model is None:
            continue
        pricing = _transcript_model_pricing(model)
        if pricing is None:
            continue
        request_id = entry.get("requestId")
        if not isinstance(request_id, str) or not request_id:
            request_id = f"__no_request_id_{no_request_id_index}"
            no_request_id_index += 1
        cost_by_request[request_id] = compute_llm_cost(usage, pricing)
    if not cost_by_request:
        return None
    return sum(cost_by_request.values())

def _model_from_transcript_entry(entry: dict[str, Any]) -> str | None:
    """
    Return ``message.model`` from an assistant transcript record.

    Surfaced on :class:`TranscriptReadResult.latest_model` for
    diagnostics. The ring's denominator comes from the statusLine
    stdin (see :func:`read_claude_context_state`); the JSONL model
    name is no longer used to size the ring.

    :param entry: One decoded transcript JSONL record.
    :returns: API model name, or ``None`` for non-assistant entries
        and entries missing the field.
    """
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    model = message.get("model")
    if isinstance(model, str) and model:
        return model
    return None

def read_claude_context_state(bridge_dir: Path) -> dict[str, Any] | None:
    """
    Read the most recent statusLine snapshot from ``context.json``.

    Written atomically by :mod:`omnigent.claude_native_status` each
    time Claude Code invokes the wrapped statusLine command. The file
    is the authoritative source for both the ring's denominator
    (``context_window_size`` — Claude Code knows the real window for
    the active model and beta tier) and an optional fresh
    ``current_usage`` block.

    :param bridge_dir: Bridge directory shared with the forwarder.
    :returns: Parsed dict with keys ``context_window_size`` (int) and
        optionally ``current_usage`` (dict). ``None`` when the file
        doesn't exist yet, is unreadable, or doesn't carry a usable
        window — the forwarder treats that as "no update".
    """
    path = bridge_dir / _CONTEXT_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    size = parsed.get("context_window_size")
    if not isinstance(size, int) or size <= 0:
        return None
    return parsed

def read_claude_status_model(bridge_dir: Path) -> str | None:
    """
    Read the active model id from the statusLine snapshot ``context.json``.

    Unlike :func:`read_claude_context_state` (which returns ``None`` unless a
    usable ``context_window_size`` is present, since it backs the context
    ring), this returns the model whenever the wrapper captured one — the
    model and the window are written independently, and the cost-budget gate
    needs the model even on a render where the window field was absent. This
    is claude-native's race-free, gate-time source of the live ``/model``
    selection (the analogue of the codex hook reading ``config.toml``).

    :param bridge_dir: Bridge directory shared with the statusLine wrapper.
    :returns: The model id, e.g. ``"claude-sonnet-4-6"``, or ``None`` when
        the file is missing / unreadable / carries no model string.
    """
    path = bridge_dir / _CONTEXT_FILE
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    model = parsed.get("model")
    return model if isinstance(model, str) and model else None

def read_user_status_line_command() -> str | None:
    """
    Return the user's globally-configured statusLine shell command, if any.

    We override Claude Code's statusLine in our per-session ``--settings``
    to capture ``context_window`` stdin. To avoid breaking the user's
    pre-existing status bar (typically claude-hud), the wrapper chains
    to whatever they had configured globally.

    :returns: The command string from
        ``~/.claude/settings.json``'s ``statusLine.command``, or
        ``None`` when no global statusLine is configured / readable.
    """
    try:
        raw = _USER_CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    status_line = parsed.get("statusLine")
    if not isinstance(status_line, dict):
        return None
    command = status_line.get("command")
    if isinstance(command, str) and command.strip():
        return command
    return None

def read_user_effort_level() -> str | None:
    """
    Return the user's configured Claude Code effort level, if any.

    Read client-side from ``effortLevel`` in ``~/.claude/settings.json`` —
    the level the wrapped ``claude`` actually runs at (we pass no ``--effort``).

    :returns: A recognized effort, e.g. ``"medium"``; ``None`` when unset,
        unreadable, or not a valid Claude effort (fail-soft, never blocks launch).
    """
    try:
        raw = _USER_CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    effort = parsed.get("effortLevel")
    if isinstance(effort, str) and effort in CLAUDE_EFFORTS:
        return effort
    return None

def _usage_from_transcript_entry(entry: dict[str, Any]) -> dict[str, int] | None:
    """
    Extract token-usage from one Claude assistant transcript entry.

    ``context_tokens`` is ``input + cache_creation + cache_read`` — the
    bytes that will reappear in the next call's prompt. Output tokens
    are reported separately since they don't shift the prompt forward.

    :param entry: One decoded transcript JSONL record.
    :returns: ``{"context_tokens", "input_tokens", "output_tokens"}``
        when the record is an assistant entry with usage; ``None``
        otherwise.
    """
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    cache_creation = usage.get("cache_creation_input_tokens")
    cache_read = usage.get("cache_read_input_tokens")
    if not isinstance(input_tokens, int):
        return None
    if not isinstance(output_tokens, int):
        output_tokens = 0
    cc = cache_creation if isinstance(cache_creation, int) else 0
    cr = cache_read if isinstance(cache_read, int) else 0
    result: dict[str, int] = {
        "context_tokens": input_tokens + cc + cr,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if cc:
        result["cache_creation_input_tokens"] = cc
    if cr:
        result["cache_read_input_tokens"] = cr
    return result

def _assistant_text_from_transcript_line(line: str) -> str | None:
    """
    Extract assistant text from one Claude transcript JSONL line.

    :param line: Raw JSONL record.
    :returns: Assistant text, or ``None`` when the record is not an
        assistant text message.
    """
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(entry, dict):
        return None
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "".join(parts) or None


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _args as _sib_args
    from . import _bridge_io as _sib_bridge_io
    from . import _helpers as _sib_helpers
    from . import _hooks as _sib_hooks
    from . import _inject as _sib_inject
    from . import _mcp as _sib_mcp
    from . import _tmux as _sib_tmux
    from . import _transcript_convert as _sib_transcript_convert
    from . import _transcript_read as _sib_transcript_read
    from . import _types as _sib_types
    for _key, _value in _sib_args.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_bridge_io.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_helpers.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_hooks.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_inject.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_mcp.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_tmux.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_transcript_convert.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_transcript_read.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_types.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
