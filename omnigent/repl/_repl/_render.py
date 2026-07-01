"""Rich-based REPL for omnigent — built on the UI SDK framework.

The public API is ``run_repl(client, agent_name, tool_handler)``.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import inspect
import json
import logging
import os
import pathlib
import sys
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, TextIO

from omnigent_client import (
    BlockContext,
    ElicitationRequestCtx,
    OmnigentClient,
    OmnigentError,
    ReasoningBlock,
    ResponseEndBlock,
    ResponseStartBlock,
    Session,
    StreamHooks,
    ToolExecution,
    ToolGroup,
    ToolHandler,
    ToolResultBlock,
    format_tool_args_brief,
)
from omnigent_ui_sdk import (
    DEFAULT_USER_CONFIG,
    OverlayTarget,
    PendingAttachment,
    RichBlockFormatter,
    TerminalHost,
    TerminalTheme,
    UserConfigError,
    load_user_config,
    save_user_config,
    update_user_config,
)

# ``FormattedItem`` is the SDK formatter's per-method return type
# (``Rich.RenderableType | StreamingText | StreamReplace``). The
# top-level package doesn't re-export it today, so import from the
# internal ``_formatter`` module — keeping the import explicit
# rather than retyping every formatter override as ``list[Any]``.
# When the SDK adds an explicit re-export this should switch to
# ``from omnigent_ui_sdk import FormattedItem``.
from omnigent_ui_sdk.terminal._completer import FileMentionCompleter
from omnigent_ui_sdk.terminal._formatter import FormattedItem
from omnigent_ui_sdk.terminal._theme import LIGHT_THEME, get_theme
from prompt_toolkit.completion import CompleteEvent, Completer, Completion, merge_completers
from prompt_toolkit.document import Document
from rich.console import RenderableType
from rich.text import Text

from omnigent.spec.types import SkillSpec

if TYPE_CHECKING:
    from omnigent.server.schemas import SessionStatusEvent

_log = logging.getLogger(__name__)


def _import_package_bindings() -> None:
    from . import _constants as _pkg_constants
    from . import _state as _pkg_state
    g = globals()
    for _mod in (_pkg_constants, _pkg_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_package_bindings()

def _render_startup_banner_ansi(
    ui_name: str,
    *,
    server_url: str | None = None,
    header: _StartupHeader | None = None,
) -> str:
    """
    Build the ANSI-styled startup banner shown when the REPL boots.

    Renders the mascot art + accent-bordered box, using the SDK's
    starfish magenta-pink brand color (``#F43BA6``) so the box border,
    mascot, prompt marker, and bottom toolbar all read as one accent.

    When *header* is supplied, the box becomes a Claude-Code-style header:
    the agent name (bold) plus dim rows for the one-line summary, the
    model + credential, the working folder, and (for a remote server) the
    server URL; a per-family creds line is appended beneath the box for
    multi-vendor agents. When *header* is ``None`` the box keeps its
    minimal form — just the name, with the server URL taking the single
    info row when the host is non-loopback (keybinding hints live in the
    bottom toolbar, so the hint row is omitted).

    :param ui_name: Humanized agent label shown bold at the top of the box.
    :param server_url: Base URL the REPL is connected to. Surfaced only
        when the host is non-loopback. ``None`` skips it.
    :param header: Resolved header data (folder / model / credential /
        summary / creds line) from :func:`_build_startup_header`, or
        ``None`` for the minimal banner.
    :returns: ANSI-styled string ready to be written to stdout.
    """
    from omnigent.inner.banner import BannerLine, startup_banner_strings

    remote = _is_remote_server_url(server_url)

    if header is None:
        banner = startup_banner_strings(
            ui_name,
            hint_line=server_url if remote else "",
            art_color="#F43BA6",
        )
        return banner.ansi

    info_lines: list[BannerLine] = []
    if header.description:
        info_lines.append(BannerLine(header.description, dim=True))
    # Model + credential on one row: "<model>  ·  <glyph credential>".
    # Either part may be absent (a subscription with no pinned model shows
    # just the credential; a remote target with no local harness shows
    # neither, so the row is skipped).
    if header.model_label and header.credential:
        info_lines.append(BannerLine(f"{header.model_label}  ·  {header.credential}", dim=True))
    elif header.credential:
        info_lines.append(BannerLine(header.credential, dim=True))
    elif header.model_label:
        info_lines.append(BannerLine(header.model_label, dim=True))
    info_lines.append(BannerLine(header.folder, dim=True))
    if remote and server_url is not None:
        info_lines.append(BannerLine(server_url, dim=True))

    banner = startup_banner_strings(ui_name, info_lines=info_lines, art_color="#F43BA6")
    if header.creds_line:
        # A playful lead-in + the per-vendor creds line, both dim, beneath the
        # box. The creds line renders only for a multi-vendor agent, which
        # always means it spawns sub-agents of another vendor — so inviting the
        # user to "spawn" them is accurate. Indented to the REPL's left margin.
        lead = f"Try asking {ui_name} to spawn the following sub-agents!"
        return (
            f"{banner.ansi}\n\n"
            f"  {_ANSI_DIM}{lead}{_ANSI_RESET}\n"
            f"  {_ANSI_DIM}{header.creds_line}{_ANSI_RESET}"
        )
    return banner.ansi

class TimedFormatter(RichBlockFormatter):  # type: ignore[misc]
    """Shows final elapsed time after response completes."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._start_time: float | None = None

    def format_response_start(self, block: ResponseStartBlock) -> list[FormattedItem]:
        self._start_time = block.ctx.timestamp
        return super().format_response_start(block)

    def format_response_end(self, block: ResponseEndBlock) -> list[FormattedItem]:
        items = super().format_response_end(block)
        if self._start_time is not None:
            elapsed = block.ctx.timestamp - self._start_time
            items.append(Text.from_markup(f"   [{self.muted}]{elapsed:.1f}s[/{self.muted}]"))
            self._start_time = None
        return items

@dataclass(frozen=True)
class _OutputItemRenderPlan:
    """
    How the streaming renderer should handle one ``OutputItemDone`` item.

    A single turn can interleave several assistant text blocks with
    tool calls. The streaming executor emits the prose as ``TextDelta``
    events and the tool calls as inline ``function_call`` output items,
    and it emits the assistant ``message`` output item only once, after
    all deltas — never between consecutive text blocks. So a tool call
    is the only signal that one text block ended and another is about to
    begin, and the renderer must commit the in-flight prose at that
    boundary or the formatter's paragraph buffer accumulates across
    blocks and re-renders the whole turn's prose on every later delta.

    :param flush_inflight_text: Commit any in-flight streamed assistant
        prose (via ``format_message_done``) before handling this item.
        ``True`` at every content-block boundary that interrupts
        streamed text — a tool call / output, or the assistant
        ``message`` item itself.
    :param render_item: Render ``item`` as a history entry. ``True`` for
        tool calls / outputs / slash commands and for a non-streamed
        assistant message; ``False`` for an assistant message whose
        prose already streamed as deltas (the deltas rendered it, so the
        full item would duplicate it).
    """

    flush_inflight_text: bool
    render_item: bool

def _plan_output_item_render(
    item_type: str | None,
    role: str | None,
    saw_text_deltas: bool,
) -> _OutputItemRenderPlan:
    """
    Decide how to handle one streamed ``OutputItemDone`` item.

    :param item_type: The item's ``type`` field, e.g. ``"function_call"``,
        ``"function_call_output"``, ``"message"``, or ``"slash_command"``.
    :param role: The item's ``role`` field for ``message`` items, e.g.
        ``"assistant"``; ``None`` for item types that have no role.
    :param saw_text_deltas: Whether any ``TextDelta`` has streamed since
        the last commit (i.e. there is in-flight assistant prose).
    :returns: The :class:`_OutputItemRenderPlan` for this item.
    """
    if item_type == "message" and role == "assistant":
        # Prose already streamed as deltas: commit the tail at the
        # boundary and skip re-rendering the full item. When no deltas
        # streamed (e.g. a non-streaming harness), render the item.
        return _OutputItemRenderPlan(
            flush_inflight_text=saw_text_deltas,
            render_item=not saw_text_deltas,
        )
    if item_type in _RENDERABLE_OUTPUT_ITEM_TYPES:
        # A concrete item interrupts streamed prose; commit the prose
        # first (no-op when nothing is in flight) so the next text block
        # starts from an empty buffer.
        return _OutputItemRenderPlan(
            flush_inflight_text=saw_text_deltas,
            render_item=True,
        )
    return _OutputItemRenderPlan(flush_inflight_text=False, render_item=False)

@dataclass
class _TurnProseTracker:
    """
    Streamed assistant prose bookkeeping for duplicate-item detection.

    The relay persists each streamed text segment at a tool-call
    boundary and publishes the persisted item as
    ``response.output_item.done`` so clients learn its store-assigned id
    (``_flush_relay_text`` in ``omnigent/server/routes/sessions.py``).
    By the time that event reaches the REPL, the tool-call item that
    triggered the flush has already committed the in-flight prose — the
    delta-based skip (``saw_text_deltas``) sees nothing in flight and
    would re-render the whole segment as a fresh "◆ agent + text" block.

    This tracker remembers the turn's streamed prose per committed
    segment so an assistant ``message`` item can be matched back (by
    byte-equal text) to prose the user already watched stream, and
    suppressed. Matching consumes the entry — multiset semantics, so a
    turn that legitimately produces two identical segments still gets
    its second copy matched by the second published item, and a
    genuinely non-streamed assistant message (no matching entry) still
    renders.

    :param segment_parts: Delta strings of the current (uncommitted)
        text segment in arrival order, e.g. ``["Got it — ", "done."]``.
    :param committed_texts: Joined text of each segment committed this
        turn, e.g. ``["Got it — done."]``.
    """

    segment_parts: list[str] = field(default_factory=list)
    committed_texts: list[str] = field(default_factory=list)

    def on_delta(self, delta: str) -> None:
        """
        Accumulate one streamed text delta into the current segment.

        :param delta: The ``response.output_text.delta`` text,
            e.g. ``"Got it — "``.
        """
        self.segment_parts.append(delta)

    def commit_segment(self) -> None:
        """
        Move the current segment into the committed list.

        Called when in-flight prose is committed at a content-block
        boundary (tool call, assistant ``message`` item). No-op when no
        deltas streamed since the last commit.
        """
        if self.segment_parts:
            self.committed_texts.append("".join(self.segment_parts))
            self.segment_parts.clear()

    def reset_turn(self) -> None:
        """
        Drop all bookkeeping at a turn boundary.

        A new turn's prose must not be matched against (or suppressed
        by) the previous turn's segments.
        """
        self.segment_parts.clear()
        self.committed_texts.clear()

    def consume_match(self, item: dict[str, object]) -> bool:
        """
        Match an assistant ``message`` item against committed prose.

        Joins the item's ``output_text`` content blocks and looks for a
        byte-equal committed segment. A match means the item is the
        relay's persisted copy of prose that already streamed, so
        rendering it would duplicate the segment. The matched entry is
        consumed.

        :param item: The ``output_item.done`` item dict, e.g.
            ``{"type": "message", "role": "assistant", "content":
            [{"type": "output_text", "text": "Got it — done."}]}``.
        :returns: ``True`` when the item's text matched (and consumed) a
            committed streamed segment; ``False`` when the item carries
            no output text or nothing matched.
        """
        content = item.get("content")
        if not isinstance(content, list):
            return False
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "output_text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        if not parts:
            return False
        joined = "".join(parts)
        try:
            self.committed_texts.remove(joined)
        except ValueError:
            return False
        return True

def _render_failed_status_error(
    fmt: RichBlockFormatter,
    host: TerminalHost,
    event: SessionStatusEvent,
) -> list[FormattedItem]:
    """
    Render the error line for a terminal ``session.status: failed`` event.

    A SETUP-phase failure (spec resolution, spawn-env build) ends the
    turn before the LLM stream starts, so no ``response.failed`` /
    ``ErrorEvent`` is ever emitted — the only signal is the terminal
    ``failed`` status. Without rendering its carried error message the
    REPL ends the turn silently: the working spinner vanishes with no
    output. This formats the message as an :class:`ErrorBlock` (the
    same red error styling used for ``response.error`` /
    ``response.failed``) and writes it to the host. Falls back to a
    generic ``"turn failed"`` message when the event carries no error
    detail so a bare ``failed`` status never crashes the renderer.

    :param fmt: The active block formatter, e.g. a
        :class:`TimedFormatter`.
    :param host: The terminal host the error line is written to.
    :param event: The terminal ``session.status`` event with
        ``status == "failed"``, e.g.
        ``SessionStatusEvent(type="session.status",
        conversation_id="conv_abc", status="failed",
        error=ErrorDetail(code="runner_error",
        message="turn setup failed: ..."))``.
    :returns: The list of formatted items written to the host (for
        debug-tape recording by the caller).
    """
    from omnigent_client import ErrorBlock

    err_message = (
        event.error.message if event.error is not None and event.error.message else "turn failed"
    )
    err_items = list(
        fmt.format_error(
            ErrorBlock(
                message=err_message,
                source="runner",
                ctx=BlockContext(agent=None, depth=0, turn=0),
            ),
        )
    )
    for err_item in err_items:
        host.output(err_item)
    return err_items

def _render_context_tree(
    agent_name: str,
    model_override: str | None,
    message_tokens: int,
    context_window: int | None,
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """
    Build and emit the context-usage Rich tree to the terminal host.

    When ``context_window`` is ``None`` the tree shows a plain token
    count and an "unknown" hint. Otherwise it renders the coin bar plus
    a per-category breakdown.

    :param agent_name: Agent's wire name, e.g. ``"my-agent"``.
    :param model_override: User-set LLM model identifier,
        e.g. ``"openai/gpt-4o"``, or ``None`` when using the agent default.
    :param message_tokens: Estimated token count for conversation messages,
        e.g. ``35_000``.
    :param context_window: Model's context window in tokens, or ``None``
        if unknown, e.g. ``200_000``.
    :param host: Terminal host to emit the tree to.
    :param fmt: Formatter supplying REPL colour names.
    """
    from rich.text import Text
    from rich.tree import Tree

    display_name = _humanize_agent_name(agent_name)
    if model_override:
        header_label = (
            f"[{fmt.accent}]{display_name}[/{fmt.accent}]"
            f" [{fmt.muted}]({model_override})[/{fmt.muted}]"
        )
    else:
        header_label = f"[{fmt.accent}]{display_name}[/{fmt.accent}]"

    tree = Tree(Text.from_markup(f"Context Usage · {header_label}"))

    if context_window is None:
        tree.add(
            Text.from_markup(
                f"[{fmt.muted}]Context window size unknown — "
                f"will be detected on first overflow[/{fmt.muted}]"
            )
        )
        tree.add(
            Text.from_markup(
                f"[{fmt.accent}]Messages[/{fmt.accent}]"
                f"  [{fmt.muted}]{message_tokens:,} tokens[/{fmt.muted}]"
            )
        )
        host.output(tree)
        return

    used_frac = min(message_tokens / context_window, 1.0)
    buf_frac = 1.0 - _CONTEXT_COMPACTION_TRIGGER  # 0.20
    used_coins = round(used_frac * _CONTEXT_COIN_TOTAL)
    buf_coins = round(buf_frac * _CONTEXT_COIN_TOTAL)
    # Free zone sits between used and buffer; clamp to zero if used is large.
    free_coins = max(_CONTEXT_COIN_TOTAL - used_coins - buf_coins, 0)
    # Absorb overflow into buffer when used spills past the trigger threshold.
    buf_coins = _CONTEXT_COIN_TOTAL - used_coins - free_coins
    coin_bar = (
        _CONTEXT_COIN_USED * used_coins
        + _CONTEXT_COIN_FREE * free_coins
        + _CONTEXT_COIN_BUF * buf_coins
    )

    free_tokens = max(context_window - message_tokens, 0)
    buf_tokens = int(context_window * buf_frac)
    used_pct = used_frac * 100.0

    tree.add(
        Text.from_markup(
            f"{coin_bar}  "
            f"[{fmt.accent}]{message_tokens / 1000:.1f}k[/{fmt.accent}]"
            f" [{fmt.muted}]/ {context_window // 1000}k tokens ({used_pct:.0f}%)[/{fmt.muted}]"
        )
    )
    tree.add(
        Text.from_markup(
            f"{_CONTEXT_COIN_USED} [{fmt.accent}]Messages[/{fmt.accent}]"
            f"  [{fmt.muted}]{message_tokens:,} tokens ({used_pct:.0f}%)[/{fmt.muted}]"
        )
    )
    tree.add(
        Text.from_markup(
            f"{_CONTEXT_COIN_FREE} [{fmt.muted}]Free space[/{fmt.muted}]"
            f"  [{fmt.muted}]{free_tokens:,} tokens"
            f" ({max(0.0, (1.0 - used_frac - buf_frac)) * 100:.0f}%)[/{fmt.muted}]"
        )
    )
    tree.add(
        Text.from_markup(
            f"{_CONTEXT_COIN_BUF} [{fmt.muted}]Compaction buffer[/{fmt.muted}]"
            f"  [{fmt.muted}]{buf_tokens:,} tokens ({buf_frac * 100:.0f}%)[/{fmt.muted}]"
        )
    )
    host.output(tree)

def _extract_message_text(item: dict[str, object]) -> str:
    """
    Concatenate ``input_text`` / ``output_text`` content blocks
    in a message item into a single string.

    :param item: A ``type="message"`` conversation item.
    :returns: Joined text from every text content block in
        order. Empty string when the message has no text blocks
        (e.g. a steering message with only file attachments).
    """
    content = item.get("content", [])
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for b in content:
        if isinstance(b, dict) and b.get("type") in ("input_text", "output_text"):
            parts.append(str(b.get("text", "")))
    return " ".join(parts)

def _extract_function_call_output_text(item: dict[str, object]) -> str:
    """
    Pull the textual output payload out of a
    ``function_call_output`` item, accepting both the API shape
    (``output`` flattened to the top level) and the entity shape
    (``data.output``). Used when re-rendering a tool result
    panel on resume.

    :param item: A ``type="function_call_output"`` conversation
        item.
    :returns: Raw output string. Empty on non-string / missing
        payloads — the caller still renders an empty result
        panel so the call/output pairing is visible.
    """
    raw = item.get("output")
    if isinstance(raw, str):
        return raw
    data = item.get("data")
    if isinstance(data, dict):
        nested = data.get("output")
        if isinstance(nested, str):
            return nested
    return ""

def _render_message_history_item(
    item: dict[str, object],
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """
    Render a ``type="message"`` item.

    User messages emit via :meth:`RichBlockFormatter.user_message`
    (same ``❯`` echo shown live). Assistant messages emit a
    ``◆ <model>`` header then the body as one or more Markdown
    paragraphs, matching what the live stream produces — so
    headers, code blocks, lists, etc. render the same on resume
    as they did originally, in default terminal foreground (the
    previous rendering used a muted gray that looked
    second-class). Empty assistant items (the workflow's
    trailing ``[{"type":"output_text","text":""}]``) are silently
    skipped to avoid a phantom header with no body underneath.

    :param item: A ``type="message"`` conversation item.
    :param host: The :class:`TerminalHost` to render against.
    :param fmt: The :class:`RichBlockFormatter` used for styling
        and for the Markdown ``code_theme``.
    """
    from rich.markdown import Markdown
    from rich.padding import Padding
    from rich.text import Text

    if item.get("is_meta") is True:
        return
    role = item.get("role", "")
    text = _extract_message_text(item)
    if role == "user":
        host.output(fmt.user_message(text))
        return
    if role == "assistant":
        # Skip empty assistant messages. The omnigent workflow
        # persists a trailing empty assistant item alongside every
        # real reply (``[{"type":"output_text","text":""}]``);
        # without this guard, replaying the conversation renders a
        # phantom ``◆ <model>`` line with no body underneath.
        if not text.strip():
            return
        model = item.get("model", "")
        host.output(Text.from_markup(f" [{fmt.assistant}]◆ {model}[/{fmt.assistant}]"))
        # Match the live stream's per-paragraph Markdown rendering
        # (see ``RichBlockFormatter._markdown_replace``): split on
        # blank-line paragraph boundaries, render each non-empty
        # paragraph as a padded Markdown panel using the
        # formatter's ``code_theme`` so resumed output is visually
        # identical to what the user originally saw — full
        # foreground color, syntax-highlighted code fences,
        # rendered headings.
        for paragraph in text.split("\n\n"):
            if not paragraph.strip():
                continue
            host.output(
                Padding(
                    Markdown(paragraph, code_theme=fmt.code_theme),
                    # (top, right, bottom, left): no vertical
                    # padding (paragraphs already separate),
                    # 1 right and 3 left = same indentation the
                    # live stream's ``_markdown_replace`` uses, so
                    # resumed paragraphs align with live ones.
                    (0, 1, 0, 3),
                ),
            )

def _render_function_call_history_item(
    item: dict[str, object],
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """
    Render a ``type="function_call"`` item as the live
    ``⏵ <name>(<args>)`` line.

    Builds a :class:`ToolExecution` from the item's flat fields,
    populates ``args_summary`` via :func:`format_tool_args_brief`
    (the same helper the live stream uses), and dispatches to
    :meth:`RichBlockFormatter.format_tool_group` so the call
    line on resume matches the line emitted live.

    :param item: A ``type="function_call"`` conversation item.
    :param host: The :class:`TerminalHost` to render against.
    :param fmt: The :class:`RichBlockFormatter` used for styling.
    """
    extracted_name, extracted_arguments = _tool_metadata_from_function_call_item(item)
    name = extracted_name or "?"
    arguments = extracted_arguments or {}
    call_id = str(item.get("call_id") or "")
    execution = ToolExecution(
        name=name,
        arguments=arguments,
        args_summary=format_tool_args_brief(name, arguments),
        call_id=call_id,
        agent_name="",
    )
    for renderable in fmt.format_tool_group(
        ToolGroup(executions=[execution], ctx=BlockContext()),
    ):
        host.output(renderable)

def _render_function_call_output_history_item(
    item: dict[str, object],
    host: TerminalHost,
    fmt: RichBlockFormatter,
    call_id_to_name: dict[str, str],
    call_id_to_tool_metadata: dict[str, tuple[str, dict[str, object]]],
) -> None:
    """
    Render a ``type="function_call_output"`` item as the live
    result panel.

    The tool name and original arguments are recovered from
    *call_id_to_tool_metadata* — the matching ``function_call``
    carries them, but the output row only carries ``call_id``. On
    miss (e.g. orphan output whose call was trimmed by the server),
    falls back to ``"?"`` rather than skipping so the turn-boundary
    signal is preserved.

    :param item: A ``type="function_call_output"`` conversation
        item.
    :param host: The :class:`TerminalHost` to render against.
    :param fmt: The :class:`RichBlockFormatter` used for styling.
    :param call_id_to_name: Back-compat map from ``call_id`` to
        tool name.
    :param call_id_to_tool_metadata: Map from ``call_id`` to
        ``(tool name, arguments)`` built once per conversation by
        :func:`_build_call_id_to_tool_metadata_lookup`.
    """
    call_id = str(item.get("call_id") or "")
    metadata = call_id_to_tool_metadata.get(call_id)
    if metadata is not None:
        name, arguments = metadata
    else:
        name = call_id_to_name.get(call_id, "?")
        arguments = {}
    output_text = _extract_function_call_output_text(item)
    for renderable in fmt.format_tool_result(
        ToolResultBlock(
            name=name,
            call_id=call_id,
            agent_name="",
            output=output_text,
            arguments=arguments,
            args_summary=format_tool_args_brief(name, arguments),
            ctx=BlockContext(),
        ),
    ):
        host.output(renderable)

def _render_reasoning_history_item(
    item: dict[str, object],
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """
    Render a ``type="reasoning"`` item as the live thinking
    panel.

    ``summary`` and ``content`` are both optional on reasoning
    rows (different providers populate different fields). When
    both are empty, the panel renderer would emit nothing
    anyway — short-circuit explicitly so the reader doesn't see
    a stray blank line.

    :param item: A ``type="reasoning"`` conversation item.
    :param host: The :class:`TerminalHost` to render against.
    :param fmt: The :class:`RichBlockFormatter` used for styling.
    """
    summary = item.get("summary")
    content = item.get("content")
    summary_text = summary if isinstance(summary, str) else ""
    reasoning_text = content if isinstance(content, str) else ""
    if not summary_text.strip() and not reasoning_text.strip():
        return
    for renderable in fmt.format_reasoning(
        ReasoningBlock(
            reasoning_text=reasoning_text,
            summary_text=summary_text,
            ctx=BlockContext(),
        ),
    ):
        host.output(renderable)

def _render_slash_command_history_item(
    item: dict[str, object],
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """
    Render a ``type="slash_command"`` item as a compact command echo.

    Skill slash commands are metadata, not normal user messages. The
    visible transcript should show the command the user invoked while
    the paired ``message.is_meta`` record carries the hidden skill
    instructions for agent context.

    :param item: A ``type="slash_command"`` conversation item.
    :param host: The :class:`TerminalHost` to render against.
    :param fmt: The :class:`RichBlockFormatter` used for styling.
    :returns: None.
    """
    from rich.text import Text

    name = str(item.get("name") or "")
    arguments = str(item.get("arguments") or "")
    output = item.get("output")
    label = f"/{name}" if not arguments else f"/{name} {arguments}"
    host.output(Text(f"  {label}", style=fmt.muted))
    if isinstance(output, str) and output:
        host.output(Text(f"  {output}", style=fmt.muted))

def _render_history_item(
    item: dict[str, object],
    host: TerminalHost,
    fmt: RichBlockFormatter | None = None,
    *,
    call_id_to_name: dict[str, str] | None = None,
    call_id_to_tool_metadata: dict[str, tuple[str, dict[str, object]]] | None = None,
) -> None:
    """
    Render a single conversation history item using the same
    visual primitives the live stream uses, so a resumed
    conversation looks identical to its original transcript.

    Dispatches to a per-type helper:

    - ``message`` → :func:`_render_message_history_item`
    - ``function_call`` → :func:`_render_function_call_history_item`
    - ``function_call_output`` →
      :func:`_render_function_call_output_history_item`
    - ``reasoning`` → :func:`_render_reasoning_history_item`

    Unknown types are silently dropped — historically the store
    has only ever emitted these four, and a future addition
    should land its own helper rather than implicitly coercing
    into one of the existing renderers.

    :param item: A conversation item dict from ``list_items``.
    :param host: The :class:`TerminalHost` to render against.
    :param fmt: The :class:`RichBlockFormatter` used for styling.
        A fresh formatter is constructed when omitted — useful
        in tests, less efficient than reusing the caller's
        instance.
    :param call_id_to_name: Back-compat map from ``call_id`` to
        tool name. ``None`` is treated as an empty map (orphan
        outputs render with ``"?"``).
    :param call_id_to_tool_metadata: Map from ``call_id`` to
        ``(tool name, arguments)``. Used to route
        ``function_call_output`` panels through pretty renderers
        that need the original function-call arguments.
    """
    if fmt is None:
        fmt = RichBlockFormatter(show_tool_output=True)
    if call_id_to_name is None:
        call_id_to_name = {}
    if call_id_to_tool_metadata is None:
        call_id_to_tool_metadata = {
            call_id: (name, {}) for call_id, name in call_id_to_name.items()
        }
    itype = item.get("type", "")
    if itype == "message":
        _render_message_history_item(item, host, fmt)
    elif itype == "function_call":
        _render_function_call_history_item(item, host, fmt)
    elif itype == "function_call_output":
        _render_function_call_output_history_item(
            item,
            host,
            fmt,
            call_id_to_name,
            call_id_to_tool_metadata,
        )
    elif itype == "reasoning":
        _render_reasoning_history_item(item, host, fmt)
    elif itype == "slash_command":
        _render_slash_command_history_item(item, host, fmt)


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _adapter as _sib_adapter
    from . import _approval as _sib_approval
    from . import _commands as _sib_commands
    from . import _context as _sib_context
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
    from . import _model as _sib_model
    from . import _overview as _sib_overview
    from . import _startup as _sib_startup
    for _key, _value in _sib_adapter.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_approval.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_commands.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_context.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_entry.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_helpers.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_model.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_overview.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_startup.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
