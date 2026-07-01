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

def _transcript_items_from_entry(
    entry: dict[str, Any],
    *,
    line_number: int,
    record_offset: int | None = None,
    agent_name: str,
    current_response_id: str | None,
    include_sidechains: bool = False,
) -> tuple[str | None, list[ClaudeTranscriptItem]]:
    """
    Convert one Claude transcript entry into Omnigent conversation items.

    :param entry: Decoded JSON object from one transcript line.
    :param line_number: One-based transcript line number.
    :param record_offset: Byte offset where the transcript record
        starts. Used for stable fallback source ids when Claude omits
        ``uuid`` and ``requestId``.
    :param agent_name: Agent/model name for assistant/tool items.
    :param current_response_id: Response id for the active Claude
        assistant turn, if a previous poll already started one.
    :param include_sidechains: When ``False`` (the default) any record
        with ``isSidechain: true`` is dropped — that's the right
        behavior when reading the parent's main transcript, where
        sub-agent records are inlined as sidechains and must not
        appear in the parent's Omnigent conversation. When ``True`` the
        flag is ignored — required when reading a sub-agent's own
        ``agent-<id>.jsonl`` (every record there is a sidechain by
        definition) so the sub-agent's items reach the child AP
        conversation. Caller is responsible for matching the flag to
        the file shape.
    :returns: Updated active response id and parsed items.
    """
    if not include_sidechains and entry.get("isSidechain") is True:
        return current_response_id, []
    if entry.get("type") == "attachment":
        return _attachment_transcript_items_from_entry(
            entry,
            line_number=line_number,
            record_offset=record_offset,
            current_response_id=current_response_id,
        )
    if entry.get("subtype") == "local_command":
        return _local_command_transcript_items_from_entry(
            entry,
            line_number=line_number,
            record_offset=record_offset,
            current_response_id=current_response_id,
        )
    message = entry.get("message")
    if not isinstance(message, dict):
        return current_response_id, []
    role = message.get("role")
    if role == "user":
        return _user_transcript_items_from_entry(
            entry,
            line_number=line_number,
            record_offset=record_offset,
            agent_name=agent_name,
            current_response_id=current_response_id,
        )
    if role == "assistant":
        return _assistant_transcript_items_from_entry(
            entry,
            line_number=line_number,
            record_offset=record_offset,
            agent_name=agent_name,
            current_response_id=current_response_id,
        )
    return current_response_id, []

def _attachment_transcript_items_from_entry(
    entry: dict[str, Any],
    *,
    line_number: int,
    record_offset: int | None,
    current_response_id: str | None,
) -> tuple[str | None, list[ClaudeTranscriptItem]]:
    """
    Parse user-visible Claude attachment transcript entries.

    Claude records prompts typed while an assistant turn is busy as
    ``attachment.type == "queued_command"`` rather than a normal
    ``role=user`` message. Treat prompt-mode queued commands as user
    messages so interruption inputs such as ``"STOP"`` appear in the
    Omnigent transcript and reset the active assistant response.

    :param entry: Decoded Claude transcript record.
    :param line_number: One-based transcript line number.
    :param record_offset: Byte offset where the transcript record
        starts, or ``None`` for legacy line-cursor reads.
    :param current_response_id: Response id for the active assistant
        turn. Ignored attachment metadata preserves this value.
    :returns: Updated active response id and parsed items.
    """
    attachment = entry.get("attachment")
    if not isinstance(attachment, dict):
        return current_response_id, []
    if attachment.get("type") != "queued_command":
        return current_response_id, []
    if attachment.get("commandMode") != "prompt":
        return current_response_id, []
    prompt = attachment.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        return current_response_id, []
    source_key = _transcript_source_key(entry, line_number, record_offset)
    item = ClaudeTranscriptItem(
        source_id=_source_id(source_key, 0, "message"),
        item_type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": prompt}],
        },
        response_id=_response_id_from_source(source_key),
    )
    return None, [item]

@dataclass(frozen=True)
class _SlashCommandPayload:
    """
    Parsed content of a slash-command ``role=user`` transcript record.

    :param name: Command name with leading ``/`` stripped, e.g.
        ``"dev-productivity:simplify"``.
    :param arguments: Verbatim ``<command-args>`` text; empty when none.
    :param output: Verbatim ``<local-command-stdout>`` text, or ``None``.
    """

    name: str
    arguments: str
    output: str | None

def _parse_slash_command_record(content: str) -> _SlashCommandPayload | None:
    """
    Parse a Claude Code slash-command marker blob.

    Returns ``None`` on a missing/empty/unclosed ``<command-name>``
    tag rather than raising — a single corrupt JSONL line must not
    kill the transcript poll loop.

    :param content: ``message.content`` string from a ``role=user``
        Claude Code JSONL record.
    :returns: Parsed payload, or ``None`` when no name could be
        extracted.
    """
    name_match = _COMMAND_NAME_RE.search(content)
    if name_match is None:
        return None
    raw_name = name_match.group(1).strip()
    if not raw_name:
        return None
    # Strip leading ``/`` so renderers can add their own prefix without double-rendering.
    name = raw_name.lstrip("/")
    if not name:
        return None
    args_match = _COMMAND_ARGS_RE.search(content)
    arguments = args_match.group(1).strip() if args_match else ""
    stdout_match = _COMMAND_STDOUT_RE.search(content)
    output = stdout_match.group(1) if stdout_match else None
    return _SlashCommandPayload(name=name, arguments=arguments, output=output)

def _local_command_transcript_items_from_entry(
    entry: dict[str, Any],
    *,
    line_number: int,
    record_offset: int | None,
    current_response_id: str | None,
) -> tuple[str | None, list[ClaudeTranscriptItem]]:
    """
    Parse a top-level Claude ``local_command`` transcript entry.

    Newer Claude Code builds can record shell-mode ``!cmd`` activity
    as top-level transcript records with ``subtype="local_command"``
    and a string ``content`` field instead of wrapping the same markup
    inside ``message.role=user``. Only ``<bash-*>`` records are
    conversation-visible here; slash-command local records are still
    handled by hook/fork detection and otherwise ignored.

    :param entry: Decoded Claude transcript record.
    :param line_number: One-based transcript line number.
    :param record_offset: Byte offset where the transcript record
        starts, or ``None`` for legacy line-cursor reads.
    :param current_response_id: Response id for an in-progress shell
        command group, if the input record was parsed in an earlier
        line.
    :returns: Updated active response id and parsed terminal-command
        items.
    """
    content = entry.get("content")
    if not isinstance(content, str) or not content:
        return current_response_id, []
    source_key = _transcript_source_key(entry, line_number, record_offset)
    fallback_response_id = _response_id_from_source(source_key)
    response_id = (
        fallback_response_id
        if _BASH_INPUT_RE.search(content) is not None
        else current_response_id or fallback_response_id
    )
    items = _terminal_command_items_from_content(
        content,
        source_key=source_key,
        response_id=response_id,
    )
    if not items:
        return current_response_id, []
    return response_id, items

def _terminal_command_items_from_content(
    content: str,
    *,
    source_key: str,
    response_id: str,
) -> list[ClaudeTranscriptItem]:
    """
    Parse Claude shell-mode markup into terminal-command items.

    Claude may emit shell input and output as separate records or as
    one record containing multiple ``<bash-*>`` tags. This helper
    emits at most one input item and one output item, preserving their
    order in the source record and giving both the same response id so
    the server transcript groups an invocation with its result.

    :param content: Transcript markup, e.g.
        ``"<bash-input>pwd</bash-input><bash-stdout>/tmp</bash-stdout>"``.
    :param source_key: Base transcript record key used to construct
        source ids, e.g. ``"rec_abc123"``.
    :param response_id: Synthetic response id for this terminal
        command group, e.g. ``"resp_claude_abc123"``.
    :returns: Parsed ``terminal_command`` items. Empty when no shell
        markers are present.
    """
    if not any(marker in content for marker in ("<bash-input>", "<bash-stdout>", "<bash-stderr>")):
        return []
    input_match = _BASH_INPUT_RE.search(content)
    stdout_match = _BASH_STDOUT_RE.search(content)
    stderr_match = _BASH_STDERR_RE.search(content)
    items: list[ClaudeTranscriptItem] = []
    item_index = 0
    if input_match is not None:
        items.append(
            ClaudeTranscriptItem(
                source_id=_source_id(source_key, item_index, "terminal_command"),
                item_type="terminal_command",
                data={"kind": "input", "input": input_match.group(1)},
                response_id=response_id,
            )
        )
        item_index += 1
    if stdout_match is not None or stderr_match is not None:
        items.append(
            ClaudeTranscriptItem(
                source_id=_source_id(source_key, item_index, "terminal_command"),
                item_type="terminal_command",
                data={
                    "kind": "output",
                    "stdout": stdout_match.group(1) if stdout_match is not None else None,
                    "stderr": stderr_match.group(1) if stderr_match is not None else None,
                },
                response_id=response_id,
            )
        )
    return items

def _user_transcript_items_from_entry(
    entry: dict[str, Any],
    *,
    line_number: int,
    record_offset: int | None,
    agent_name: str,
    current_response_id: str | None,
) -> tuple[str | None, list[ClaudeTranscriptItem]]:
    """
    Parse a Claude ``role=user`` transcript entry.

    :param entry: Decoded Claude transcript record.
    :param line_number: One-based transcript line number.
    :param record_offset: Byte offset where the transcript record
        starts, or ``None`` for legacy line-cursor reads.
    :param agent_name: Agent/model name attached to ``slash_command``
        items so the web UI can attribute the invocation.
    :param current_response_id: Response id for the active assistant
        turn; tool results keep this id.
    :returns: Updated active response id and parsed user/tool-result
        items.
    """
    # ``isMeta=true`` carries CLI scaffolding like
    # ``<local-command-caveat>``; no user-visible content.
    if entry.get("isMeta") is True:
        return current_response_id, []
    message = entry["message"]
    content = message.get("content") if isinstance(message, dict) else None
    source_key = _transcript_source_key(entry, line_number, record_offset)
    fallback_response_id = _response_id_from_source(source_key)
    items: list[ClaudeTranscriptItem] = []

    if isinstance(content, str):
        if not content:
            return current_response_id, []
        stripped = content.lstrip()
        # Skill invocations with args ship the tag order
        # ``<command-message>…<command-name>…<command-args>…`` — i.e.
        # ``<command-name>`` is NOT the first tag. Detect it anywhere
        # in the content, not just at the start.
        if "<command-name>" in stripped:
            payload = _parse_slash_command_record(content)
            # Drop unparseable markup rather than letting it fall through
            # to the user-bubble path — that rendered the markup verbatim
            # in the original bug.
            if payload is None or payload.name in _CLAUDE_CLI_DROPPED_COMMANDS:
                return current_response_id, []
            kind = "command" if payload.name in _CLAUDE_CLI_SURFACED_COMMANDS else "skill"
            data: dict[str, Any] = {
                "agent": agent_name,
                "kind": kind,
                "name": payload.name,
                "arguments": payload.arguments,
            }
            if payload.output is not None:
                data["output"] = payload.output
            items.append(
                ClaudeTranscriptItem(
                    source_id=_source_id(source_key, 0, "slash_command"),
                    item_type="slash_command",
                    data=data,
                    response_id=fallback_response_id,
                )
            )
            # Slash command opens a new logical turn; subsequent
            # assistant text must inherit this id so it clusters with
            # the indicator, not the prior bubble.
            return fallback_response_id, items
        # ``!cmd`` terminal commands may arrive here in older Claude
        # builds; newer builds use top-level ``local_command`` records.
        # In both shapes, surface the command and result as their own
        # transcript group instead of inheriting the previous assistant
        # response id.
        terminal_response_id = (
            fallback_response_id
            if _BASH_INPUT_RE.search(content) is not None
            else current_response_id or fallback_response_id
        )
        terminal_items = _terminal_command_items_from_content(
            content,
            source_key=source_key,
            response_id=terminal_response_id,
        )
        if terminal_items:
            return terminal_response_id, terminal_items
        # Other CLI-scaffolding records (stdout/stderr from /effort, etc.)
        # arrive as standalone ``role=user`` records and must drop instead
        # of leaking as user bubbles.
        if any(stripped.startswith(m) for m in _CLI_SCAFFOLDING_MARKERS):
            return current_response_id, []
        items.append(
            ClaudeTranscriptItem(
                source_id=_source_id(source_key, 0, "message"),
                item_type="message",
                data={
                    "role": "user",
                    "content": [{"type": "input_text", "text": content}],
                },
                response_id=fallback_response_id,
            )
        )
        return None, items

    if not isinstance(content, list):
        return current_response_id, []

    user_blocks: list[dict[str, Any]] = []
    saw_user_text = False
    item_index = 0
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str) or not text:
                continue
            # Defensively guard against slash-command markup or other
            # CLI-scaffolding markers ever arriving in list-form
            # content. Today these only ship in string content (the
            # branch above), but Claude Code's JSONL format is not
            # under our control — without this filter, a format
            # change would regress to rendering ``<command-name>…``
            # markup as a user bubble.
            stripped = text.lstrip()
            if "<command-name>" in stripped or any(
                stripped.startswith(m) for m in _CLI_SCAFFOLDING_MARKERS
            ):
                continue
            user_blocks.append({"type": "input_text", "text": text})
            saw_user_text = True
            continue
        if block_type != "tool_result":
            continue
        call_id = block.get("tool_use_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        response_id = current_response_id or _response_id_from_source(
            _parent_or_record_source_key(entry, line_number, record_offset)
        )
        items.append(
            ClaudeTranscriptItem(
                source_id=_source_id(source_key, item_index, "function_call_output"),
                item_type="function_call_output",
                data={
                    "call_id": call_id,
                    "output": _tool_result_output(entry, block),
                },
                response_id=response_id,
            )
        )
        item_index += 1

    if user_blocks:
        items.insert(
            0,
            ClaudeTranscriptItem(
                source_id=_source_id(source_key, item_index, "message"),
                item_type="message",
                data={
                    "role": "user",
                    "content": user_blocks,
                },
                response_id=fallback_response_id,
            ),
        )
    return (None if saw_user_text else current_response_id), items

def _assistant_transcript_items_from_entry(
    entry: dict[str, Any],
    *,
    line_number: int,
    record_offset: int | None,
    agent_name: str,
    current_response_id: str | None,
) -> tuple[str | None, list[ClaudeTranscriptItem]]:
    """
    Parse a Claude ``role=assistant`` transcript entry.

    :param entry: Decoded Claude transcript record.
    :param line_number: One-based transcript line number.
    :param record_offset: Byte offset where the transcript record
        starts, or ``None`` for legacy line-cursor reads.
    :param agent_name: Agent/model name for assistant/tool items.
    :param current_response_id: Response id for the active Claude
        assistant turn.
    :returns: Updated active response id and parsed assistant/tool
        items.
    """
    message = entry["message"]
    content = message.get("content") if isinstance(message, dict) else None
    source_key = _transcript_source_key(entry, line_number, record_offset)
    response_id = current_response_id or _response_id_from_source(source_key)
    items: list[ClaudeTranscriptItem] = []

    if isinstance(content, str):
        if content:
            items.append(
                _assistant_message_item(
                    source_key=source_key,
                    item_index=0,
                    agent_name=agent_name,
                    response_id=response_id,
                    text=content,
                )
            )
        return response_id, items

    if not isinstance(content, list):
        return current_response_id, []

    for item_index, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                items.append(
                    _assistant_message_item(
                        source_key=source_key,
                        item_index=item_index,
                        agent_name=agent_name,
                        response_id=response_id,
                        text=text,
                    )
                )
            continue
        if block_type == "tool_use":
            tool_id = block.get("id")
            name = block.get("name")
            if not isinstance(tool_id, str) or not tool_id:
                continue
            if not isinstance(name, str) or not name:
                continue
            arguments = block.get("input")
            if not isinstance(arguments, dict):
                arguments = {}
            items.append(
                ClaudeTranscriptItem(
                    source_id=_source_id(source_key, item_index, "function_call"),
                    item_type="function_call",
                    data={
                        "agent": agent_name,
                        "name": name,
                        "arguments": json.dumps(arguments, separators=(",", ":")),
                        "call_id": tool_id,
                    },
                    response_id=response_id,
                )
            )
    return response_id if items else current_response_id, items

def _assistant_message_item(
    *,
    source_key: str,
    item_index: int,
    agent_name: str,
    response_id: str,
    text: str,
) -> ClaudeTranscriptItem:
    """
    Build an assistant message item from one Claude text block.

    :param source_key: Base transcript record key.
    :param item_index: Content block index inside the record.
    :param agent_name: Agent/model name for the assistant message.
    :param response_id: Response id grouping the Claude turn.
    :param text: Assistant text block.
    :returns: Parsed transcript item.
    """
    return ClaudeTranscriptItem(
        source_id=_source_id(source_key, item_index, "message"),
        item_type="message",
        data={
            "role": "assistant",
            "agent": agent_name,
            "content": [{"type": "output_text", "text": text}],
        },
        response_id=response_id,
    )

def _tool_result_output(entry: dict[str, Any], block: dict[str, Any]) -> str:
    """
    Return the UI-facing output string for a Claude tool result.

    :param entry: Decoded Claude transcript record containing
        optional ``toolUseResult`` metadata.
    :param block: ``tool_result`` content block from ``message``.
    :returns: String output for a ``function_call_output`` item.
    """
    content = block.get("content")
    if isinstance(content, str):
        return content
    if content is not None:
        return json.dumps(content, separators=(",", ":"))
    tool_use_result = entry.get("toolUseResult")
    if isinstance(tool_use_result, str):
        return tool_use_result
    if tool_use_result is not None:
        return json.dumps(tool_use_result, separators=(",", ":"))
    return ""

def _transcript_source_key(
    entry: dict[str, Any],
    line_number: int,
    record_offset: int | None = None,
) -> str:
    """
    Return the stable key for a Claude transcript record.

    :param entry: Decoded Claude transcript record.
    :param line_number: One-based transcript line number.
    :param record_offset: Byte offset where the transcript record
        starts, or ``None`` when unavailable.
    :returns: Claude UUID/request id, byte-offset fallback, or a
        legacy line-number fallback.
    """
    for key in ("uuid", "requestId"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    if record_offset is not None:
        return f"byte-{record_offset}"
    return f"line-{line_number}"

def _parent_or_record_source_key(
    entry: dict[str, Any],
    line_number: int,
    record_offset: int | None = None,
) -> str:
    """
    Return a parent key for tool results when Claude supplies one.

    :param entry: Decoded Claude transcript record.
    :param line_number: One-based transcript line number.
    :param record_offset: Byte offset where the transcript record
        starts, or ``None`` when unavailable.
    :returns: Parent UUID when present, otherwise the record key.
    """
    parent = entry.get("parentUuid")
    if isinstance(parent, str) and parent:
        return parent
    return _transcript_source_key(entry, line_number, record_offset)

def _response_id_from_source(source: str) -> str:
    """
    Derive a deterministic Omnigent response id from a Claude source key.

    :param source: Claude UUID/request id/line key.
    :returns: String id with the standard ``resp_`` prefix.
    """
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]
    return f"resp_claude_{digest}"

def _source_id(source_key: str, item_index: int, item_type: str) -> str:
    """
    Build a per-item idempotency key for a transcript-derived item.

    :param source_key: Base Claude record key.
    :param item_index: Content block index inside the record.
    :param item_type: Omnigent item type.
    :returns: Stable source id string.
    """
    return f"{source_key}:{item_index}:{item_type}"


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _args as _sib_args
    from . import _bridge_io as _sib_bridge_io
    from . import _cost as _sib_cost
    from . import _helpers as _sib_helpers
    from . import _hooks as _sib_hooks
    from . import _inject as _sib_inject
    from . import _mcp as _sib_mcp
    from . import _tmux as _sib_tmux
    from . import _transcript_read as _sib_transcript_read
    from . import _types as _sib_types
    for _key, _value in _sib_args.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_bridge_io.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_cost.__dict__.items():
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
    for _key, _value in _sib_transcript_read.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_types.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
