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

async def run_repl(
    client: OmnigentClient,
    agent_name: str,
    tool_handler: ToolHandler | None,
    *,
    initial_message: str | None = None,
    resume_conversation_id: str | None = None,
    log_dir: pathlib.Path | None = None,
    debug_events: bool = False,
    server_log_path: pathlib.Path | None = None,
    runner_log_path: pathlib.Path | None = None,
    session_bundle: bytes | None = None,
    session_bundle_filename: str = "agent.tar.gz",
    runner_id: str | None = None,
    runner_recover: Callable[[], str] | None = None,
    resume_parts: list[str] | None = None,
    ephemeral: bool = False,
    skills: list[SkillSpec] | None = None,
    server_url: str | None = None,
    on_session_start: Callable[[str], None] | None = None,
    harness: str | None = None,
    agent_description: str | None = None,
    used_families: list[str] | None = None,
    attach_only: bool = False,
) -> str | None:
    """The entire REPL — using the framework.

    :param client: Connected OmnigentClient.
    :param agent_name: Agent name (used for API calls).
    :param tool_handler: Optional client-side tool handler.
    :param initial_message: If set, auto-send this message on startup
        (e.g. a greeting prompt for onboarding).
    :param resume_conversation_id: When set, the REPL opens
        attached to this existing session instead of creating a
        fresh session. Resolved upstream from ``--continue`` /
        ``--resume <id>`` (see designs/RUN_OMNIGENT_SESSION_RESUMPTION.md).
        ``None`` opens a fresh session through ``POST /v1/sessions``.
    :param log_dir: When set, write a JSON dump of the active
        conversation to ``{log_dir}/{timestamp}-{conv_short}.json``
        on REPL exit. ``None`` (default) skips the dump. Maps to
        the CLI ``--log`` flag (and the ``~/.omnigent/logs/``
        default location); see ``omnigent.repl._session_log`` for
        the schema. The dump runs in the SAME ``async with
        OmnigentClient(...)`` scope as the REPL itself, so the
        client is still connected when we fetch the conversation +
        items. Failures are logged to stderr but do NOT propagate —
        a write error at REPL exit shouldn't take the user's
        terminal down.
    :param debug_events: When ``True``, enable the SSE-to-UI debug
        pipeline: a ``Ctrl+E`` event tape overlay, JSONL event
        logging to ``~/.omnigent/debug/``, and pipeline stage
        counters in the toolbar. Maps to ``--debug-events`` on the
        CLI.
    :param session_bundle: Gzipped agent bundle bytes used to
        create a fresh sessions-API session. Required when
        ``resume_conversation_id`` is ``None``.
    :param session_bundle_filename: Filename for the multipart
        upload, e.g. ``"agent.tar.gz"``.
    :param runner_id: Registered runner id to bind before the first
        turn, e.g. ``"runner_0123456789abcdef"``.
    :param runner_recover: Optional callback that returns the current
        runner id, restarting the local runner if it has exited.
    :param resume_parts: Pre-built argument list prefix for the
        resume hint, e.g. ``["omnigent", "run", "agent.yaml",
        "--server", "https://example.com"]``.  Built from Click's
        parsed context at CLI dispatch time so one-shot flags
        (``-p``, ``--fork``, ``-c``) are already excluded.
        ``None`` omits the resume hint on exit.
    :param ephemeral: When ``True``, suppress the resume hint on
        exit — the session data lives in a tmpdir that won't
        survive process exit, so the hint would be misleading.
    :param skills: Parsed skill list from the agent spec, e.g.
        ``[SkillSpec(name="code-review", ...)]``. Each skill is
        registered as a ``/<name>`` slash command at REPL startup.
        ``None`` (default) means no skill commands are registered.
    :param server_url: Base URL of the Omnigent server the REPL is
        connected to. Surfaced in the welcome banner when it
        points at a non-loopback host so the user can see which
        workspace they're talking to. ``None`` omits it.
    :param on_session_start: Optional callback invoked once when
        the top-level session id is known, e.g.
        ``lambda session_id: open_url(session_id)``.
    :param harness: The launch harness derived from the local spec,
        e.g. ``"claude-sdk"`` — used to name the model + credential in
        the startup header and the ``/model`` readout. ``None`` for a
        remote-URL target (no local spec).
    :param agent_description: The agent spec's ``description``, surfaced
        as the one-line summary row in the startup header, e.g.
        polly's ``"Multi-agent coding orchestrator. …"``. ``None``
        omits the summary row.
    :param used_families: Provider families the agent's harnesses (incl.
        sub-agents) consume, e.g. ``["anthropic", "openai"]`` for
        polly. A multi-family agent gets a per-family creds line under
        the startup header. ``None`` / a single family omits that line.
    :returns: The conversation id from the last active conversation,
        or ``None`` if the user exited before any conversation was
        created (e.g. immediate Ctrl-D).
    """
    # Register skill-based slash commands before the input loop
    # so they appear in autocomplete and /help from the start.
    # Track the registered names so we can clean them up on exit
    # and avoid leaking into subsequent run_repl calls.
    _registered_skill_cmds: list[str] = []
    if skills:
        _registered_skill_cmds = register_skill_commands(skills)

    ui_name = _humanize_agent_name(agent_name)
    theme = _load_startup_theme()
    fmt = TimedFormatter(show_agent_labels=True, theme=theme)
    # Pass ``WELCOME_HINTS`` for the bottom toolbar. Without
    # this the bar only showed "esc cancel · ctrl+c exit",
    # leaving ``/help`` and the Ctrl+O overlay invisible to
    # users.
    # ``window_title`` mirrors the legacy CLI's terminal-title
    # behavior (omnigent/inner/cli.py:2979 + :2984): when a user
    # has multiple agent sessions open across tabs, the tab bar
    # should show which agent is which. Without this, every tab
    # reads "Terminal" / "$SHELL" and there's no way to tell them
    # apart short of switching to each one.
    # When debug-events is on, add the Ctrl+E hint to the welcome
    # panel and toolbar so the user knows the overlay exists.
    hints = list(WELCOME_HINTS)
    if debug_events:
        hints.insert(1, "Ctrl+E events")
    host = TerminalHost(
        model_name=ui_name,
        toolbar_hints=hints,
        window_title=ui_name,
        # Live popup for registered slash commands and @-mention
        # file completion.  ``merge_completers`` yields results from
        # both completers — only one fires per keystroke because
        # their trigger characters (``/`` vs ``@``) don't overlap.
        completer=merge_completers([_SlashCommandCompleter(), FileMentionCompleter()]),
        theme=theme,
    )

    # ── Debug event pipeline (--debug-events) ──────────────────
    # Lazily initialized only when the flag is set so zero overhead
    # in the normal path. The tape and counters are closed over by
    # the instrumented renderers below.
    from omnigent.repl._event_tape import EventTape, PipelineCounters, TapeEntry

    _event_tape: EventTape | None = None
    _event_log_fh: TextIO | None = None  # file handle for JSONL log
    _event_log_path: pathlib.Path | None = None  # JSONL log path, shown in Ctrl+O
    _pipeline_counters: PipelineCounters | None = None
    if debug_events:
        _pipeline_counters = PipelineCounters()
        _event_tape = EventTape(counters=_pipeline_counters)
        host.pipeline_counters = _pipeline_counters  # type: ignore[attr-defined]

    # Ctrl+T: toggle tool-output panels in the formatter.
    def _toggle_tool_output() -> None:
        fmt.show_tool_output = not fmt.show_tool_output
        # Update the toolbar hint to reflect the new state.
        new_label = "Ctrl+T hide tools" if fmt.show_tool_output else "Ctrl+T show tools"
        for i, h in enumerate(host._toolbar_hints):
            if h.startswith("Ctrl+T "):
                host._toolbar_hints[i] = new_label
                break

    host.on_toggle_tool_output = _toggle_tool_output

    # Wire the policy-ASK seam into the session so any policy
    # in the agent's spec that returns ASK surfaces an inline
    # y/n prompt here. The hook lives on the session so every
    # turn in this REPL benefits — no per-call re-registration.
    # Shared state couples the hook (which awaits a future) to
    # the main input loop (which resolves it); reusing the
    # normal prompt_toolkit input path avoids the stdin /
    # patch_stdout fight that a direct input() call produced.
    approval_state = _ApprovalState()
    hooks = StreamHooks(
        on_elicitation_request=_make_elicitation_prompt(
            host, fmt, approval_state, server_url=server_url
        ),
    )
    # Build the tool_callables map from the legacy ToolHandler
    # when present so client-side tool tunneling still works.
    # The ToolHandler's ``execute`` callable matches the
    # SessionsChat ToolCallable contract closely enough; the
    # name → callable indirection is what SessionsChat expects.
    tool_callables: dict[str, object] | None = None
    if tool_handler is not None:
        tool_callables = {
            schema["name"]: tool_handler.execute  # type: ignore[index]
            for schema in tool_handler.schemas
            if isinstance(schema, dict) and "name" in schema
        }
    # ``Session`` typing here is intentional: the adapter
    # is duck-compatible with the legacy surface the REPL
    # uses (send/cancel/current_response_id/model/
    # is_streaming/reset/resume_from_response/
    # set_reasoning_effort/reasoning_effort). mypy is
    # appeased via the runtime cast; the static type
    # mismatch surfaces in tests, not at runtime.
    session = _SessionsChatReplAdapter(  # type: ignore[assignment]
        client=client,
        agent_name=agent_name,
        tool_callables=tool_callables,
        hooks=hooks,
        session_id=resume_conversation_id,
        session_bundle=session_bundle,
        session_bundle_filename=session_bundle_filename,
        runner_id=runner_id,
        runner_recover=runner_recover,
        on_session_start=on_session_start,
        harness=harness,
        attach_only=attach_only,
    )
    # Make per-invocation log paths visible to slash commands such as
    # /logs without broadening the slash-command dispatch signature.
    session._server_log_path = server_log_path  # type: ignore[attr-defined]
    session._runner_log_path = runner_log_path  # type: ignore[attr-defined]

    # True once any TextDelta has been rendered for the current
    # turn. Used to suppress the duplicate full-text that arrives
    # in output_item.done (type=message, role=assistant) after the
    # same prose already streamed as deltas.
    _saw_text_deltas = False
    # Streamed-prose bookkeeping for the relay's persisted-segment
    # publishes: matches an assistant ``message`` output item back to
    # prose that already streamed this turn so it isn't re-rendered as
    # a duplicate "◆ agent + text" block. See :class:`_TurnProseTracker`.
    _prose_tracker = _TurnProseTracker()
    # Tracks whether the most-recent ResponseCompleted event carried
    # provider-reported usage. Reset to False at each "running" status
    # (new turn begins) so the idle-event local-estimate fallback fires
    # on every turn for harnesses that never report usage (e.g. codex).
    _context_ring_state: list[bool] = [False]  # [last_completed_had_usage]

    def _flush_inflight_assistant_text() -> list[FormattedItem]:
        """
        Commit in-flight streamed assistant text at a content-block boundary.

        A single turn can contain several assistant text blocks
        interleaved with tool calls. The streaming executor emits the
        prose as ``TextDelta`` events and the tool calls as inline
        ``function_call`` output items, but it emits the
        ``message`` (role=assistant) output item only once, after all
        deltas — never between consecutive text blocks. The formatter's
        paragraph buffer is reset by ``format_message_done`` only at
        that boundary, so when a tool call interrupts streamed prose the
        buffer keeps accumulating across blocks and the live region
        re-renders the whole turn's prose — prefixed with a fresh
        ``◆`` — on every later delta and tool round (the "growing
        preamble" duplication).

        Flushing here commits the in-flight text whenever a concrete
        non-text item is about to render, so the next block starts from
        an empty buffer. Idempotent: a no-op when no deltas have
        streamed since the last commit. Resets ``_saw_text_deltas`` so
        the next block's first delta re-arms it.

        :returns: The ``StreamReplace`` items emitted by
            ``format_message_done`` (empty when nothing was in flight or
            the text ended on a paragraph boundary). The caller forwards
            these to the event tape for audit accounting.
        """
        nonlocal _saw_text_deltas
        if not _saw_text_deltas:
            return []
        flush_items = list(fmt.format_message_done())
        for it in flush_items:
            host.output(it)
        # Remember the committed segment's text so the relay's
        # persisted-item publish for it (an assistant ``message``
        # output_item.done arriving after a tool call reset
        # ``_saw_text_deltas``) is recognized as already rendered.
        _prose_tracker.commit_segment()
        _saw_text_deltas = False
        return flush_items

    def _spawn_metadata_refresh() -> None:
        """
        Fire a background re-sync of session metadata from a snapshot.

        Shared by the two triggers that can observe an in-place agent
        switch: the ``session.agent_changed`` stream event (live, while
        attached) and the turn-start catch-up in the ``running`` status
        branch. Both funnel into :func:`_refresh_session_metadata` so
        adapter state is always derived from a snapshot — never applied
        piecemeal from event payloads.

        :returns: None.
        """
        _refresh_task = asyncio.create_task(_refresh_session_metadata(session, client, host, fmt))
        _background_event_tasks.add(_refresh_task)
        _refresh_task.add_done_callback(_background_event_tasks.discard)

    def _render_session_event(event: object) -> None:
        """Push-based renderer for all session stream events.

        Called by the pump for every event, both during
        ``send()`` (user-initiated turn) and between sends
        (autonomous turns). Handles:

        * ``TextDelta`` -> streamed text via formatter
        * ``OutputItemDoneEvent`` -> tool calls/results via
          ``_render_history_item``; client-side tool dispatch
          for ``action_required``
        * ``ResponseCreated`` -> response header + track id
        * Terminal events -> signal ``_turn_done``
        * ``ElicitationRequest`` -> approval hook
        * ``ClientTaskCancelEvent`` -> cancel local tool task
        """
        nonlocal _saw_text_deltas

        from omnigent_client._events import (
            ElicitationRequest as _Elicit,
        )
        from omnigent_client._events import (
            ResponseCreated as _Created,
        )
        from omnigent_client._events import (
            TextDelta as _TD,
        )

        from omnigent.server.schemas import (
            ClientTaskCancelEvent as _Cancel,
        )
        from omnigent.server.schemas import (
            OutputItemDoneEvent as _OIDE,
        )
        from omnigent.server.schemas import (
            SessionAgentChangedEvent as _AgentChangedEv,
        )
        from omnigent.server.schemas import (
            SessionInputConsumedEvent as _SICEv,
        )
        from omnigent.server.schemas import (
            SessionStatusEvent as _StatusEv,
        )

        tape_entry = None
        if _event_tape is not None:
            tape_entry = _event_tape.record_raw(event, path="sessions")

        if isinstance(event, _StatusEv):
            if tape_entry is not None:
                _event_tape.update_translation(tape_entry, event)  # type: ignore[union-attr]
            if event.status == "running":
                from omnigent_client import BlockContext, ResponseStartBlock

                _saw_text_deltas = False
                # New turn: drop the prior turn's streamed-segment
                # bookkeeping so its prose can't suppress a later,
                # legitimately identical assistant message.
                _prose_tracker.reset_turn()
                _context_ring_state[0] = False  # reset: new turn, provider usage unknown yet
                host.start_timer()
                # Local name distinct from run_repl's `agent_name` param:
                # assigning to `agent_name` here would shadow it for the
                # whole handler, leaving it unbound in other branches.
                current_agent = session._agent_name  # type: ignore[union-attr]
                items_out = list(
                    fmt.format_response_start(
                        ResponseStartBlock(
                            model=current_agent,
                            response_id="",
                            ctx=BlockContext(agent=current_agent, depth=0, turn=0),
                        ),
                    )
                )
                if tape_entry is not None:
                    _event_tape.update_format(tape_entry, items_out)  # type: ignore[union-attr]
                for item in items_out:
                    host.output(item)
                if tape_entry is not None and items_out:
                    _event_tape.mark_rendered(tape_entry, len(items_out))  # type: ignore[union-attr]
                # The bound agent (and its llm_model / harness /
                # context_window) can change between turns via an
                # in-place agent switch from another client. The
                # session.agent_changed branch below catches that live,
                # but the event is transient (no replay) — one landing in
                # a stream-pump reconnect gap or before this REPL
                # attached is lost — so also re-sync at each turn start.
                _spawn_metadata_refresh()
            elif event.status in ("idle", "failed"):
                from omnigent_client import TextDone

                # A SETUP-phase failure (spec resolution, spawn-env
                # build) ends the turn before the LLM stream starts, so
                # no response.failed / ErrorEvent ever arrives — the only
                # signal is this terminal ``failed`` status. Render its
                # error message as an error line; without this the turn
                # ends silently and the user sees the spinner vanish with
                # no output. The helper falls back to a generic message
                # when the event carries no error detail.
                if event.status == "failed":
                    err_items = _render_failed_status_error(fmt, host, event)
                    if tape_entry is not None and err_items:
                        _event_tape.mark_rendered(tape_entry, len(err_items))  # type: ignore[union-attr]

                items_out = list(
                    fmt.format_text_done(TextDone(full_text="", has_code_blocks=False))
                )
                if tape_entry is not None:
                    _event_tape.update_format(tape_entry, items_out)  # type: ignore[union-attr]
                for item in items_out:
                    host.output(item)
                if tape_entry is not None and items_out:
                    _event_tape.mark_rendered(tape_entry, len(items_out))  # type: ignore[union-attr]
                host.stop_timer()
                turn_done = getattr(session, "_turn_done", None)
                if turn_done is not None:
                    turn_done.set()
                # Fall back to a local token-count estimate only when the
                # provider didn't report usage for this turn.  Prefer the
                # provider-reported value (set by ResponseCompleted via
                # host.update_context_usage) over the local estimate —
                # the local estimate counts conversation history, not the
                # real input window fill.
                _cw = getattr(session, "context_window", None)
                if _cw and not _context_ring_state[0]:
                    _ring_task = asyncio.create_task(
                        _update_context_ring_estimate(session, client, host, _cw)
                    )
                    _background_event_tasks.add(_ring_task)
                    _ring_task.add_done_callback(_background_event_tasks.discard)
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        if isinstance(event, _AgentChangedEv):
            if tape_entry is not None:
                _event_tape.update_translation(tape_entry, event)  # type: ignore[union-attr]
                _maybe_log_tape_entry(tape_entry)
            # Another client switched the session's agent in place. The
            # event is a trigger, not a data source — it carries no
            # llm_model / harness / context_window, and state must come
            # from one place — so re-derive from a fresh snapshot (the
            # refresh renders the "Agent switched" notice).
            _spawn_metadata_refresh()
            return

        if isinstance(event, _SICEv):
            if tape_entry is not None:
                _event_tape.update_translation(tape_entry, event)  # type: ignore[union-attr]
            if event.data.data.get("is_meta") is True:
                if tape_entry is not None:
                    _maybe_log_tape_entry(tape_entry)
                return
            if event.data.type == "message" and event.data.data.get("role") == "user":
                if session._pending_local_user_sends > 0:  # type: ignore[union-attr]
                    session._pending_local_user_sends -= 1  # type: ignore[union-attr]
                else:
                    text = _extract_message_text(event.data.data)
                    items_out = [fmt.user_message(text)]
                    if tape_entry is not None:
                        _event_tape.update_format(tape_entry, items_out)  # type: ignore[union-attr]
                    for item in items_out:
                        host.output(item)
                    if tape_entry is not None:
                        _event_tape.mark_rendered(tape_entry, len(items_out))  # type: ignore[union-attr]
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        # When an elicitation is resolved externally (via the standalone
        # approval page), the server publishes ``elicitation_resolved``.
        # Wake the parked ``_ApprovalState`` future so the REPL unblocks.
        # The ``elicitation_resolved`` event doesn't carry the verdict
        # (accept/decline), but ``_handle_elicitation`` will POST a
        # redundant resolve that the server silently ignores (already
        # resolved). We approve here so the REPL unblocks; the actual
        # outcome is whatever the user chose on the page.
        from omnigent.server.schemas import ElicitationResolvedEvent

        if isinstance(event, ElicitationResolvedEvent):
            if approval_state.pending:
                approval_state.resolve_verdict(_ApprovalVerdict.APPROVE_ONCE)
                host.output(
                    Text.from_markup(
                        f"   [{fmt.muted}]› resolved via approval page[/{fmt.muted}]",
                    ),
                )
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        sdk_ev = _server_event_to_sdk_event(event)
        if tape_entry is not None:
            _event_tape.update_translation(tape_entry, sdk_ev)  # type: ignore[union-attr]
        if sdk_ev is None:
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        if isinstance(sdk_ev, _TD):
            from omnigent_client import TextChunk

            _saw_text_deltas = True
            _prose_tracker.on_delta(sdk_ev.delta)
            items_out = list(fmt.format_text_chunk(TextChunk(text=sdk_ev.delta)))
            if tape_entry is not None:
                _event_tape.update_format(tape_entry, items_out)  # type: ignore[union-attr]
            for item in items_out:
                host.output(item)
            if tape_entry is not None and items_out:
                _event_tape.mark_rendered(tape_entry, len(items_out))  # type: ignore[union-attr]
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        from omnigent_client._events import (
            CompactionCompleted as _CC,
        )
        from omnigent_client._events import (
            CompactionInProgress as _CIP,
        )
        from omnigent_client._events import (
            ReasoningDelta as _RD,
        )
        from omnigent_client._events import (
            ReasoningStarted as _RS,
        )
        from omnigent_client._events import (
            ReasoningSummaryDelta as _RSD,
        )

        if isinstance(sdk_ev, _CIP):
            items_out = [
                Text.from_markup(f"  [{fmt.muted}]Compacting conversation context…[/{fmt.muted}]")
            ]
            if tape_entry is not None:
                _event_tape.update_format(tape_entry, items_out)  # type: ignore[union-attr]
            for item in items_out:
                host.output(item)
            if tape_entry is not None and items_out:
                _event_tape.mark_rendered(tape_entry, len(items_out))  # type: ignore[union-attr]
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        if isinstance(sdk_ev, _CC):
            items_out = [Text.from_markup(f"  [{fmt.muted}]Compaction complete.[/{fmt.muted}]")]
            if tape_entry is not None:
                _event_tape.update_format(tape_entry, items_out)  # type: ignore[union-attr]
            for item in items_out:
                host.output(item)
            if tape_entry is not None and items_out:
                _event_tape.mark_rendered(tape_entry, len(items_out))  # type: ignore[union-attr]
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        if isinstance(sdk_ev, _RS):
            from omnigent_client import BlockContext, ReasoningStartBlock

            items_out = list(
                fmt.format_reasoning_start(
                    ReasoningStartBlock(
                        ctx=BlockContext(
                            agent=session._agent_name,  # type: ignore[union-attr]
                            depth=0,
                            turn=0,
                        ),
                    ),
                ),
            )
            if tape_entry is not None:
                _event_tape.update_format(tape_entry, items_out)  # type: ignore[union-attr]
            for item in items_out:
                host.output(item)
            if tape_entry is not None and items_out:
                _event_tape.mark_rendered(tape_entry, len(items_out))  # type: ignore[union-attr]
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        if isinstance(sdk_ev, _RD | _RSD):
            from omnigent_client import BlockContext
            from omnigent_client import ReasoningChunk as _RC

            items_out = list(
                fmt.format_reasoning_chunk(
                    _RC(
                        text=sdk_ev.delta,
                        ctx=BlockContext(
                            agent=session._agent_name,  # type: ignore[union-attr]
                            depth=0,
                            turn=0,
                        ),
                    ),
                ),
            )
            if tape_entry is not None:
                _event_tape.update_format(tape_entry, items_out)  # type: ignore[union-attr]
            for item in items_out:
                host.output(item)
            if tape_entry is not None and items_out:
                _event_tape.mark_rendered(tape_entry, len(items_out))  # type: ignore[union-attr]
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        if isinstance(sdk_ev, _OIDE):
            item = sdk_ev.item
            if isinstance(item, dict):
                if (
                    item.get("type") == "function_call"
                    and item.get("status") == "action_required"
                    and session._tool_callables  # type: ignore[union-attr]
                ):
                    call_id = item.get("call_id", "")
                    name = item.get("name", "")
                    args_str = item.get("arguments", "{}")
                    sid = getattr(session, "session_id", None) or ""
                    if isinstance(call_id, str) and isinstance(name, str):
                        session._spawn_client_tool(  # type: ignore[union-attr]
                            sid,
                            call_id,
                            name,
                            str(args_str),
                        )
                item_type = item.get("type")
                if item_type == "slash_command" and _consume_pending_local_skill_slash_command(
                    session,
                    item,
                ):
                    if tape_entry is not None:
                        _event_tape.update_format(tape_entry, [])  # type: ignore[union-attr]
                        _maybe_log_tape_entry(tape_entry)
                    return
                # An assistant message whose prose is no longer in flight
                # may still be a duplicate: the relay publishes each
                # persisted text segment as an output_item.done AFTER the
                # tool-call boundary already committed the streamed prose
                # (resetting ``_saw_text_deltas``). Matching the item's
                # text against the turn's committed segments catches that
                # — see :class:`_TurnProseTracker`.
                _streamed_match = (
                    item_type == "message"
                    and item.get("role") == "assistant"
                    and not _saw_text_deltas
                    and _prose_tracker.consume_match(item)
                )
                plan = _plan_output_item_render(
                    item_type,
                    item.get("role"),
                    _saw_text_deltas or _streamed_match,
                )
                should_render = plan.render_item
                # When ``True``, the message-boundary flush below already
                # recorded the tape entry; the trailing ``elif`` must
                # not overwrite it with an empty marker.
                tape_handled = False
                if plan.flush_inflight_text and not should_render:
                    # Streamed deltas already rendered this assistant
                    # message; commit the trailing tail at the boundary
                    # instead of re-rendering the full item.
                    flush_items = _flush_inflight_assistant_text()
                    # The flush just recorded this segment's text; this
                    # item IS that segment's persisted copy, so consume
                    # the entry — a stale one could wrongly suppress a
                    # later identical (non-streamed) message this turn.
                    # Skipped when ``_streamed_match`` already consumed
                    # its entry above (the flush was then a no-op, and a
                    # second consume could eat a different identical
                    # segment's entry).
                    if not _streamed_match:
                        _prose_tracker.consume_match(item)
                    if tape_entry is not None:
                        _event_tape.update_format(tape_entry, flush_items)  # type: ignore[union-attr]
                        if flush_items:
                            _event_tape.mark_rendered(  # type: ignore[union-attr]
                                tape_entry,
                                len(flush_items),
                            )
                        tape_handled = True

                if should_render:
                    call_id_to_tool_metadata = getattr(
                        session,
                        "_live_call_id_to_tool_metadata",
                        None,
                    )
                    if call_id_to_tool_metadata is None:
                        call_id_to_tool_metadata = {}
                        session._live_call_id_to_tool_metadata = call_id_to_tool_metadata  # type: ignore[attr-defined]
                    if item_type == "function_call":
                        call_id = item.get("call_id")
                        name, arguments = _tool_metadata_from_function_call_item(item)
                        if isinstance(call_id, str) and name is not None:
                            call_id_to_tool_metadata[call_id] = (name, arguments or {})
                    if tape_entry is not None:
                        captured: list[object] = []
                        original_output = host.output

                        def _capturing_output(it: object) -> None:
                            captured.append(it)
                            original_output(it)

                        host.output = _capturing_output  # type: ignore[assignment]
                        try:
                            if plan.flush_inflight_text:
                                # Commit in-flight streamed prose before
                                # this tool call / output renders, so a
                                # new text block in the same turn doesn't
                                # append to (and re-render) the prior one.
                                _flush_inflight_assistant_text()
                            _render_history_item(
                                item,
                                host,
                                fmt,
                                call_id_to_tool_metadata=call_id_to_tool_metadata,
                            )
                        finally:
                            host.output = original_output  # type: ignore[assignment]
                        _event_tape.update_format(tape_entry, captured)  # type: ignore[union-attr]
                        _event_tape.mark_rendered(tape_entry, len(captured))  # type: ignore[union-attr]
                    else:
                        if plan.flush_inflight_text:
                            # See above: commit in-flight prose at the
                            # content-block boundary before rendering.
                            _flush_inflight_assistant_text()
                        _render_history_item(
                            item,
                            host,
                            fmt,
                            call_id_to_tool_metadata=call_id_to_tool_metadata,
                        )
                elif tape_entry is not None and not tape_handled:
                    _event_tape.update_format(tape_entry, [])  # type: ignore[union-attr]
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        if isinstance(sdk_ev, _Created):
            session._current_response_id = sdk_ev.response.id  # type: ignore[union-attr]
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        from omnigent_client._events import ResponseCompleted as _Completed

        if isinstance(sdk_ev, _Completed):
            # Update the toolbar context-ring with the best available
            # estimate of next-turn context fill.
            # ``context_tokens`` is populated by multi-call executors
            # (e.g. openai-agents) with the last sub-turn's total, which
            # correctly reflects context fill without over-counting the
            # repeated history across sub-turns. Single-call executors
            # don't set it, so we fall back to ``total_tokens`` (= input
            # + output for that one call), which is equally correct for
            # single-call turns.
            usage = sdk_ev.response.usage
            cw = getattr(session, "context_window", None)
            if usage is not None and cw:
                ring_tokens = getattr(usage, "context_tokens", None) or usage.total_tokens
                host.update_context_usage(ring_tokens, cw)
                _context_ring_state[0] = True  # provider reported usage; skip idle estimate
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        from omnigent_client._events import ErrorEvent as _ErrorEvent

        if isinstance(sdk_ev, _ErrorEvent):
            from omnigent_client import BlockContext, ErrorBlock

            items_out = list(
                fmt.format_error(
                    ErrorBlock(
                        message=sdk_ev.error.message,
                        source=sdk_ev.source,
                        ctx=BlockContext(agent=None, depth=0, turn=0),
                    ),
                )
            )
            if tape_entry is not None:
                _event_tape.update_format(tape_entry, items_out)  # type: ignore[union-attr]
            for item in items_out:
                host.output(item)
            if tape_entry is not None and items_out:
                _event_tape.mark_rendered(tape_entry, len(items_out))  # type: ignore[union-attr]
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        from omnigent_client._events import ResponseFailed as _Failed

        if isinstance(sdk_ev, _Failed):
            err = sdk_ev.response.error
            msg = err.message if err else "unknown error"
            from omnigent_client import BlockContext, ErrorBlock

            items_out = list(
                fmt.format_error(
                    ErrorBlock(
                        message=msg,
                        source="llm",
                        ctx=BlockContext(agent=None, depth=0, turn=0),
                    ),
                )
            )
            if tape_entry is not None:
                _event_tape.update_format(tape_entry, items_out)  # type: ignore[union-attr]
            for item in items_out:
                host.output(item)
            if tape_entry is not None and items_out:
                _event_tape.mark_rendered(tape_entry, len(items_out))  # type: ignore[union-attr]
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        if isinstance(sdk_ev, _Elicit):
            # A mirrored sub-agent prompt names the child session that
            # parked on it via ``target_session_id``; resolve there so the
            # verdict reaches the parked child rather than 404ing against
            # the ancestor stream this event was relayed onto.
            sid = _elicitation_resolve_session_id(
                sdk_ev, getattr(session, "session_id", None) or ""
            )
            elicit_task = asyncio.create_task(
                session._handle_elicitation(sid, sdk_ev),  # type: ignore[union-attr]
            )
            _background_event_tasks.add(elicit_task)
            elicit_task.add_done_callback(_background_event_tasks.discard)
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        if isinstance(sdk_ev, _Cancel):
            cid = sdk_ev.call_id
            pending = getattr(session, "_pending_local_tasks", {})
            if cid and cid in pending:
                task = pending[cid]
                if not task.done():
                    task.cancel()
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

    session._on_event = _render_session_event  # type: ignore[union-attr]

    def _maybe_log_tape_entry(entry: TapeEntry) -> None:
        """Write a tape entry to the JSONL log if the handle is open.

        :param entry: A :class:`TapeEntry` to log.
        """
        if _event_log_fh is not None:
            from omnigent.repl._event_tape import log_entry_jsonl

            log_entry_jsonl(_event_log_fh, entry)  # type: ignore[arg-type]

    is_streaming = False

    # Session id tracked for resume hints and session logs.
    conversation_id: str | None = resume_conversation_id
    # Active background event tasks, cancelled at REPL exit to prevent leaks.
    _background_event_tasks: set[asyncio.Task[None]] = set()

    def show_help() -> None:
        from rich.text import Text

        lines = []
        for name, (desc, _) in COMMANDS.items():
            if name in ("/?", "/exit"):
                continue  # Skip aliases.
            lines.append(
                f"  [{fmt.accent}]{name}[/{fmt.accent}]  [{fmt.muted}]{desc}[/{fmt.muted}]"
            )
        host.output(Text.from_markup("\n".join(lines)))

    host.on_help = show_help

    async def on_input(text: str, attachments: list[PendingAttachment] | None = None) -> None:
        nonlocal conversation_id, is_streaming

        # Pending policy approval: consume this input as the
        # verdict BEFORE slash-command / normal-send routing.
        # The hook is awaiting a future; resolving it wakes
        # the SSE stream. Echo the user's choice in dim so the
        # transcript makes sense on scrollback — otherwise a
        # bare "y" would look like an unrelated message.
        if approval_state.pending:
            if approval_state._url_mode:
                host.output(
                    Text.from_markup(
                        f"   [{fmt.muted}]waiting for approval via the URL above[/{fmt.muted}]",
                    ),
                )
                return
            verdict = _parse_approval_input(text)
            verdict_label = {
                _ApprovalVerdict.APPROVE_ONCE: "approved",
                _ApprovalVerdict.APPROVE_ALWAYS: "approved always (this session)",
                _ApprovalVerdict.REFUSE: "refused",
            }[verdict]
            host.output(
                Text.from_markup(
                    f"   [{fmt.muted}]› {verdict_label}[/{fmt.muted}]",
                ),
            )
            approval_state.resolve_verdict(verdict)
            return

        # Slash commands are short tokens like "/help", "/clear".
        # File paths like "/Users/foo/bar.jpg" start with "/" but
        # contain more path separators — don't treat those as commands.
        first_token = text.split()[0] if text.split() else ""
        if first_token.startswith("/") and "/" not in first_token[1:]:
            await handle_slash_command(text, session, client, host, fmt)
            return

        files = [a.path for a in attachments] if attachments else None
        cwd = os.getcwd()
        filenames = [os.path.relpath(a.path, cwd) for a in attachments] if attachments else None
        # The display text (``text``) has ``@path`` tokens stripped
        # — the ``📎`` chip shows them instead.  But the LLM needs
        # the paths inline so it knows which files are referenced.
        llm_text = text
        if filenames:
            suffix = " ".join(filenames)
            llm_text = f"{text} {suffix}".strip() if text else suffix

        if is_streaming:
            # Show the message immediately in dimmed style so the
            # user knows it sent, then steer the agent. Pad with
            # blank lines above AND below so the steering input
            # has visual breathing room — otherwise it wedges
            # directly between the streamed assistant text above
            # and the next tool-call block below. Mid-stream
            # steering has no response-start block to space off
            # against (the agent is already mid-turn), so the
            # caller-side trailing blank IS the only separator
            # before the next tool-call rendering.
            from rich.text import Text as RText

            host.output(RText.from_markup(""))
            host.output(fmt.steering_message(text, attachments=filenames))
            host.output(RText.from_markup(""))
            async for _ in session.send(llm_text, files=files):
                pass  # Steer yields nothing if delivered.
            return

        # Non-streaming (new-turn) user message: blank line
        # ABOVE the prompt to separate it from whatever was
        # there (prior turn, slash-command output, welcome
        # banner). No trailing blank: ``format_response_start``
        # in the SDK formatter already prefixes ``◆ model`` with
        # a ``\n``, so a blank here would stack into two blanks.
        from rich.text import Text as RText

        host.output(RText.from_markup(""))
        host.output(fmt.user_message(text, attachments=filenames))
        # Reset per-turn debug counters so the toolbar reflects only
        # events from the current turn, not cumulative history.
        if _event_tape is not None:
            _event_tape.reset_turn()

        host.start_timer()
        await asyncio.sleep(0)
        is_streaming = True
        try:
            # Sessions mode: send() just POSTs and waits for
            # _turn_done. All rendering is push-based via _on_event.
            async for _ in session.send(llm_text, files=files):
                pass
        except asyncio.CancelledError:
            # Escape key cancels this task. Tell the server to cancel
            # the in-progress response so the session state stays in
            # sync. Without this, _is_terminal stays False and the
            # next send() tries to steer a dead response.
            # shield() prevents the cancel() coroutine from being
            # re-cancelled by the propagating CancelledError.
            # Also refuse any pending approval fail-closed so the
            # hook's future doesn't leak waiting for a verdict
            # that will never come.
            approval_state.cancel()
            # Best-effort — server may already have finished.
            with contextlib.suppress(Exception):
                await asyncio.shield(session.cancel())
            from rich.text import Text as RText

            host.output(RText.from_markup(f"\n  [{fmt.muted}]cancelled[/{fmt.muted}]"))
            raise
        except Exception as exc:  # noqa: BLE001 — REPL UI boundary: any uncaught error here would be swallowed by prompt-toolkit's background runner, leaving the user staring at a silent prompt (see comment below for the concrete incident this guards against)
            # Any non-cancel exception from the server (HTTP 5xx from
            # ``raise_for_status``, transport errors, malformed SSE,
            # etc.) bubbles up through the session send path into
            # here. Without this branch, the exception propagates to
            # prompt-toolkit's background-task runner, which swallows
            # it silently — leaving the user staring at the prompt
            # with no idea why the agent produced no output. This
            # was the exact user-reported bug where ``omnigent chat`` would
            # return to the prompt after "Hello" with zero feedback
            # when ``OPENAI_BASE_URL`` was unset. Render as an
            # :class:`ErrorBlock` so the UI formatter surfaces the
            # panel it already knows how to draw for server-side
            # ``response.error`` events; users see a consistent error
            # UI regardless of whether the failure was pre-stream
            # (HTTP error) or mid-stream (ResponseFailed event).
            _log.exception("REPL send error (server/transport)")
            from omnigent_client import BlockContext, ErrorBlock

            host.output(RText.from_markup(""))  # separate from scrollback above
            host.output(
                fmt.format_error(
                    ErrorBlock(
                        message=str(exc),
                        source="server",
                        ctx=BlockContext(agent=None, depth=0, turn=0),
                    ),
                )[0],
            )
        finally:
            is_streaming = False
            host.stop_timer()
            conversation_id = getattr(session, "session_id", None)

    # Ctrl+O debug overview. Registered here — not inside the SDK —
    # because the content (conversation history, model metadata,
    # usage totals) is omnigent-specific. The SDK's
    # :class:`Overlay` primitive is intentionally content-agnostic;
    # the REPL owns what to render. See ``_build_debug_overview``
    # for the actual content.
    #
    # Why Ctrl+O and not Ctrl+G: Warp terminal (and some others)
    # intercepts Ctrl+G for its own AI Command Search before the
    # sequence reaches the running program, so the binding never
    # fires in `omnigent chat`'s pinned-prompt mode. Ctrl+O is not grabbed
    # by the common terminal emulators we target (iTerm2, Terminal.app,
    # Warp) and prompt-toolkit binds it cleanly.
    from omnigent_ui_sdk import Overlay

    async def _overview_builder(target: OverlayTarget) -> RenderableType:
        from omnigent.cli_diagnostics import current_cli_log_path

        return await _build_debug_overview(
            target,
            client=client,
            session=session,
            agent_name=agent_name,
            fmt=fmt,
            server_log_path=server_log_path,
            runner_log_path=runner_log_path,
            event_log_path=_event_log_path,
            cli_log_path=current_cli_log_path(),
        )

    async def _overview_targets() -> list[OverlayTarget]:
        return await _collect_overview_targets(client, session)

    # Per-target action keys for terminal targets. ``O`` opens
    # an attach in a fresh tmux window; ``R`` opens it
    # read-only. Capitalized so the binding doesn't fight with
    # lowercase letters that may be reserved for navigation /
    # search inside future overlay surfaces. Both no-op (with a
    # stderr message) when the selected target isn't a terminal
    # or when the user isn't running inside tmux to begin with —
    # there's nowhere to open the new window otherwise. Mirrors
    # the legacy non-AP mode F20-overlay shortcuts at
    # ``omnigent/inner/cli.py:1791-1797``.
    from omnigent_ui_sdk import OverlayAction

    async def _attach_handler(target: OverlayTarget, *, read_only: bool) -> None:
        await _open_terminal_in_tmux(
            target,
            client=client,
            read_only=read_only,
        )

    async def _attach_read_write(target: OverlayTarget) -> None:
        await _attach_handler(target, read_only=False)

    async def _attach_read_only(target: OverlayTarget) -> None:
        await _attach_handler(target, read_only=True)

    host.add_overlay(
        Overlay(
            trigger="c-o",
            builder=_overview_builder,
            targets_builder=_overview_targets,
            title=f" Debug overview — {ui_name}",
            actions=(
                OverlayAction(key="O", label="attach", handler=_attach_read_write),
                OverlayAction(key="R", label="attach (read-only)", handler=_attach_read_only),
            ),
        ),
    )

    # ── Ctrl+E event tape overlay (--debug-events only) ────────
    # Registered unconditionally only when the debug flag is set.
    # Uses the two-pane Overlay mode: the sidebar lists every tape
    # entry (type + delta + stage icon), and selecting one shows
    # the full detail panel — pipeline journey + raw JSON payload.
    if debug_events and _event_tape is not None:
        from omnigent.repl._event_tape import build_tape_detail, build_tape_targets

        async def _tape_builder(target: OverlayTarget | None) -> RenderableType:
            """Build the detail panel for the selected tape entry.

            :param target: The selected sidebar entry, or ``None``
                when no entries exist.
            :returns: Rich renderable for the detail panel.
            """
            if target is None:
                return Text.from_markup("[dim]No events recorded yet.[/dim]")
            return build_tape_detail(
                _event_tape,
                target.key,
                fmt,  # type: ignore[arg-type]
            )

        async def _tape_targets() -> list[OverlayTarget]:
            """Build the sidebar target list from the tape.

            :returns: One :class:`OverlayTarget` per tape entry.
            """
            from omnigent.repl._event_tape import _OverlayTargetLike

            raw_targets: list[_OverlayTargetLike] = build_tape_targets(
                _event_tape,  # type: ignore[arg-type]
            )
            return [OverlayTarget(key=t.key, label=t.label, icon=t.icon) for t in raw_targets]

        host.add_overlay(
            Overlay(
                trigger="c-e",
                builder=_tape_builder,
                targets_builder=_tape_targets,
                title=" SSE Event Tape",
                sidebar_width=30,
            ),
        )

    async with host:
        # ── Open JSONL event log when --debug-events is on ────
        # Opened inside ``async with host:`` so the finally block
        # that closes it is guaranteed to run even if host setup
        # succeeds but a later step fails.
        if debug_events:
            import time as _dbg_time

            from omnigent.repl._event_tape import open_event_log

            _sid = resume_conversation_id or f"fresh-{int(_dbg_time.time())}"
            _event_log_path = open_event_log(_sid)
            session._event_log_path = _event_log_path  # type: ignore[attr-defined]
            _event_log_fh = open(_event_log_path, "a")  # noqa: SIM115 — closed in finally below

        # Mirror the legacy CLI's mascot-art startup banner so the
        # Omnigent REPL feels identical at boot. Raw stdout write
        # (matching ``omnigent/inner/cli.py:2962``) — the banner
        # is a pre-formatted ANSI string with explicit centering;
        # routing it through ``host.output`` (which renders via a
        # Rich Console at the current terminal width) risks double-
        # padding or wrap surprises, and we don't need the SDK's
        # stream-state bookkeeping at REPL boot since nothing has
        # streamed yet. See
        # ``designs/RUN_OMNIGENT_REPL_PARITY.md``.
        import sys as _sys

        # Resolve the Claude-Code-style header data (folder, model,
        # credential, one-line summary, per-family creds). Best-effort:
        # the header reads the provider config, so on any failure we fall
        # back to the plain name-only banner rather than blocking boot.
        _header: _StartupHeader | None = None
        if attach_only:
            # Session-honest attach banner: agent name + harness + folder. No
            # host-local credential badge and no fresh-start "spawn sub-agents"
            # hint — this is a co-drive client joining the host's live session,
            # not the runner owner, so those would reflect the wrong machine.
            _header = _StartupHeader(
                folder=_display_cwd(),
                description=None,
                model_label=harness,
                credential=None,
                creds_line=None,
            )
        else:
            try:
                _header = _build_startup_header(harness, agent_description, used_families)
            except Exception:  # noqa: BLE001 — startup-UI boundary: a config read must never block REPL boot
                _log.exception("Failed to build startup header; falling back to plain banner")
        _sys.stdout.write(
            _render_startup_banner_ansi(ui_name, server_url=server_url, header=_header)
        )
        _sys.stdout.flush()

        from omnigent_ui_sdk import StreamingText

        host.output(StreamingText(text="\n\n\n"))
        # Resume an existing conversation when requested.
        # ``redraw_screen=False`` because the welcome banner
        # was just printed above — a second banner would
        # double-render and the cleared scrollback would push
        # the first banner off-screen, making the welcome
        # appear twice. ``ui_name`` is reused so any banner
        # text (the "Resumed conversation …" line) matches
        # the panel's display name and avoids the
        # ``resume_test`` / ``resume test`` mismatch.
        if resume_conversation_id is not None:
            try:
                await _attach_to_conversation(
                    resume_conversation_id,
                    session,
                    client,
                    host,
                    fmt,
                    ui_name=ui_name,
                    redraw_screen=False,
                )
                session._notify_session_start_once()
            except Exception as exc:  # noqa: BLE001 — REPL boundary: never crash on resume failure; render and proceed
                _log.exception("Failed to resume conversation %s", resume_conversation_id)
                host.output(
                    Text.from_markup(
                        f"  [bold red]Failed to resume {resume_conversation_id[:16]}…: {exc}[/]"
                    )
                )
        # Hold a reference to the auto-send task for the lifetime of
        # ``host.run`` — ``asyncio.create_task`` only weakly roots its
        # result, so dropping the handle would let the GC collect the
        # task mid-execution and the initial message could vanish.
        auto_send_task: asyncio.Task[None] | None = None
        if initial_message:
            # Auto-send the initial message (e.g. onboarding greeting).
            auto_send_task = asyncio.create_task(on_input(initial_message))
        try:
            await host.run(on_input)
        finally:
            if auto_send_task is not None and not auto_send_task.done():
                auto_send_task.cancel()
            for _task in list(_background_event_tasks):
                _task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await _task
            # Close the JSONL event log file handle if it was opened.
            if _event_log_fh is not None:
                _event_log_fh.close()  # type: ignore[union-attr]
    close_session = getattr(session, "aclose", None)
    if close_session is not None:
        result = close_session()
        if inspect.isawaitable(result):
            await result
    # Write the session log (--log) BEFORE the goodbye banner so the
    # user sees the path in their final scroll. The dump uses the
    # same connected client, so it runs inside the same async-with
    # scope that opened it. Failures are caught + reported on
    # stderr; the REPL still exits cleanly.
    if log_dir is not None:
        await _maybe_write_session_log(
            client,
            session,
            agent_name,
            log_dir,
            host,
            fmt,
        )
    # The sessions adapter tracks the durable session_id which doubles as
    # the conversation_id for resume purposes. Prefer it over the local
    # ``conversation_id`` fallback.
    conv_id = getattr(session, "session_id", None) or conversation_id
    # Top-level ``omnigent resume`` only dispatches claude-native today;
    # the REPL exit path is always chat/run, so print the original-invocation
    # form. ``resume_parts`` already carries --server etc.
    resume_hint: str | None = None
    if conv_id is not None and not ephemeral and resume_parts is not None:
        import shlex

        resume_hint = shlex.join([*resume_parts, "--resume", conv_id])
    host.output(fmt.goodbye(resume_hint=resume_hint))
    unregister_skill_commands(_registered_skill_cmds)
    return conv_id


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _adapter as _sib_adapter
    from . import _approval as _sib_approval
    from . import _commands as _sib_commands
    from . import _context as _sib_context
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
    for _key, _value in _sib_context.__dict__.items():
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
