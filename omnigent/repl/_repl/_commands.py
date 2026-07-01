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

def _cmd(
    name: str,
    help_text: str,
) -> Callable[[SlashCommandHandler], SlashCommandHandler]:
    """Decorator to register a slash command."""

    def _register(fn: SlashCommandHandler) -> SlashCommandHandler:
        COMMANDS[name] = (help_text, fn)
        return fn

    return _register

@_cmd("/help", "Show this help")
async def _cmd_help(
    arg: str,  # noqa: ARG001 — dispatch-contract params (see COMMANDS docstring)
    session: Session,  # noqa: ARG001
    client: OmnigentClient,  # noqa: ARG001
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    from rich.text import Text

    lines = []
    for name, (desc, _) in COMMANDS.items():
        if name in ("/?", "/exit"):
            continue  # Skip aliases.
        lines.append(f"  [{fmt.accent}]{name}[/{fmt.accent}]  [{fmt.muted}]{desc}[/{fmt.muted}]")
    host.output(Text.from_markup("\n".join(lines)))

@_cmd("/theme", "Show/set terminal theme; /theme light or /theme dark")
async def _cmd_theme(
    arg: str,
    session: Session,  # noqa: ARG001 — dispatch-contract params
    client: OmnigentClient,  # noqa: ARG001 — dispatch-contract params
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """Show or explicitly set the TUI's light/dark palette.

    ``/theme`` (no args) shows the current theme and usage hint.
    ``/theme dark`` / ``/theme light`` sets explicitly with a preview.
    ``/theme default`` resets to the built-in default (light).
    """
    from omnigent_ui_sdk.terminal._theme import DARK_THEME, LIGHT_THEME
    from rich.text import Text

    from omnigent.repl._theme_picker import _build_preview, build_theme_confirmation

    value = arg.strip().lower()
    if not value:
        current = getattr(host, "theme", LIGHT_THEME).name
        host.output(Text.from_markup(f"  [{fmt.muted}]theme: {current}[/{fmt.muted}]"))
        host.output(
            Text.from_markup(
                f"  [{fmt.muted}]usage: /theme light · /theme dark · "
                f"/theme default to reset[/{fmt.muted}]"
            )
        )
        return
    if value in _THEME_CLEAR_ALIASES or value == "light":
        selected = LIGHT_THEME
    elif value == "dark":
        selected = DARK_THEME
    else:
        host.output(
            Text.from_markup("  [bold red]Invalid theme: expected light, dark, or default[/]")
        )
        return

    if value in _THEME_CLEAR_ALIASES:
        save_user_config(DEFAULT_USER_CONFIG)
    else:
        update_user_config(theme=selected.name)
    host.set_theme(selected)
    fmt.set_theme(selected)
    # Show confirmation + preview panel via host.output() so it
    # integrates cleanly with prompt-toolkit (no alternate screen,
    # no nested Application).
    host.output(build_theme_confirmation(selected))
    host.output(_build_preview(selected.name))

@_cmd("/effort", "Show/set reasoning effort; /effort lists options")
async def _cmd_effort(
    arg: str,
    session: Session,
    client: OmnigentClient,  # noqa: ARG001 — dispatch-contract params
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """Show or set the session-level reasoning effort override."""
    from rich.text import Text

    value = arg.strip().lower()
    if not value:
        current = getattr(session, "reasoning_effort", None)
        selected = current or "default"
        label = "reasoning effort: default" if current is None else f"reasoning effort: {current}"
        rendered_values = ", ".join(
            f"[{opt}]" if opt == selected else opt for opt in _EFFORT_VALUES
        )
        rendered_default = "[default]" if selected == "default" else "default"
        rendered_options = f"{rendered_values} {rendered_default}"
        host.output(Text.from_markup(f"  [{fmt.muted}]{label}[/{fmt.muted}]"))
        host.output(Text.from_markup(f"  [{fmt.muted}]options: {rendered_options}[/{fmt.muted}]"))
        return

    if value in _EFFORT_CLEAR_ALIASES:
        await _set_session_reasoning_effort(session, None)
        suffix = " (current response unchanged)" if session.is_streaming else ""
        host.output(
            Text.from_markup(
                f"  [{fmt.muted}]reasoning effort reset to agent default{suffix}[/{fmt.muted}]"
            )
        )
        return

    if value not in _EFFORT_VALUES:
        host.output(
            Text.from_markup(
                "  [bold red]Invalid effort: "
                f"{value} · expected none, minimal, low, medium, high, xhigh, max, or default[/]"
            )
        )
        return

    await _set_session_reasoning_effort(session, value)
    suffix = " (current response unchanged)" if session.is_streaming else ""
    host.output(
        Text.from_markup(
            f"  [{fmt.muted}]reasoning effort set to {value} "
            f"for future responses{suffix}[/{fmt.muted}]"
        )
    )

@_cmd("/model", "Show/set the LLM model for this session")
async def _cmd_model(
    arg: str,
    session: Session,
    client: OmnigentClient,  # noqa: ARG001 — dispatch-contract params
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """Show or set the session-level LLM model override.

    No-arg shows the active credential (model · provider · source) via
    :func:`describe_active_credential` plus the other configured
    providers. ``/model`` changes the *model within the active provider*
    only: ``/model <model>`` / ``/model <active-provider>/<model>``
    validate against the catalog (warn, never block) and set the override;
    a bare ``/model <active-provider>`` resolves that provider's default
    model. A value naming a **different** configured provider fails loud
    with guidance — switching the active provider mid-session is not wired
    (it goes through ``omnigent setup --no-internal-beta`` + a restart).
    ``/model default|off|reset`` clears the override.
    """
    from rich.text import Text

    value = arg.strip()
    if not value:
        from omnigent.onboarding.detected import effective_config_with_detected
        from omnigent.onboarding.provider_config import load_config

        current = getattr(session, "model_override", None)
        harness = _session_readout_harness(session)
        # Merge ambient detections so the readout names the provider that is
        # actually authenticating the turn (matching routing), never a guess.
        config = effective_config_with_detected(load_config())
        for line in _build_model_readout_lines(config, harness, current):
            host.output(Text.from_markup(f"  [{fmt.muted}]{line}[/{fmt.muted}]"))
        return

    if value.lower() in _MODEL_CLEAR_ALIASES:
        result = session.set_model_override(None)
        if inspect.isawaitable(result):
            await result
        suffix = " (current response unchanged)" if session.is_streaming else ""
        host.output(
            Text.from_markup(f"  [{fmt.muted}]model reset to agent default{suffix}[/{fmt.muted}]")
        )
        return

    # ``/model`` changes the *model* within the already-active provider. It
    # cannot switch the provider — that's resolved server-side from the
    # configured default / agent YAML, independently of the model override
    # (see _resolve_provider_for_build). So:
    #   - a value naming a DIFFERENT configured provider (by raw or friendly
    #     name) fails loud with guidance, instead of silently shipping a
    #     mismatched model string to the wrong provider's harness;
    #   - a bare value naming the ACTIVE provider resolves its default model;
    #   - anything else is treated as a model string within the active provider.
    from omnigent.onboarding.configure_models import kind_glyph, provider_display_name
    from omnigent.onboarding.detected import effective_config_with_detected
    from omnigent.onboarding.provider_config import (
        default_provider_for_harness,
        load_config,
    )

    # Merge ambient detections so the "active provider" the switch-guard
    # resolves matches what actually routes the turn.
    config = effective_config_with_detected(load_config())
    harness = _session_readout_harness(session)
    active = default_provider_for_harness(config, harness)
    active_name = active.name if active is not None else None

    candidate = value.split("/", 1)[0] if "/" in value else value
    matched = _match_configured_provider(config, candidate)
    if matched is not None and active_name is not None and matched != active_name:
        active_label = f"{kind_glyph(active.kind)} {provider_display_name(active_name)}".strip()
        target_label = f"{provider_display_name(matched)}"
        host.output(
            Text.from_markup(
                "  [bold red]Switching the active provider isn't supported mid-session.[/]"
            )
        )
        host.output(
            Text.from_markup(
                f"  [{fmt.muted}]Active provider: {active_label}. To use {target_label}, run "
                f"`omnigent setup --no-internal-beta` and select it as the "
                f"default, then restart. "
                f"(You can still change the model within {active_label}: /model <model-name>.)"
                f"[/{fmt.muted}]"
            )
        )
        return

    target = value
    if "/" not in value and matched is not None and matched == active_name:
        # Bare active-provider name → resolve its configured default model.
        resolved = _resolve_provider_default_model(config, matched)
        if resolved is None:
            # databricks / subscription pick their own model — nothing to set.
            host.output(
                Text.from_markup(
                    f"  [{fmt.muted}]{provider_display_name(matched)} picks the model itself; "
                    f"pass a specific one: /model <model-name>.[/{fmt.muted}]"
                )
            )
            return
        target = resolved
        host.output(
            Text.from_markup(
                f"  [{fmt.muted}]resolved provider {value!r} → {target}[/{fmt.muted}]"
            )
        )

    # Validate against the catalog — inform, but do NOT block (gateways and
    # brand-new models are legitimately absent from the bundled catalog, so
    # a non-catalog name is normal, not an error). Keep it a muted note.
    if "/" in target:
        warning = _model_validation_warning(target)
        if warning is not None:
            host.output(Text.from_markup(f"  [dim]note: {warning}[/dim]"))

    # set_model_override raises ValueError on empty-after-trim;
    # surface inline rather than letting it crash the REPL.
    try:
        result = session.set_model_override(target)
        if inspect.isawaitable(result):
            await result
    except ValueError as exc:
        host.output(Text.from_markup(f"  [bold red]Invalid model: {exc}[/]"))
        return

    suffix = " (current response unchanged)" if session.is_streaming else ""
    host.output(
        Text.from_markup(
            f"  [{fmt.muted}]model set to {target} for future responses{suffix}[/{fmt.muted}]"
        )
    )

@_cmd("/new", "Start a new conversation (keeps scrollback)")
async def _cmd_new(
    arg: str,  # noqa: ARG001 — dispatch-contract params
    session: Session,
    client: OmnigentClient,  # noqa: ARG001
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """Start a new conversation in place; the prior transcript stays on screen."""
    from rich.text import Text

    if not await _start_new_conversation(session, host, fmt):
        return
    # Humanize the agent name so the banner matches the initial
    # ``run_repl`` welcome (avoids a ``resume_test`` / ``resume test``
    # mismatch).
    host.output(fmt.welcome(_humanize_agent_name(session.model), hints=WELCOME_HINTS))
    host.output(Text.from_markup(f"\n  [{fmt.muted}]New conversation.[/{fmt.muted}]"))

@_cmd("/clear", "Clear the screen and start a new conversation")
async def _cmd_clear(
    arg: str,  # noqa: ARG001 — dispatch-contract params
    session: Session,
    client: OmnigentClient,  # noqa: ARG001
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """Clear the visible scrollback and start a new conversation.

    The prior conversation persists server-side and is resumable via
    ``/switch``.
    """
    from rich.text import Text

    if not await _start_new_conversation(session, host, fmt):
        return
    _clear_screen()
    host.output(fmt.welcome(_humanize_agent_name(session.model), hints=WELCOME_HINTS))
    host.output(Text.from_markup(f"\n  [{fmt.muted}]New conversation.[/{fmt.muted}]"))

@_cmd("/switch", "List or switch conversations")
async def _cmd_switch(
    arg: str,
    session: Session,
    client: OmnigentClient,
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    from datetime import datetime

    from rich.table import Table
    from rich.text import Text

    if not arg:
        sessions_list = await client.sessions.list(limit=20)
        if sessions_list:
            table = Table(title="Switch to…")
            table.add_column("#", style="bold " + fmt.accent)
            table.add_column("ID", style="dim")
            table.add_column("Title")
            table.add_column("Status", style="dim")
            table.add_column("Created", style="dim")
            for i, s in enumerate(sessions_list, 1):
                when = datetime.fromtimestamp(s.created_at).strftime("%b %d %H:%M")
                table.add_row(str(i), s.id, s.title or "(untitled)", s.status, when)
            host.output(table)
            host.output(
                Text.from_markup(f"  [{fmt.muted}]/switch <#> or <id> to resume[/{fmt.muted}]")
            )
        else:
            host.output(Text.from_markup(f"  [{fmt.muted}]No sessions.[/{fmt.muted}]"))
    else:
        if arg.isdigit():
            sessions_list = await client.sessions.list(limit=20)
            index = int(arg) - 1
            if index < 0 or index >= len(sessions_list):
                host.output(
                    Text.from_markup(
                        f"  [bold red]No session #{arg} "
                        f"({len(sessions_list)} listed). Run /switch with no "
                        f"argument to see the table.[/]"
                    )
                )
                return
            arg = sessions_list[index].id
        try:
            # Sessions mode: re-point the adapter (session_id,
            # runner PATCH, SSE pump) before re-rendering history.
            # session.reset() / resume_from_response() are no-ops
            # in sessions mode, so without this the REPL keeps
            # sending to the original session.
            await session.switch_to_session(arg)  # type: ignore[attr-defined]

            # ``/switch`` runs mid-session, so the user already
            # has prior-conversation transcript on screen —
            # redraw to clear that visual context and replace
            # it with the full target conversation rendered
            # below the welcome banner.
            await _attach_to_conversation(
                arg,
                session,
                client,
                host,
                fmt,
                ui_name=_humanize_agent_name(session.model),
                redraw_screen=True,
            )
        except Exception as exc:  # noqa: BLE001 — REPL UI boundary: render network/server errors as inline text so the REPL stays responsive instead of crashing
            host.output(Text.from_markup(f"  [bold red]Error: {exc}[/]"))

@_cmd("/fork", "Fork the current conversation into a new session")
async def _cmd_fork(
    arg: str,
    session: Session,
    client: OmnigentClient,
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """Fork the current session into a new session with copied items.

    Creates a server-side fork via ``POST /v1/sessions/{id}/fork``,
    then switches the REPL to the fork **in-place** — no screen
    clear, no transcript repaint. The fork is an exact copy of the
    conversation up to this point, so there is nothing new to render.

    The original session id is printed so the user can recover it
    via ``/switch``.

    :param arg: User-supplied text after ``/fork``.  Treated as the
        fork's title when non-empty, e.g. ``"experiment-2"``.
    :param session: The active REPL session (must expose
        ``session_id`` for sessions-API mode).
    :param client: Agent-plane HTTP client used to call the fork
        endpoint.
    :param host: Terminal host for rendering output messages.
    :param fmt: Rich block formatter for consistent styling.
    """
    from rich.text import Text

    # Only supported in sessions-API mode.
    current_id = getattr(session, "session_id", None)
    if current_id is None:
        host.output(
            Text.from_markup(
                "  [bold red]/fork requires the sessions API (not available in legacy mode).[/]"
            )
        )
        return

    title = arg.strip() or None
    try:
        result = await client.sessions.fork(current_id, title=title)
    except Exception as exc:  # noqa: BLE001 — REPL UI boundary: render server errors inline
        host.output(Text.from_markup(f"  [bold red]Fork failed: {exc}[/]"))
        return

    new_id = result["id"]

    # Switch the session adapter to the fork in-place.
    switch_fn = getattr(session, "switch_session", None)
    if switch_fn is not None:
        switch_fn(new_id)

    host.output(
        Text.from_markup(
            f"  [{fmt.muted}]Conversation forked. "
            f"To return to the previous conversation, run /switch {current_id}[/{fmt.muted}]"
        )
    )

@_cmd("/history", "Show current conversation history")
async def _cmd_history(
    arg: str,  # noqa: ARG001 — dispatch-contract params
    session: Session,
    client: OmnigentClient,
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    from rich.text import Text

    # Sessions-API mode: the adapter exposes the durable
    # session_id (== conversation_id) directly. Skip the
    # responses.get round-trip entirely.
    sessions_api_conv_id = getattr(session, "session_id", None)
    if sessions_api_conv_id is not None:
        try:
            items = await _list_all_conversation_items(
                client,
                sessions_api_conv_id,
            )
            call_id_to_tool_metadata = _build_call_id_to_tool_metadata_lookup(items)
            for item in items:
                _render_history_item(
                    item,
                    host,
                    fmt,
                    call_id_to_tool_metadata=call_id_to_tool_metadata,
                )
        except Exception as exc:  # noqa: BLE001 — REPL UI boundary: surface server errors inline
            host.output(Text.from_markup(f"  [bold red]Error: {exc}[/]"))
        return

    if not session.current_response_id:
        host.output(Text.from_markup(f"  [{fmt.muted}]No active conversation.[/{fmt.muted}]"))
        return
    try:
        resp = await client.responses.get(session.current_response_id)
        if resp.conversation:
            items = await _list_all_conversation_items(client, resp.conversation.id)
            call_id_to_tool_metadata = _build_call_id_to_tool_metadata_lookup(items)
            for item in items:
                _render_history_item(
                    item,
                    host,
                    fmt,
                    call_id_to_tool_metadata=call_id_to_tool_metadata,
                )
        else:
            host.output(Text.from_markup(f"  [{fmt.muted}]No conversation.[/{fmt.muted}]"))
    except Exception as exc:  # noqa: BLE001 — REPL UI boundary: render network/server errors as inline text so the REPL stays responsive instead of crashing
        host.output(Text.from_markup(f"  [bold red]Error: {exc}[/]"))

@_cmd("/compact", "Compact conversation context now")
async def _cmd_compact(
    arg: str,  # noqa: ARG001 — dispatch-contract params
    session: Session,
    client: OmnigentClient,  # noqa: ARG001 — dispatch-contract params
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """Request proactive context compaction for the current conversation."""
    from rich.text import Text

    if session.is_streaming:
        host.output(
            Text.from_markup(
                f"  [{fmt.muted}]Cannot compact while a response is running; "
                f"use /cancel or wait for it to finish.[/{fmt.muted}]"
            )
        )
        return
    compact = getattr(session, "compact", None)
    if not callable(compact):
        host.output(
            Text.from_markup(
                f"  [{fmt.muted}]This connection does not support /compact.[/{fmt.muted}]"
            )
        )
        return
    # Progress messages ("Compacting…" / "Compaction complete.") arrive
    # via the session SSE stream as response.compaction.in_progress /
    # response.compaction.completed events, so we don't output them here
    # — doing so would duplicate them for explicit /compact calls.
    try:
        result = compact()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001 — REPL boundary: keep prompt alive
        host.output(Text.from_markup(f"  [bold red]Compaction failed: {exc}[/]"))

@_cmd("/context", "Show context window usage")
async def _cmd_context(
    arg: str,  # noqa: ARG001 — dispatch-contract params
    session: Session,
    client: OmnigentClient,
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """
    Display context window usage for the current conversation.

    Delegates item fetching to :func:`_fetch_context_items` and
    rendering to :func:`_render_context_tree`. Falls back gracefully
    when the context window size is unknown (custom model or first turn
    before any overflow has been observed).

    :param arg: Ignored (dispatch-contract filler), e.g. ``""``.
    :param session: Current REPL session; provides agent name and
        optional model override.
    :param client: Agent-plane HTTP client used to fetch conversation
        items for token counting.
    :param host: Terminal host used to render the output.
    :param fmt: Formatter supplying the REPL colour names.
    """
    from rich.text import Text

    from omnigent.runtime.compaction import count_tokens

    agent_name = session.model
    # /model override wins; fall back to the spec model. ``is not None``
    # so an empty-string override doesn't silently fall through.
    _override = getattr(session, "model_override", None)
    llm_model: str | None = (
        _override if _override is not None else getattr(session, "llm_model", None)
    )

    # context_window is pre-computed server-side (litellm lookup) and
    # returned in SessionResponse — avoids requiring litellm client-side.
    context_window: int | None = getattr(session, "context_window", None)

    # Use the provider-reported token count from the most recently
    # completed response when available — it includes the system prompt,
    # tool schemas, and all messages, so it matches what the provider
    # will see as input_tokens on the next turn. Fall back to a local
    # count_tokens() estimate only when no response has completed yet
    # (e.g. before the very first turn in a fresh session).
    live_tokens: int | None = getattr(host, "tokens_used", None)
    if live_tokens is not None:
        message_tokens = live_tokens
    else:
        result = await _fetch_context_items(session, client)
        if result.error is not None:
            host.output(Text.from_markup(f"  [bold red]Error fetching history: {result.error}[/]"))
            return
        # count_tokens falls back to cl100k_base when agent_name isn't a
        # recognised LLM identifier — good enough for an estimate.
        effective_items = _items_for_context_token_count(result.items)
        message_tokens = count_tokens(
            [dict(item) for item in effective_items],  # type: ignore[arg-type]
            llm_model or agent_name,
        )
    _render_context_tree(agent_name, llm_model, message_tokens, context_window, host, fmt)

@_cmd("/cancel", "Cancel the current response")
async def _cmd_cancel(
    arg: str,  # noqa: ARG001 — dispatch-contract params
    session: Session,
    client: OmnigentClient,  # noqa: ARG001
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    from rich.text import Text

    resp = await session.cancel()
    if resp:
        host.output(Text.from_markup(f"  [{fmt.warning}]Cancelled {resp.id}[/{fmt.warning}]"))

@_cmd("/logs", "Collect current session logs into a zip")
async def _cmd_logs(
    arg: str,
    session: Session,
    client: OmnigentClient,
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """Create a zip bundle containing logs for the active REPL session."""
    from rich.text import Text

    from omnigent.cli_diagnostics import current_cli_log_path
    from omnigent.repl._session_log import write_logs_zip, write_session_log

    output_path = pathlib.Path(arg).expanduser() if arg.strip() else None
    session_id: str | None = getattr(session, "session_id", None)
    log_paths: list[pathlib.Path] = []

    # Always-on CLI diagnostics are per process/invocation, so the
    # current path is session-scoped. Do not glob the whole logs dir.
    cli_log = current_cli_log_path()
    if cli_log is not None:
        log_paths.append(cli_log)

    # These attributes are attached by run_repl for local diagnostics
    # that are specific to this REPL invocation/session.
    for attr in ("_event_log_path", "_server_log_path", "_runner_log_path"):
        value = getattr(session, attr, None)
        if isinstance(value, pathlib.Path):
            log_paths.append(value)

    # Include a fresh JSON transcript of the active session. This is
    # the only file we create here; all other entries are explicit
    # per-invocation paths. A fresh REPL with no turns has no session
    # id yet, so it simply omits the transcript instead of sweeping
    # unrelated old conversation logs.
    if session_id:
        try:
            transcript = await write_session_log(
                client,
                session_id,
                agent_name=session.model,
                log_dir=None,
            )
            log_paths.append(transcript)
        except Exception as exc:  # noqa: BLE001 — slash-command UI boundary
            _log.exception("Session transcript write failed for /logs")
            host.output(
                Text.from_markup(
                    f"  [{fmt.muted}]Could not write session transcript "
                    f"({type(exc).__name__}: {exc}); bundling available logs.[/{fmt.muted}]"
                )
            )

    path, count = write_logs_zip(output_path, log_paths=log_paths, session_id=session_id)
    conversation_label = session_id or "(none yet)"
    host.output(
        Text.from_markup(
            f"  [{fmt.muted}]Collected {count} current-session log file"
            f"{'s' if count != 1 else ''} into {path}\n"
            f"  Conversation ID: {conversation_label}[/{fmt.muted}]"
        )
    )

@_cmd("/report", "Open a pre-filled GitHub issue for this session")
async def _cmd_report(
    arg: str,
    session: Session,
    client: OmnigentClient,  # noqa: ARG001 — dispatch-contract param
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """Open a GitHub issue pre-filled with the current session context."""
    import platform
    import webbrowser
    from importlib.metadata import version as _pkg_version

    from rich.text import Text

    session_id: str | None = session.session_id if hasattr(session, "session_id") else None

    try:
        version = _pkg_version("omnigent")
    except Exception:  # noqa: BLE001
        version = None

    os_info = f"{platform.system()} {platform.release()}".strip() or None

    url = _build_github_issue_url(
        session_id=session_id,
        agent_name=session.model,
        description=arg,
        version=version,
        os_info=os_info,
    )
    opened = webbrowser.open(url)
    if opened:
        host.output(
            Text.from_markup(f"  [{fmt.muted}]Opening GitHub issue in browser…[/{fmt.muted}]")
        )
    else:
        host.output(
            Text.from_markup(
                f"  [{fmt.muted}]Could not open browser. "
                f"Copy this URL to file an issue:[/{fmt.muted}]"
            )
        )
        host.output(Text(f"  {url}"))

@_cmd("/quit", "Exit")
async def _cmd_quit(
    arg: str,  # noqa: ARG001 — dispatch-contract params
    session: Session,  # noqa: ARG001
    client: OmnigentClient,  # noqa: ARG001
    host: TerminalHost,
    fmt: RichBlockFormatter,  # noqa: ARG001
) -> None:
    host.request_exit()

def _consume_pending_local_skill_slash_command(
    session: object,
    item: dict[str, object],
) -> bool:
    """
    Consume a matching locally echoed skill slash command.

    The command handler echoes the user's local ``/<skill>`` input
    immediately. When the server later publishes the durable
    ``slash_command`` item, the live TUI should skip exactly one
    matching item for this session while still rendering commands
    that came from another client.

    :param session: REPL session object, usually
        :class:`_SessionsChatReplAdapter`.
    :param item: Live ``slash_command`` item from
        ``response.output_item.done``.
    :returns: ``True`` when a matching pending local echo was found
        and removed.
    """
    pending = getattr(session, "_pending_local_skill_slash_commands", None)
    if not isinstance(pending, list):
        return False
    command_key = (
        str(item.get("name") or ""),
        str(item.get("arguments") or ""),
    )
    for idx, pending_key in enumerate(pending):
        if pending_key == command_key:
            del pending[idx]
            return True
    return False

def register_skill_commands(skills: list[SkillSpec]) -> list[str]:
    """
    Auto-register each discovered skill as a REPL slash command.

    For every :class:`SkillSpec` whose ``/<name>`` does not collide
    with an existing built-in command, a handler is added to the
    global :data:`COMMANDS` registry. Collisions are skipped with a
    warning log so built-in commands always win.

    :param skills: The agent's parsed skill list.
    :returns: List of registered command names (e.g. ``["/code-review"]``).
        Callers should pass this to :func:`unregister_skill_commands`
        on exit to prevent leaking into subsequent ``run_repl`` calls.
    """
    registered: list[str] = []
    for skill in skills:
        cmd_name = f"/{skill.name}"
        if cmd_name in COMMANDS:
            _log.warning(
                "Skill %r skipped: /%s collides with a built-in command",
                skill.name,
                skill.name,
            )
            continue

        def _make_handler(sk: SkillSpec) -> SlashCommandHandler:
            """Build a slash-command handler for a single skill."""

            async def _skill_handler(
                arg: str,
                session: Session,
                client: OmnigentClient,  # noqa: ARG001 — dispatch-contract params
                host: TerminalHost,
                fmt: RichBlockFormatter,
            ) -> None:
                send_skill = getattr(session, "send_skill_slash_command", None)
                if not callable(send_skill):
                    raise RuntimeError("Skill slash commands require the sessions API adapter")
                if arg:
                    host.output(Text.from_markup(f"  [{fmt.muted}]/{sk.name}[/{fmt.muted}]"))
                    host.output(Text.from_markup(""))
                    host.output(fmt.user_message(arg))
                else:
                    host.output(
                        Text.from_markup(f"  [{fmt.muted}]Loading skill {sk.name}…[/{fmt.muted}]")
                    )
                host.start_timer()
                await asyncio.sleep(0)
                async for _ in send_skill(sk.name, arg):
                    pass

            return _skill_handler

        handler = _make_handler(skill)
        COMMANDS[cmd_name] = (skill.description, handler)
        registered.append(cmd_name)
        _log.debug("Registered skill slash command: %s", cmd_name)

    return registered

def unregister_skill_commands(names: list[str]) -> None:
    """Remove previously registered skill commands from the global registry."""
    for name in names:
        COMMANDS.pop(name, None)

class _SlashCommandCompleter(Completer):
    """
    Suggest registered slash commands as the user types.

    Trigger conditions are kept parallel to the dispatcher in
    :func:`run_repl.on_input` so the popup only fires when the
    typed text would actually be routed as a command. Suggestions
    come from :data:`COMMANDS` at call time (not import time), so
    new ``@_cmd`` registrations appear without rewiring.
    """

    def get_completions(
        self,
        document: Document,
        complete_event: CompleteEvent,  # noqa: ARG002 — Completer protocol contract
    ) -> Iterable[Completion]:
        """
        Yield :class:`Completion` entries matching the current input.

        :param document: prompt-toolkit's input-buffer view; this
            method only reads ``document.text_before_cursor``
            (e.g. ``"/he"``).
        :param complete_event: prompt-toolkit trigger metadata
            (manual vs. while-typing). Unused — the popup is cheap
            so we always return the same set.
        :returns: One :class:`prompt_toolkit.completion.Completion`
            per matching slash command, in :data:`COMMANDS` order.
        """
        text_before = document.text_before_cursor
        # Trigger only inside the first token: ``hey /tmp`` is chat,
        # not a command. Path-like ``/Users/foo`` is parallel with
        # the dispatcher's guard in :func:`run_repl.on_input`.
        if " " in text_before or "\n" in text_before:
            return
        if not text_before.startswith("/"):
            return
        if "/" in text_before[1:]:
            return

        prefix = text_before.lower()
        for name, (desc, _) in COMMANDS.items():
            if name in _SLASH_COMMAND_ALIASES:
                continue
            if not name.startswith(prefix):
                continue
            yield Completion(
                text=name,
                # Replace everything typed so far so the splice
                # produces ``/help``, not ``//help``.
                start_position=-len(text_before),
                display=name,
                display_meta=desc,
            )

async def handle_slash_command(
    line: str,
    session: Session,
    client: OmnigentClient,
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """
    Dispatch a slash command from the registry.

    :param line: Raw user input line, e.g. ``"/switch"``.
    :param session: Current REPL session.
    :param client: Agent-plane client used by command handlers.
    :param host: Terminal host used for rendering command output.
    :param fmt: Formatter carrying the REPL style names.
    :returns: None.
    """
    from rich.text import Text

    parts = line.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    entry = COMMANDS.get(cmd)
    if entry:
        _, handler = entry
        try:
            await handler(arg, session, client, host, fmt)
        except Exception as exc:  # noqa: BLE001
            # Slash commands run in prompt-toolkit background tasks, so
            # render failures inline instead of letting asyncio log an
            # unretrieved task exception.
            _log.exception("Slash command failed: %s", cmd)
            host.output(Text(f"  Error: {exc}", style="bold red"))
    else:
        host.output(
            Text.from_markup(
                f"  [{fmt.muted}]Unknown command: {cmd} · /help for list[/{fmt.muted}]"
            )
        )


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _adapter as _sib_adapter
    from . import _approval as _sib_approval
    from . import _context as _sib_context
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
    for _key, _value in _sib_render.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_startup.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
