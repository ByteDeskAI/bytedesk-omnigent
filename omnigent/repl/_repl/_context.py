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

async def _start_new_conversation(
    session: Session,
    host: TerminalHost,
    fmt: RichBlockFormatter,  # noqa: ARG001 — reserved for future banner styling
) -> bool:
    """Tear down the current session; legacy mode falls back to sync ``reset()``.

    :returns: ``False`` if the server unbind PATCH failed (rendered
        inline); caller skips the welcome banner redraw.
    """
    from rich.text import Text

    starter = getattr(session, "start_new_conversation", None)
    if callable(starter):
        try:
            await starter()
        except Exception as exc:  # noqa: BLE001 — REPL boundary
            _log.exception("New conversation failed")
            host.output(Text.from_markup(f"  [bold red]New conversation failed: {exc}[/]"))
            return False
    else:
        session.reset()
    return True

async def _attach_to_conversation(
    conversation_id: str,
    session: Session,
    client: OmnigentClient,
    host: TerminalHost,
    fmt: RichBlockFormatter,
    *,
    ui_name: str,
    redraw_screen: bool,
) -> None:
    """
    Attach the current REPL session to an existing conversation
    and re-render its complete history.

    Fetches every item in the conversation (paginating past the
    server's per-request 100-item cap), threads new turns onto
    the last response_id, and renders the conversation in full
    using the same :class:`RichBlockFormatter` the live stream
    uses — so a resumed conversation looks identical to the
    transcript the user originally saw, with full tool-call
    args, result panels, reasoning panels, and untruncated
    assistant text.

    Used by:

    - The ``/switch <id>`` slash command (interactive switch
      mid-session) — passes ``redraw_screen=True`` because the
      previous conversation's transcript is visible above the
      input prompt and needs to be cleared before re-rendering
      the welcome banner + new conversation.
    - ``run_repl(resume_conversation_id=...)`` on startup
      (``--continue`` / ``--resume <id>``; see
      designs/RUN_OMNIGENT_SESSION_RESUMPTION.md) — passes
      ``redraw_screen=False`` because the welcome banner has
      already been drawn by ``run_repl`` and there's nothing
      else on screen to replace.

    Both call sites render the FULL conversation, not a tail —
    truncating to a "preview" lost too much context (tool args,
    result content, multi-turn reasoning) and surprised users
    coming back to long sessions.

    :param conversation_id: The conversation to attach to.
    :param session: The active REPL session (gets ``reset()``
        + ``resume_from_response()`` called on it).
    :param client: The :class:`OmnigentClient` for fetching
        items.
    :param host: The :class:`TerminalHost` to render against.
    :param fmt: The :class:`RichBlockFormatter` driving styling.
    :param ui_name: The display-formatted agent name shown in
        the welcome banner when *redraw_screen* is True.
        Callers compute this consistently with the initial
        banner — typically via :func:`_humanize_agent_name`.
    :param redraw_screen: When True, clear the screen and
        re-render the welcome banner before re-rendering the
        conversation. When False, only print the "Resumed
        conversation …" line — appropriate when the banner is
        already on screen.
    :raises Exception: Network / server errors propagate;
        callers render them as inline REPL output.
    """
    from rich.text import Text

    # Fail loud on a bad session id: _list_all_conversation_items
    # silently falls back to the legacy items endpoint (which returns
    # [] for missing conversations), hiding the 404 until first send.
    if hasattr(session, "session_id"):
        await client.sessions.get(conversation_id)

    # Eagerly bind THIS REPL's runner and start the SSE pump so
    # turns posted from the web UI / another client stream into the
    # local REPL right away — without this, they only surface after
    # the local user sends a message and triggers the lazy bind.
    # Idempotent: a later ``send()`` short-circuits in ``_ensure_session``.
    ensure = getattr(session, "_ensure_session", None)
    if callable(ensure):
        await ensure()

    items = await _list_all_conversation_items(client, conversation_id)

    last_response_id = None
    for item in reversed(items):
        rid = item.get("response_id")
        if isinstance(rid, str):
            last_response_id = rid
            break
    if last_response_id is None:
        # An empty conversation (no response items yet). On a fresh
        # `omnigent run` the daemon hands the REPL a freshly-created session
        # as the resume target, so this is the normal new-session case — the
        # old "Empty conversation." line was misleading noise at the top of
        # every new run. Render nothing extra on the startup path (the welcome
        # header is already on screen); on the interactive `/switch` path,
        # redraw the welcome header so the screen still reflects the switch.
        if redraw_screen:
            _clear_screen()
            host.output(fmt.welcome(ui_name, hints=WELCOME_HINTS))
        return
    session.reset()
    session.resume_from_response(last_response_id)
    if redraw_screen:
        _clear_screen()
        host.output(fmt.welcome(ui_name, hints=WELCOME_HINTS))
    host.output(
        Text.from_markup(
            f"  [{fmt.muted}]Resumed conversation {conversation_id[:16]}…[/{fmt.muted}]\n"
        )
    )

    # Pre-pass: build a call_id → tool metadata lookup so
    # ``function_call_output`` items (which only carry
    # ``call_id``, never ``name`` / ``arguments``) can be rendered
    # by the same pretty tool renderers as the live stream.
    call_id_to_tool_metadata = _build_call_id_to_tool_metadata_lookup(items)
    for item in items:
        _render_history_item(
            item,
            host,
            fmt,
            call_id_to_tool_metadata=call_id_to_tool_metadata,
        )

    # Seed the toolbar ring immediately on resume so it reflects the
    # existing context usage without waiting for the first idle event.
    # ``items`` is already fetched above — no extra API call needed.
    cw = getattr(session, "context_window", None)
    if cw:
        last_total = getattr(session, "_last_total_tokens", None)
        if last_total is not None:
            # Use the provider-reported total_tokens from the most
            # recent completed task. This includes system prompt +
            # tool schemas + messages, so it matches what the provider
            # will see as input_tokens on the next turn — far more
            # accurate than a local count_tokens() estimate.
            tokens = last_total
        else:
            from omnigent.runtime.compaction import count_tokens

            effective = _items_for_context_token_count(items)
            llm = getattr(session, "llm_model", None) or getattr(session, "_agent_name", "")
            tokens = count_tokens(
                [dict(i) for i in effective],  # type: ignore[arg-type]
                llm,
            )
        host.update_context_usage(tokens, cw)

@dataclass
class _ContextItems:
    """
    Result of the conversation-item fetch for ``/context``.

    :param items: Conversation item dicts fetched from the server.
    :param error: User-facing error string if the fetch failed, or
        ``None`` on success.
    """

    items: list[dict[str, object]]
    error: str | None

async def _fetch_context_items(
    session: Session,
    client: OmnigentClient,
) -> _ContextItems:
    """
    Fetch conversation items for the current REPL session.

    Mirrors the fetch logic in :func:`_cmd_history`: tries the
    sessions-API path (``session_id``) first, then falls back to the
    legacy responses path (``current_response_id``).

    :param session: Current REPL session; exposes ``session_id`` and
        ``current_response_id`` for the two fetch paths.
    :param client: Agent-plane HTTP client used to query items.
    :returns: A :class:`_ContextItems` with populated ``items`` on
        success or a non-``None`` ``error`` string on failure.
    """
    sessions_api_conv_id = getattr(session, "session_id", None)
    if sessions_api_conv_id is not None:
        try:
            items = await _list_all_conversation_items(client, sessions_api_conv_id)
            return _ContextItems(items=items, error=None)
        except Exception as exc:  # noqa: BLE001 — REPL UI boundary: surface inline
            return _ContextItems(items=[], error=str(exc))

    if session.current_response_id:
        try:
            resp = await client.responses.get(session.current_response_id)
            if resp.conversation:
                items = await _list_all_conversation_items(client, resp.conversation.id)
                return _ContextItems(items=items, error=None)
        except Exception as exc:  # noqa: BLE001 — REPL UI boundary
            return _ContextItems(items=[], error=str(exc))

    return _ContextItems(items=[], error=None)

def _items_for_context_token_count(
    items: list[dict[str, object]],
) -> list[dict[str, object]]:
    """
    Return the effective prompt history represented by conversation items.

    Raw conversation storage keeps older items even after compaction and
    appends a metadata ``type=compaction`` item. Runtime prompt loading uses
    that metadata as a cursor and sends only a synthetic summary pair plus
    items after ``last_item_id``. ``/context`` should report that same
    effective prompt, not the raw archival transcript.

    :param items: Chronological conversation items from the server.
    :returns: Compaction-aware items suitable for token counting.
    """
    latest_compaction = next(
        (item for item in reversed(items) if item.get("type") == "compaction"),
        None,
    )
    if latest_compaction is None:
        return [item for item in items if item.get("type") not in {"resource_event"}]

    last_item_id = latest_compaction.get("last_item_id")
    summary = latest_compaction.get("summary")
    if not isinstance(last_item_id, str) or not isinstance(summary, str):
        return [item for item in items if item.get("type") not in {"compaction", "resource_event"}]

    boundary_index = -1
    for idx, item in enumerate(items):
        if item.get("id") == last_item_id:
            boundary_index = idx
            break
    recent_items = items[boundary_index + 1 :] if boundary_index >= 0 else []
    content_recent = [
        item for item in recent_items if item.get("type") not in {"compaction", "resource_event"}
    ]
    summary_items: list[dict[str, object]] = [
        {
            "type": "message",
            "role": "user",
            "content": (
                "[This is an automatically generated summary of the prior "
                "conversation context.]\n\n"
                "Please provide a summary of our conversation so far."
            ),
        },
        {
            "type": "message",
            "role": "assistant",
            "content": summary,
        },
    ]
    return summary_items + content_recent

async def _refresh_session_metadata(
    session: Session,
    client: OmnigentClient,
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """
    Re-sync client-side session metadata from a fresh server snapshot.

    Fired in the background from two triggers. The session's bound
    agent — and with it ``llm_model`` / ``harness`` /
    ``context_window`` / ``model_override`` — can change between turns
    when another client switches the agent in place
    (``POST /v1/sessions/{id}/switch-agent``). The server publishes a
    ``session.agent_changed`` stream event for the switch (the live
    trigger), but the event is transient SSE-only with no replay — one
    landing in a stream-pump reconnect gap or before this REPL attached
    is lost — so each turn start re-fires the refresh as the catch-up
    trigger. Both paths re-derive state from the snapshot rather than
    applying event payloads. When the agent name changed, the toolbar
    label and window title are updated and a muted notice line is
    rendered.

    :param session: Sessions-API adapter; must expose ``session_id``,
        ``model``, and ``_hydrate_from_session_snapshot``. Legacy
        sessions without those attributes are a no-op.
    :param client: Omnigent HTTP client used to fetch the snapshot.
    :param host: Terminal host whose toolbar label is updated.
    :param fmt: Active formatter; supplies the muted style for the
        switch notice.
    :returns: None.
    """
    session_id = getattr(session, "session_id", None)
    hydrate = getattr(session, "_hydrate_from_session_snapshot", None)
    if session_id is None or hydrate is None:
        return
    old_name = session.model
    try:
        snap = await client.sessions.get(session_id)
    except Exception:  # noqa: BLE001 — background refresh at the REPL UI boundary: a stale toolbar beats an unhandled-task traceback
        return
    hydrate(snap)
    new_name = session.model
    if new_name != old_name:
        host.set_model_name(_humanize_agent_name(new_name))
        host.output(
            Text.from_markup(
                f"  [{fmt.muted}]Agent switched: {_humanize_agent_name(old_name)} → "
                f"{_humanize_agent_name(new_name)}[/{fmt.muted}]"
            )
        )

async def _update_context_ring_estimate(
    session: Session,
    client: OmnigentClient,
    host: TerminalHost,
    context_window: int,
) -> None:
    """
    Update the toolbar context ring from a local token-count estimate.

    Fallback for turn-idle when the provider reported no usage (e.g.
    native harnesses). Fetches the conversation items, reduces them to
    the compaction-aware effective prompt, counts tokens, and pushes
    the result to the host's context ring. A failed item fetch leaves
    the ring untouched.

    :param session: Current REPL session; exposes ``llm_model`` (the
        spec-pinned LLM id, ``None`` for native harnesses) and
        ``model`` (the agent name, e.g. ``"claude-native-ui"``).
    :param client: Omnigent HTTP client used to query items.
    :param host: Terminal host whose context ring is updated.
    :param context_window: Context window size in tokens, e.g. ``200_000``.
    :returns: None.
    """
    from omnigent.runtime.compaction import count_tokens

    result = await _fetch_context_items(session, client)
    if result.error is not None:
        return
    effective = _items_for_context_token_count(result.items)
    # Fall back to the agent name when the spec pins no LLM model
    # (native-harness agents); count_tokens maps unknown names to
    # cl100k_base. Both values are read from the session at call
    # time, not captured at task-spawn time, so they stay current.
    llm = getattr(session, "llm_model", None) or session.model
    tokens = count_tokens(
        [dict(i) for i in effective],  # type: ignore[arg-type]
        llm,
    )
    host.update_context_usage(tokens, context_window)


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _adapter as _sib_adapter
    from . import _approval as _sib_approval
    from . import _commands as _sib_commands
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
    from . import _model as _sib_model
    from . import _overview as _sib_overview
    from . import _render as _sib_render
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
    for _key, _value in _sib_render.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_startup.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
