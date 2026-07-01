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

def _elicitation_resolve_session_id(sdk_event: object, fallback_session_id: str) -> str:
    """
    Pick the session a parked elicitation's verdict must be POSTed to.

    A sub-agent's approval prompt is mirrored into its ancestors' streams
    so the human watching the parent chat sees it, but the elicitation
    Future lives on the *child* session that parked on it. The mirrored
    event carries the child id in ``target_session_id``; when present the
    verdict must go there, otherwise resolving against the ancestor stream
    404s and the sub-agent stays blocked. Own-session elicitations leave
    ``target_session_id`` unset and fall back to the stream's session.

    :param sdk_event: The translated
        :class:`omnigent_client._events.ElicitationRequest`; its
        ``target_session_id`` is read when set.
    :param fallback_session_id: The session the event was received on,
        e.g. ``"conv_parent123"``. Used when the elicitation is not a
        mirrored child prompt.
    :returns: The session id to resolve against, e.g. ``"conv_child123"``
        for a mirrored prompt or ``fallback_session_id`` otherwise.
    """
    return getattr(sdk_event, "target_session_id", None) or fallback_session_id

def _server_event_to_sdk_event(event: object) -> object | None:
    """Translate a server-shape ``ServerStreamEvent`` into an SDK-shape event.

    :class:`SessionsChat` yields validated server-side Pydantic
    events; the REPL renderer consumes the SDK-shape dataclasses in
    :mod:`omnigent_client._events`. Returns ``None`` for variants
    the renderer doesn't consume (forward-compatible skip).
    """
    from omnigent_client._events import (
        CompactionCompleted,
        CompactionInProgress,
        ElicitationRequest,
        ReasoningDelta,
        ReasoningStarted,
        ReasoningSummaryDelta,
        ResponseCancelled,
        ResponseCompleted,
        ResponseCreated,
        ResponseFailed,
        ResponseIncomplete,
        ResponseInProgress,
        ResponseQueued,
        TextDelta,
    )
    from omnigent_client._events import (
        ErrorEvent as SDKErrorEvent,
    )
    from omnigent_client._types import ErrorInfo
    from omnigent_client._types import Response as SDKResponse

    from omnigent.server.schemas import (
        CancelledEvent,
        ClientTaskCancelEvent,
        CompactionCompletedEvent,
        CompactionInProgressEvent,
        CompletedEvent,
        CreatedEvent,
        ElicitationRequestEvent,
        ErrorEvent,
        FailedEvent,
        IncompleteEvent,
        InProgressEvent,
        OutputItemDoneEvent,
        OutputTextDeltaEvent,
        QueuedEvent,
        ReasoningStartedEvent,
        ReasoningSummaryTextDeltaEvent,
        ReasoningTextDeltaEvent,
    )

    def _resp(env: object) -> SDKResponse:
        raw = env.response.model_dump()  # type: ignore[attr-defined]
        return SDKResponse.from_dict(raw)

    if isinstance(event, CreatedEvent):
        return ResponseCreated(response=_resp(event))
    if isinstance(event, QueuedEvent):
        return ResponseQueued(response=_resp(event))
    if isinstance(event, InProgressEvent):
        return ResponseInProgress(response=_resp(event))
    if isinstance(event, CompletedEvent):
        return ResponseCompleted(response=_resp(event))
    if isinstance(event, FailedEvent):
        return ResponseFailed(response=_resp(event))
    if isinstance(event, CancelledEvent):
        return ResponseCancelled(response=_resp(event))
    if isinstance(event, IncompleteEvent):
        resp = _resp(event)
        reason = resp.incomplete_details.reason if resp.incomplete_details else ""
        return ResponseIncomplete(response=resp, reason=reason)
    if isinstance(event, OutputTextDeltaEvent):
        return TextDelta(delta=event.delta)
    if isinstance(event, ReasoningStartedEvent):
        return ReasoningStarted()
    if isinstance(event, ReasoningTextDeltaEvent):
        return ReasoningDelta(delta=event.delta)
    if isinstance(event, ReasoningSummaryTextDeltaEvent):
        return ReasoningSummaryDelta(delta=event.delta)
    if isinstance(event, ElicitationRequestEvent):
        params = event.params
        return ElicitationRequest(
            elicitation_id=event.elicitation_id,
            message=params.message,
            requested_schema=params.requestedSchema or {},
            mode=params.mode,
            phase=params.phase or "",
            policy_name=params.policy_name or "",
            content_preview=params.content_preview or "",
            url=params.url,
            # Mirrored sub-agent prompts carry the child session id so the
            # verdict is POSTed back to the child that parked on it, not
            # the ancestor stream the event was relayed onto.
            target_session_id=params.target_session_id,
        )
    if isinstance(event, CompactionInProgressEvent):
        return CompactionInProgress()
    if isinstance(event, CompactionCompletedEvent):
        return CompactionCompleted()
    if isinstance(event, ErrorEvent):
        return SDKErrorEvent(
            source=event.source,
            tool_name=event.tool_name,
            error=ErrorInfo(
                code=event.error.code,
                message=event.error.message,
            ),
        )
    # OutputItemDoneEvent and ClientTaskCancelEvent are returned
    # as-is (not translated to SDK events) — the adapter handles
    # them directly for client-side tool execution.
    if isinstance(event, (OutputItemDoneEvent, ClientTaskCancelEvent)):
        return event
    return None

class _SessionsChatReplAdapter:
    """
    Sessions-API adapter for the REPL.

    Drives all server I/O through ``/v1/sessions``. A persistent
    SSE stream pump pushes every event through an ``_on_event``
    callback (set by :func:`run_repl`) that renders directly to
    the terminal. ``send()`` just POSTs the user message and
    waits for the turn-terminal event.

    Duck-compatible with the legacy :class:`Session` surface
    (``send``, ``cancel``, ``model``, ``current_response_id``,
    ``is_streaming``, ``reset``, ``resume_from_response``,
    ``set_reasoning_effort``, ``reasoning_effort``,
    ``set_model_override``, ``model_override``).
    """

    def __init__(
        self,
        client: OmnigentClient,
        agent_name: str,
        tool_callables: dict[str, object] | None = None,
        hooks: StreamHooks | None = None,
        session_id: str | None = None,
        session_bundle: bytes | None = None,
        session_bundle_filename: str = "agent.tar.gz",
        runner_id: str | None = None,
        runner_recover: Callable[[], str] | None = None,
        on_session_start: Callable[[str], None] | None = None,
        harness: str | None = None,
        attach_only: bool = False,
    ) -> None:
        """
        Wire the adapter; do NOT issue any HTTP calls.

        :param client: The :class:`OmnigentClient` used to
            build the chat helper on first :meth:`send`.
        :param agent_name: Human-readable agent name for
            display.
        :param tool_callables: Optional name → callable mapping
            for client-side tools. When present, the adapter
            detects ``action_required`` tool call events in the
            stream and executes them locally.
        :param hooks: Optional lifecycle hooks. The
            ``on_elicitation_request`` hook is invoked when the
            server emits an elicitation event.
        :param session_id: When set, attach to this existing
            session instead of creating a new one on first
            :meth:`send`. Used by ``--continue`` / ``--resume``
            resume. ``None`` (default) creates a fresh session.
        :param session_bundle: Gzipped agent tarball bytes used to
            create a fresh session, e.g. bytes sent as the
            multipart ``bundle`` part. Required when
            ``session_id`` is ``None``.
        :param session_bundle_filename: Filename for the multipart
            upload, e.g. ``"agent.tar.gz"``.
        :param runner_id: Registered runner id to bind before the
            first turn, e.g. ``"runner_0123456789abcdef"``.
        :param runner_recover: Optional callback that returns the
            currently online runner id, restarting the local runner
            first if needed.
        :param on_session_start: Optional callback invoked once
            after a session id is known, e.g. ``lambda id: ...``.
        :param harness: The launch harness (e.g. ``"codex"``), known
            locally from the spec / ``--harness`` flag. Seeds the
            ``/model`` readout's harness so it's correct *before* the
            first turn binds the session (the snapshot's ``harness``
            then confirms/refreshes it). ``None`` for URL targets where
            the harness is only known after the snapshot.
        :param attach_only: When ``True``, run as a pure co-drive client:
            never bind/recover a runner (turns post to the session's
            existing host-bound runner). Used by ``omnigent attach``.
            ``False`` (default) is the runner-owning ``run`` path.
        """
        self._client = client
        self._agent_id: str | None = None
        self._agent_name = agent_name
        self._tool_callables = tool_callables
        self._hooks = hooks or StreamHooks()
        self._session_id: str | None = session_id
        self._session_bundle = session_bundle
        self._session_bundle_filename = session_bundle_filename
        self._runner_id = runner_id
        self._runner_recover = runner_recover
        # Attach/co-drive mode: this client does NOT own a runner. It posts
        # turns to the session's already-bound runner (the host's), exactly
        # like the web UI co-drive, and never PATCHes the runner binding —
        # binding is owner-only, and re-binding would be a no-op even for the
        # owner. ``attach_only`` short-circuits all runner bind/recover logic.
        self._attach_only = attach_only
        self._on_session_start = on_session_start
        self._session_start_notified = False
        self._bound_runner_id: str | None = None
        # Push-based event callback. The pump calls this for every
        # event — always, regardless of whether send() is active.
        # Set by run_repl() to the rendering callback.
        self._on_event: Callable[[object], None] | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._recover_task: asyncio.Task[None] | None = None
        self._recover_lock = asyncio.Lock()
        self._bind_lock = asyncio.Lock()
        # Serializes _ensure_session so concurrent callers don't
        # race to create duplicate sessions.
        self._ensure_session_lock = asyncio.Lock()
        self._last_runner_recovery_error_key: tuple[str, str, str, str] | None = None
        self._current_response_id: str | None = None
        self._is_streaming: bool = False
        self._reasoning_effort: str | None = None
        # Session-local /model override. This is an LLM model
        # override (not an agent switch) applied by this adapter when
        # dispatching future turns through the sessions route.
        self._model_override: str | None = None
        self._llm_model: str | None = None
        self._harness: str | None = harness
        self._context_window: int | None = None
        self._last_total_tokens: int | None = None
        self._pending_local_tasks: dict[str, asyncio.Task[None]] = {}
        # FIFO counter: local sends are already echoed by ``on_input``,
        # so their ``session.input.consumed`` events are suppressed.
        self._pending_local_user_sends: int = 0
        # Locally invoked skill commands are echoed immediately by
        # the command handler; suppress the matching visible
        # ``slash_command`` item when it arrives on the live stream.
        self._pending_local_skill_slash_commands: list[tuple[str, str]] = []

    async def _recover_runner_if_needed(self) -> None:
        """
        Refresh the local runner id from the recovery callback.

        The callback owns process supervision. The adapter only
        observes the returned runner id and clears its cached binding
        when that id changes so the next ``PATCH /v1/sessions/{id}``
        writes the new affinity.

        :returns: None.
        """
        if self._runner_recover is None:
            return
        async with self._recover_lock:
            runner_id = await asyncio.to_thread(self._runner_recover)
            if runner_id != self._runner_id:
                self._runner_id = runner_id
                self._bound_runner_id = None

    async def _runner_recover_watch(self) -> None:
        """
        Keep a resumed session bound to a live local runner.

        The CLI recovery callback owns process supervision. This
        watchdog only invokes it periodically while the REPL is open,
        then reuses the same last-write-wins session PATCH used by
        create and resume.

        :returns: None.
        """
        _dbg = bool(os.environ.get("OMNIGENT_SESSIONS_ADAPTER_DEBUG"))
        while True:
            try:
                await asyncio.sleep(1.0)
                await self._recover_runner_if_needed()
                if self._session_id is not None:
                    await self._bind_runner_if_needed()
                self._clear_runner_recovery_error()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _log.warning("Runner recovery watchdog failed", exc_info=exc)
                self._emit_runner_recovery_error_once(exc)
                if _dbg:
                    print(
                        f"[sessions-adapter] runner recovery watchdog failed: {exc!r}",
                        file=sys.stderr,
                        flush=True,
                    )

    @property
    def session_id(self) -> str | None:
        """
        The durable session id once :meth:`send` has run at least once.

        Exposed so the REPL's debug overview and conversation-item
        fetchers can look up the session_id directly from the
        adapter without round-tripping through ``responses.get``.

        :returns: The session id, e.g. ``"conv_abc123"``, or
            ``None`` if no send has happened yet.
        """
        return self._session_id

    @property
    def model(self) -> str:
        """
        Return the agent's human-readable name.

        :returns: The agent name, e.g. ``"hello-world"``.
        """
        return self._agent_name

    @property
    def current_response_id(self) -> str | None:
        """
        Most recent ``response.created`` id observed on the SSE stream.

        Updated inside :meth:`send` as :class:`ResponseCreated`
        events fly past. ``None`` before the first turn.

        :returns: The response id, e.g. ``"resp_abc123"``, or
            ``None`` if no turn has run yet.
        """
        return self._current_response_id

    @property
    def is_streaming(self) -> bool:
        """
        Whether a turn is currently being streamed.

        :returns: ``True`` while :meth:`send` is iterating its
            async generator; ``False`` before/after.
        """
        return self._is_streaming

    @property
    def reasoning_effort(self) -> str | None:
        """
        Per-session reasoning-effort hint.

        Reads the locally cached value; the authoritative copy
        lives on the server (set via ``PATCH /v1/sessions/{id}``
        in :meth:`set_reasoning_effort`).

        :returns: The effort string (e.g. ``"high"``) or
            ``None`` if unset.
        """
        return self._reasoning_effort

    @property
    def model_override(self) -> str | None:
        """
        Current per-session LLM model override, or ``None`` for the
        agent spec default.

        This mirrors the legacy SDK helper's property so the shared
        ``/model`` slash command works in the sessions-backed REPL.
        """
        return self._model_override

    async def set_model_override(self, model: str | None) -> None:
        """
        Set or clear the session-local LLM model override.

        Before the session exists, caches the requested value locally;
        :meth:`_ensure_session` then PATCHes it onto the session row
        immediately after ``POST /v1/sessions`` returns, so the first
        event's workflow already sees ``conv.model_override`` via the
        server-side fallback. After the session exists, persists
        through ``PATCH /v1/sessions/{id}`` (matching
        :meth:`set_reasoning_effort`) so the ap-web picker and the
        REPL stay in sync on the next snapshot read.

        :param model: New model identifier, e.g. ``"claude-opus-4-7"``,
            or ``None`` to clear to the agent default.
        :raises ValueError: If *model* is a string that is empty
            after trimming.
        """
        if model is not None:
            normalized = model.strip()
            if not normalized:
                raise ValueError("model override must be a non-empty string")
            model = normalized
        if self._session_id is None:
            self._model_override = model
            return
        session = await self._client.sessions.set_model_override(
            self._session_id,
            model_override=model,
        )
        self._model_override = session.model_override

    @property
    def llm_model(self) -> str | None:
        """
        LLM model identifier from the bound agent's spec.

        Populated from the server's ``SessionResponse.llm_model``
        field on the first successful session fetch. ``None`` until
        the session has been hydrated or when the agent has no
        explicit ``llm:`` block.

        :returns: The model identifier, e.g.
            ``"anthropic/claude-sonnet-4-6"``, or ``None``.
        """
        return self._llm_model

    @property
    def harness(self) -> str | None:
        """
        The bound agent's canonical harness, e.g. ``"openai-agents"``.

        Populated from the server's ``SessionResponse.harness`` on the first
        session fetch. The ``/model`` readout uses it to describe the active
        credential for the correct provider *family* instead of guessing
        from the model string. ``None`` until hydrated / when unavailable.

        :returns: The canonical harness name, or ``None``.
        """
        return self._harness

    @property
    def context_window(self) -> int | None:
        """
        Context window size in tokens for the bound agent's LLM.

        Populated from the server's ``SessionResponse.context_window``
        field (looked up server-side via litellm) on the first
        successful session fetch. ``None`` until the session has been
        hydrated or when the model is not found in the litellm
        registry.

        :returns: Token count, e.g. ``200_000``, or ``None``.
        """
        return self._context_window

    async def set_reasoning_effort(self, effort: str | None) -> None:
        """
        Set or clear session reasoning effort.

        Before the session exists, caches the requested value so it
        can be sent in the multipart ``POST /v1/sessions`` metadata.
        After creation/resume, persists through
        ``PATCH /v1/sessions/{id}`` and updates the cache from the
        authoritative server snapshot.

        :param effort: New effort, e.g. ``"high"``, or ``None``
            to clear.
        """
        if self._session_id is None:
            self._reasoning_effort = effort
            return
        session = await self._client.sessions.set_reasoning_effort(
            self._session_id,
            reasoning_effort=effort,
        )
        self._reasoning_effort = session.reasoning_effort

    async def compact(self) -> None:
        """
        Request explicit context compaction for the current session.

        :raises RuntimeError: If no session exists yet.
        """
        if self._session_id is None:
            raise RuntimeError("No active conversation to compact")
        await self._client.sessions.compact(self._session_id)

    def _hydrate_from_session_snapshot(self, session: _SessionSnapshot) -> None:
        """
        Copy mutable session fields from a sessions API snapshot.

        :param session: Snapshot returned by ``client.sessions``.
            It must expose ``agent_id``, ``agent_name``, ``runner_id``,
            ``reasoning_effort``, ``model_override``, ``llm_model``,
            ``harness``, ``context_window``, and ``last_total_tokens``
            attributes.
        :returns: None.
        """
        self._agent_id = session.agent_id
        # The agent name changes when the session is switched in place
        # to a different agent (web UI "Switch agent"). Don't clobber
        # the launch-time name when the snapshot omits it (old server
        # or unresolved agent row).
        if session.agent_name:
            self._agent_name = session.agent_name
        self._bound_runner_id = session.runner_id
        self._reasoning_effort = session.reasoning_effort
        self._model_override = session.model_override
        self._llm_model = session.llm_model
        # Don't clobber a launch-provided harness with a None from the
        # snapshot (e.g. an agent the server couldn't resolve a spec for).
        if session.harness is not None:
            self._harness = session.harness
        self._context_window = session.context_window
        self._last_total_tokens = session.last_total_tokens

    async def _ensure_session(self) -> str:
        """
        Lazily create the session and start the persistent stream.

        Serialized by ``_ensure_session_lock`` so that concurrent
        ``send()`` calls (rapid-fire messages before the first turn
        starts) don't each create a separate session and race on
        the runner-bind PATCH.

        Set ``OMNIGENT_SESSIONS_ADAPTER_DEBUG=1`` to trace
        construction + per-event flow on stderr.

        :returns: The durable session id, e.g. ``"conv_abc123"``.
        """
        _dbg = bool(os.environ.get("OMNIGENT_SESSIONS_ADAPTER_DEBUG"))
        await self._recover_runner_if_needed()
        if self._session_id is not None and self._stream_task is not None:
            return self._session_id
        async with self._ensure_session_lock:
            if self._session_id is not None and self._stream_task is not None:
                return self._session_id
            if self._session_id is None:
                if self._session_bundle is None:
                    raise RuntimeError(
                        "Sessions API fresh session creation requires a local agent bundle. "
                        "Start the REPL from `omnigent run <agent.yaml>` so the CLI can "
                        "upload the bundle through POST /v1/sessions."
                    )
                if _dbg:
                    print(
                        "[sessions-adapter] POST /v1/sessions multipart bundle",
                        file=sys.stderr,
                        flush=True,
                    )
                # Snapshot pre-create /model pick before hydration
                # clobbers it; PATCHed below since create() has no
                # model_override metadata field.
                pending_model_override = self._model_override
                session = await self._client.sessions.create(
                    self._session_bundle,
                    filename=self._session_bundle_filename,
                    reasoning_effort=self._reasoning_effort,
                    # Record the user's terminal cwd so the Web UI
                    # can show "running locally in <workspace>" for
                    # CLI sessions. Doesn't drive any behavior —
                    # CLI sessions don't bind to a host_id, so the
                    # ck_conversations_workspace_required_for_host
                    # constraint isn't active.
                    workspace=os.getcwd(),
                )
                self._session_id = session.id
                self._hydrate_from_session_snapshot(session)
                if pending_model_override is not None and session.model_override is None:
                    # PATCH the pre-session ``/model`` pick so the
                    # first event picks it up via conv.model_override.
                    # ``silent`` skips the tmux ``/model`` forward —
                    # the user already typed the command locally; we
                    # don't want a second copy injected into the pane.
                    try:
                        patched = await self._client.sessions.set_model_override(
                            self._session_id,
                            model_override=pending_model_override,
                            silent=True,
                        )
                        self._model_override = patched.model_override
                    except Exception:  # noqa: BLE001 — REPL boundary; log and clear
                        _log.warning(
                            "Failed to apply pending /model=%r to session %s; "
                            "clearing local cache.",
                            pending_model_override,
                            self._session_id,
                            exc_info=True,
                        )
                        self._model_override = None
                if _dbg:
                    print(
                        f"[sessions-adapter] session created id={self._session_id!r}",
                        file=sys.stderr,
                        flush=True,
                    )
            else:
                if _dbg:
                    print(
                        f"[sessions-adapter] resuming existing session id={self._session_id!r}",
                        file=sys.stderr,
                        flush=True,
                    )
                session = await self._client.sessions.get(self._session_id)
                self._hydrate_from_session_snapshot(session)
            await self._bind_runner_if_needed()
            if self._stream_task is None:
                self._stream_task = asyncio.create_task(
                    self._stream_pump(),
                    name=f"sessions-adapter-stream-{self._session_id}",
                )
            if self._runner_recover is not None and self._recover_task is None:
                self._recover_task = asyncio.create_task(
                    self._runner_recover_watch(),
                    name=f"sessions-adapter-recover-{self._session_id}",
                )
            self._notify_session_start_once()
            return self._session_id

    def _notify_session_start_once(self) -> None:
        """
        Invoke the session-start callback once after a session id is known.

        :returns: None.
        """
        if self._session_start_notified or self._session_id is None:
            return
        self._session_start_notified = True
        if self._on_session_start is not None:
            self._on_session_start(self._session_id)

    async def _bind_runner_if_needed(self) -> None:
        """
        Patch this session to the current registered runner.

        The sessions API has one dispatch precondition: a session
        must be bound to an online runner before a turn is posted.
        ``PATCH /v1/sessions/{id}`` is last-write-wins, so resume
        and recover use the same call as first bind.

        :raises RuntimeError: If the adapter has no session id or no
            runner id.
        """
        # Attach/co-drive clients never bind: they post turns to the
        # session's existing host-bound runner. Binding is owner-only
        # server-side, so a non-owner attach must not PATCH it.
        if self._attach_only:
            return
        async with self._bind_lock:
            if self._session_id is None:
                raise RuntimeError("Cannot bind runner before a session exists")
            if self._runner_id is None:
                raise RuntimeError(
                    "Sessions API dispatch requires a registered runner id. "
                    "Start through `omnigent run <agent>` or pass --server so the CLI "
                    "can launch and bind a runner."
                )
            if self._bound_runner_id == self._runner_id:
                self._clear_runner_recovery_error()
                return
            _dbg = bool(os.environ.get("OMNIGENT_SESSIONS_ADAPTER_DEBUG"))
            if _dbg:
                print(
                    f"[sessions-adapter] PATCH /v1/sessions/{self._session_id} "
                    f"runner_id={self._runner_id!r}",
                    file=sys.stderr,
                    flush=True,
                )
            session = await self._client.sessions.bind_runner(
                self._session_id,
                runner_id=self._runner_id,
            )
            self._hydrate_from_session_snapshot(session)
            self._clear_runner_recovery_error()
            if _dbg:
                print(
                    f"[sessions-adapter] runner bound id={self._bound_runner_id!r}",
                    file=sys.stderr,
                    flush=True,
                )

    def _clear_runner_recovery_error(self) -> None:
        """
        Mark the runner recovery path healthy after a successful bind.

        :returns: None.
        """
        self._last_runner_recovery_error_key = None

    def _emit_runner_recovery_error_once(self, exc: Exception) -> None:
        """
        Render a runner recovery failure once per failure transition.

        Background recovery failures cannot bubble to ``send()`` or
        prompt-toolkit will leave the user at an apparently live prompt
        with a dead runner. Emitting the same typed server error event
        shape used by normal streams lets the existing REPL renderer
        show the error panel without adding a second rendering path.

        :param exc: Failure raised while relaunching or rebinding a
            runner, e.g. an SDK ``OmnigentError`` from
            ``PATCH /v1/sessions/{id}``.
        :returns: None.
        """
        if self._on_event is None:
            return

        code = str(getattr(exc, "code", "") or "")
        status_code = str(getattr(exc, "status_code", "") or "")
        key = (type(exc).__name__, code, status_code, str(exc))
        if key == self._last_runner_recovery_error_key:
            return
        self._last_runner_recovery_error_key = key

        from omnigent.server.schemas import ErrorEvent, RetryErrorDetail

        message = self._runner_recovery_error_message(exc)
        self._on_event(
            ErrorEvent(
                type="response.error",
                source="execution",
                error=RetryErrorDetail(
                    code=code or "runner_recovery_failed",
                    message=message,
                ),
            )
        )

    def _runner_recovery_error_message(self, exc: Exception) -> str:
        """
        Build the user-facing message for a recovery failure.

        Server-declared runner state errors are terminal until the
        session is rebound to an online runner. Transport and other
        unexpected failures are treated as transient because the
        watchdog and stream pump will retry with backoff.

        :param exc: Failure raised while relaunching or rebinding a
            runner.
        :returns: Message rendered in the REPL error panel.
        """
        detail = str(exc) or repr(exc)
        if self._is_terminal_runner_recovery_error(exc):
            return f"Runner recovery failed: {detail}"
        return f"Runner recovery hit a transient error and will retry: {detail}"

    def _is_terminal_runner_recovery_error(self, exc: Exception) -> bool:
        """
        Return whether a recovery failure requires user action.

        :param exc: Failure raised while relaunching or rebinding a
            runner.
        :returns: ``True`` for typed server runner-state errors,
            ``False`` for transport-style failures that should keep
            retrying.
        """
        if not isinstance(exc, OmnigentError):
            return False
        code = exc.code or ""
        return code in {"conflict", "invalid_input", "runner_unavailable"} or (
            exc.status_code is not None and 400 <= exc.status_code < 500
        )

    async def switch_to_session(self, new_session_id: str) -> str:
        """
        Re-point the adapter at a different existing session.

        Unbinds the runner from the prior session so the 1:1
        session↔runner invariant holds, cancels the SSE pump bound to
        the old session id, hydrates the new session snapshot, PATCHes
        the runner binding onto the new session, and restarts the
        pump. Called by ``/switch``.

        :param new_session_id: Conversation/session id to attach to,
            e.g. ``"conv_abc123"``.
        :returns: The new session id (echoed back).
        :raises Exception: SDK errors from ``sessions.get`` or the
            bind PATCH propagate; ``/switch`` renders them inline.
            The unbind is soft-failed on old servers (see
            :meth:`_unbind_runner_soft`).
        """
        old_session_id = self._session_id
        if old_session_id is not None and old_session_id != new_session_id:
            await self._unbind_runner_soft(old_session_id)
        if self._stream_task is not None:
            self._stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stream_task
            self._stream_task = None

        self._session_id = new_session_id

        session = await self._client.sessions.get(new_session_id)
        self._hydrate_from_session_snapshot(session)
        await self._bind_runner_if_needed()

        self._stream_task = asyncio.create_task(self._stream_pump())
        return new_session_id

    async def aclose(self) -> None:
        """
        Stop the background stream pump and local tool tasks.

        :returns: None.
        """
        if self._stream_task is not None:
            self._stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stream_task
            self._stream_task = None
        if self._recover_task is not None:
            self._recover_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recover_task
            self._recover_task = None
        for task in list(self._pending_local_tasks.values()):
            task.cancel()
        if self._pending_local_tasks:
            await asyncio.gather(
                *self._pending_local_tasks.values(),
                return_exceptions=True,
            )
            self._pending_local_tasks.clear()

    async def _stream_pump(self) -> None:
        """Subscribe to ``/v1/sessions/{id}/stream`` indefinitely.

        Subscribes to the session's SSE stream and pushes every
        event through ``_on_event``, which renders directly to the
        terminal (text streaming, tool call panels, lifecycle
        headers). Auto-reconnects on disconnect with backoff.

        Turn completion tracking is built into the pump itself:
        when a ``session.status`` event with status ``idle`` or
        ``failed`` arrives, ``_turn_done`` is set regardless of
        whether ``_on_event`` is wired. This ensures ``send()``
        never hangs even when the adapter is used without
        ``run_repl()`` (e.g. integration tests).

        Cancelled on REPL exit via ``_stream_task.cancel()``.
        """
        from omnigent.server.schemas import SessionStatusEvent as _StatusEv

        _dbg = bool(os.environ.get("OMNIGENT_SESSIONS_ADAPTER_DEBUG"))
        backoff = 0.5
        max_backoff = 5.0
        assert self._session_id is not None
        while True:
            try:
                if _dbg:
                    print(
                        f"[sessions-adapter] subscribing /stream {self._session_id}",
                        file=sys.stderr,
                        flush=True,
                    )
                async for event in self._client.sessions.stream(self._session_id):
                    if isinstance(event, _StatusEv) and event.status in ("idle", "failed"):
                        turn_done = getattr(self, "_turn_done", None)
                        if turn_done is not None:
                            turn_done.set()
                    if self._on_event is not None:
                        self._on_event(event)
                # Clean close (server sent [DONE]). Reopen.
                await asyncio.sleep(backoff)
                backoff = 0.5
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect on any error
                # Recoverable transport errors (peer closed mid-chunk,
                # read timeout, transient network error) are normal
                # background noise — the session continues server-side
                # and the next subscription picks up where we left off.
                # Emit a one-line INFO so the TUI doesn't paint a fresh
                # multi-line traceback every reconnect, and keep the
                # full postmortem behind a DEBUG sibling for engineers
                # who flip logging to DEBUG. Genuinely unexpected
                # failures still get a WARNING with the traceback.
                if _is_recoverable_sse_transport_error(exc):
                    _log.info("SSE transport interrupted, reconnecting")
                    _log.debug("recoverable SSE disconnect", exc_info=exc)
                else:
                    _log.warning("SSE stream error, reconnecting", exc_info=exc)
                if self._runner_recover is not None:
                    try:
                        await self._recover_runner_if_needed()
                        await self._bind_runner_if_needed()
                        self._clear_runner_recovery_error()
                    except Exception as recover_exc:  # noqa: BLE001
                        _log.warning(
                            "Runner recover after stream error failed",
                            exc_info=recover_exc,
                        )
                        with contextlib.suppress(Exception):
                            self._emit_runner_recovery_error_once(recover_exc)
                        if _dbg:
                            print(
                                "[sessions-adapter] runner recover after stream "
                                f"error failed: {recover_exc!r}",
                                file=sys.stderr,
                                flush=True,
                            )
                if _dbg:
                    print(
                        f"[sessions-adapter] /stream error: {exc!r}; "
                        f"reconnecting in {backoff:.1f}s",
                        file=sys.stderr,
                        flush=True,
                    )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    async def send(
        self,
        input: str | list[dict[str, object]],
        *,
        files: list[str] | None = None,
    ):
        """
        Post a user message. Rendering is push-based via ``_on_event``.

        The persistent stream pump delivers every event through the
        ``_on_event`` callback — there is no queue or drain loop.
        ``send()`` just POSTs the message and waits for the turn to
        complete (terminal event). All rendering happens in the
        callback, which the pump calls for every event regardless of
        whether a ``send()`` is in flight.

        :param input: User text or content blocks. Strings are
            wrapped in a single ``input_text`` block.
        :param files: Optional file paths to upload and attach.
        :yields: SDK-shape terminal events for callers that still
            iterate the session surface.
        """
        import mimetypes

        _dbg = bool(os.environ.get("OMNIGENT_SESSIONS_ADAPTER_DEBUG"))
        session_id = await self._ensure_session()
        await self._recover_runner_if_needed()
        await self._bind_runner_if_needed()

        # ── C1: File attachments ──────────────────────────────
        if files:
            if isinstance(input, str):
                content_blocks: list[dict[str, object]] = []
                if input:
                    content_blocks.append({"type": "input_text", "text": input})
            else:
                content_blocks = list(input)
            session_files = self._client.files.for_session(session_id)
            for path in files:
                uploaded = await session_files.upload(path)
                ct = mimetypes.guess_type(path)[0]
                if ct and ct.startswith("image/"):
                    content_blocks.append({"type": "input_image", "file_id": uploaded.id})
                else:
                    content_blocks.append(
                        {
                            "type": "input_file",
                            "file_id": uploaded.id,
                            "filename": pathlib.Path(path).name,
                        }
                    )
            input = content_blocks  # type: ignore[assignment]

        if isinstance(input, str):
            content: list[dict[str, object]] = [{"type": "input_text", "text": input}]
        else:
            content = list(input)
        event_payload: dict[str, object] = {
            "type": "message",
            "data": {"role": "user", "content": content},
        }
        if self._model_override is not None:
            event_payload["model_override"] = self._model_override

        # Signal that a turn is active. The _on_event callback
        # uses this to know it should handle streaming text deltas
        # and tool rendering inline rather than as history items.
        self._is_streaming = True
        self._turn_done: asyncio.Event = asyncio.Event()
        self._pending_local_user_sends += 1

        try:
            if _dbg:
                print(
                    f"[sessions-adapter] POST /events session={session_id}",
                    file=sys.stderr,
                    flush=True,
                )
            await self._client.sessions.post_event(session_id, event_payload)
            if _dbg:
                print(
                    "[sessions-adapter] POST returned; waiting for terminal event",
                    file=sys.stderr,
                    flush=True,
                )
            # Wait for the turn to complete. The stream pump sets
            # _turn_done when it sees session.status idle/failed;
            # the _on_event callback (when wired) also sets it.
            #
            # Fallback polling: httpx's ASGI transport does not
            # flush streaming body chunks eagerly, so the pump's
            # SSE subscription may not be active when the workflow
            # publishes its terminal event (no-replay pub-sub).
            # We poll the snapshot every second as a backstop.
            while not self._turn_done.is_set():
                try:
                    # Event.wait() is cancellation-safe (its finally block
                    # removes the waiter from Event._waiters), so no
                    # asyncio.shield() is needed — shield leaks orphaned
                    # Tasks on each timeout iteration.
                    await asyncio.wait_for(
                        self._turn_done.wait(),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    snap = await self._client.sessions.get(session_id)
                    if snap.status in ("idle", "failed"):
                        self._turn_done.set()
            # Yield a terminal event so callers iterating send()
            # observe completion. Rendering already happened via
            # _on_event.
            from omnigent_client._events import ResponseCompleted
            from omnigent_client._types import Response as SDKResponse

            yield ResponseCompleted(
                response=SDKResponse.from_dict(
                    {
                        "id": self._current_response_id or "",
                        "status": "completed",
                        "model": self._agent_name,
                        "output": [],
                    }
                ),
            )
        finally:
            self._is_streaming = False

    async def send_skill_slash_command(
        self,
        skill_name: str,
        arguments: str,
    ) -> AsyncGenerator[object, None]:
        """
        Post a structured skill slash-command event.

        The Omnigent server persists a visible ``slash_command`` item and
        injects the skill body as a hidden ``message`` with
        ``is_meta=True``. This method deliberately does not call
        :meth:`send`, because skill commands are not user-message
        text and must not reintroduce the legacy ``load_skill``
        prompt into the transcript.

        :param skill_name: Skill name without the leading slash,
            e.g. ``"code-review"``.
        :param arguments: Raw text typed after the slash command,
            e.g. ``"review this diff"``. Empty string when none.
        :yields: SDK-shape terminal events for callers that still
            iterate the session surface.
        """
        _dbg = bool(os.environ.get("OMNIGENT_SESSIONS_ADAPTER_DEBUG"))
        session_id = await self._ensure_session()
        await self._recover_runner_if_needed()
        await self._bind_runner_if_needed()

        event_payload: dict[str, object] = {
            "type": "slash_command",
            "data": {
                "kind": "skill",
                "name": skill_name,
                "arguments": arguments,
            },
        }
        if self._model_override is not None:
            event_payload["model_override"] = self._model_override

        self._is_streaming = True
        self._turn_done: asyncio.Event = asyncio.Event()
        command_key = (skill_name, arguments)
        self._pending_local_skill_slash_commands.append(command_key)

        try:
            if _dbg:
                print(
                    f"[sessions-adapter] POST skill slash command session={session_id}",
                    file=sys.stderr,
                    flush=True,
                )
            await self._client.sessions.post_event(session_id, event_payload)
            while not self._turn_done.is_set():
                try:
                    await asyncio.wait_for(
                        self._turn_done.wait(),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    snap = await self._client.sessions.get(session_id)
                    if snap.status in ("idle", "failed"):
                        self._turn_done.set()
            from omnigent_client._events import ResponseCompleted
            from omnigent_client._types import Response as SDKResponse

            yield ResponseCompleted(
                response=SDKResponse.from_dict(
                    {
                        "id": self._current_response_id or "",
                        "status": "completed",
                        "model": self._agent_name,
                        "output": [],
                    }
                ),
            )
        finally:
            with contextlib.suppress(ValueError):
                self._pending_local_skill_slash_commands.remove(command_key)
            self._is_streaming = False

    async def cancel(self):
        """
        Interrupt the running turn (if any).

        Posts an ``{"type": "interrupt"}`` event to the session,
        which bypasses the input queue and cancels the running
        task directly. Returns ``None`` rather than a
        :class:`Response` because the sessions API has no
        per-response object to return. The REPL's ``/cancel``
        command treats ``None`` as "nothing to print".

        :returns: ``None``.
        """
        if self._session_id is None:
            return
        await self._client.sessions.interrupt(self._session_id)
        return

    def _spawn_client_tool(
        self,
        session_id: str,
        call_id: str,
        name: str,
        args_str: str,
    ) -> None:
        """
        Spawn a background task to execute a client-side tool.

        Looks up the tool in ``_tool_callables``, runs it, and
        POSTs the result as a ``function_call_output`` event.

        :param session_id: Session to post the result to.
        :param call_id: The tool call's correlation id.
        :param name: Tool name, e.g. ``"search.web"``.
        :param args_str: JSON-encoded arguments string.
        """
        import inspect
        import json as _json

        callable_fn = self._tool_callables.get(name) if self._tool_callables else None  # type: ignore[union-attr]
        if callable_fn is None:
            return

        async def _run() -> None:
            try:
                args = _json.loads(args_str) if args_str else {}
            except (ValueError, TypeError):
                args = {}
            from omnigent_client._tool_handler import ToolCallInfo

            call_info = ToolCallInfo(
                name=name,
                arguments=args,
                call_id=call_id,
                agent_name="",
                response_id=self._current_response_id or "",
                iteration=0,
            )
            try:
                result = callable_fn(call_info)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:  # noqa: BLE001
                result = f"Error executing tool: {exc}"
            await self._client.sessions.post_event(
                session_id,
                {
                    "type": "function_call_output",
                    "data": {"call_id": call_id, "output": str(result)},
                },
            )

        task = asyncio.create_task(_run(), name=f"client-tool-{call_id}")
        self._pending_local_tasks[call_id] = task
        task.add_done_callback(lambda _t, _k=call_id: self._pending_local_tasks.pop(_k, None))

    async def _handle_elicitation(
        self,
        session_id: str,
        event: object,
    ) -> None:
        """
        Route an elicitation through the hook and POST the verdict.

        :param session_id: Session to post the approval to.
        :param event: The translated :class:`ElicitationRequest`.
        """
        import inspect

        from omnigent_client._tool_handler import ElicitationRequestCtx

        elicitation_id = getattr(event, "elicitation_id", "")
        hook = self._hooks.on_elicitation_request
        if hook is None:
            action = "decline"
        else:
            ctx = ElicitationRequestCtx(
                elicitation_id=elicitation_id,
                message=getattr(event, "message", ""),
                requested_schema=getattr(event, "requested_schema", {}),
                mode=getattr(event, "mode", "form"),
                phase=getattr(event, "phase", ""),
                policy_name=getattr(event, "policy_name", ""),
                content_preview=getattr(event, "content_preview", ""),
                response_id=self._current_response_id or "",
                url=getattr(event, "url", None),
            )
            try:
                result = hook(ctx)
                if inspect.isawaitable(result):
                    result = await result
                action = "accept" if result else "decline"
            except Exception:  # noqa: BLE001
                action = "decline"
        # Build the resolve payload. For accept with a requestedSchema,
        # populate ``content`` from the schema so the MCP server receives
        # the form data it expects. Simple schemas (boolean, enum) are
        # auto-filled; complex schemas that require free-form user input
        # fall back to decline with a message to use the web UI.
        resolve_payload: dict[str, object] = {"action": action}
        if action == "accept":
            schema = getattr(event, "requested_schema", None) or {}
            content = _build_elicitation_content_from_schema(schema)
            if content is not None:
                resolve_payload["content"] = content
            elif schema.get("properties"):
                # Schema has properties we can't auto-fill — the REPL
                # can't render arbitrary forms. Decline and tell the
                # user to use the web UI.
                # TODO: render schema fields as terminal input prompts.
                resolve_payload["action"] = "decline"

        try:
            # URL-based elicitation: deliver the verdict to the
            # elicitation's dedicated resolve URL rather than as an
            # in-band ``approval`` session event. Same server-side
            # effect (both converge on ``_resolve_elicitation``).
            await self._client.sessions.resolve_elicitation(
                session_id,
                elicitation_id,
                resolve_payload,
            )
        except OmnigentError as exc:
            if exc.code == "not_found":
                # Elicitation already resolved by another client (e.g. web
                # UI approved while the terminal prompt was still open).
                # The harness already received the verdict — treat as no-op.
                return
            raise

    async def _unbind_runner_soft(self, session_id: str) -> None:
        """
        Unbind the runner from ``session_id``; soft-fail on old servers.

        Forward-compat shim: servers without the empty-string clear
        sentinel reject ``{"runner_id": ""}`` with
        ``invalid_input: runner_id must not be empty``. We log that
        case at debug and continue, so ``/clear`` and ``/switch`` keep
        working against unpatched deployments — the 1:1 session↔runner
        invariant just isn't enforced until the server is redeployed.
        Other errors propagate.
        """
        _dbg = bool(os.environ.get("OMNIGENT_SESSIONS_ADAPTER_DEBUG"))
        try:
            await self._client.sessions.unbind_runner(session_id)
        except OmnigentError as exc:
            if exc.code == "invalid_input" and "runner_id must not be empty" in str(exc):
                if _dbg:
                    print(
                        f"[sessions-adapter] unbind_runner not supported by server "
                        f"(session {session_id!r} keeps stale runner binding until "
                        f"server is redeployed): {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                return
            raise

    def reset(self) -> None:
        """
        Legacy hook — no-op in sessions mode.

        ``_attach_to_conversation`` calls ``reset()`` *after* the
        runner bind + SSE pump are set up, so doing teardown here
        would silently break ``--resume`` / ``/switch``. ``/clear``
        and ``/new`` use :meth:`start_new_conversation` instead.
        """
        return

    async def start_new_conversation(self) -> None:
        """
        Tear down the current session so the next ``send()`` POSTs a fresh one.

        Used by ``/clear`` and ``/new``. Unbinds the runner from the
        old session (1:1 session↔runner invariant), cancels the SSE
        pump, and clears local state. Session creation stays lazy —
        the next :meth:`send` takes :meth:`_ensure_session`'s create
        branch. Idempotent when no session is established. The unbind
        is soft-failed on old servers (see :meth:`_unbind_runner_soft`).
        """
        old_session_id = self._session_id
        if old_session_id is not None:
            await self._unbind_runner_soft(old_session_id)
        if self._stream_task is not None:
            self._stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stream_task
            self._stream_task = None
        for task in self._pending_local_tasks.values():
            task.cancel()
        self._pending_local_tasks = {}
        self._session_id = None
        self._current_response_id = None
        self._is_streaming = False
        self._pending_local_user_sends = 0
        self._pending_local_skill_slash_commands = []
        self._bound_runner_id = None

    def resume_from_response(self, response_id: str) -> None:  # noqa: ARG002 — legacy hook accepted but ignored in sessions mode
        """
        Legacy hook — no-op in sessions mode.

        Used by the REPL to seed ``previous_response_id`` after
        discovering an external in-flight response. The sessions
        API drives off session_id only, so there is nothing to
        seed.

        :param response_id: Ignored.
        """
        return

    def switch_session(self, new_session_id: str) -> None:
        """
        Switch the adapter to a different session in-place.

        Cancels the existing SSE stream pump (if running) and
        updates ``_session_id`` so the next :meth:`_ensure_session`
        call reconnects to the new session. Used by ``/fork`` to
        continue in the forked conversation without repainting the
        transcript.

        :param new_session_id: The session id to switch to, e.g.
            ``"conv_fork_abc123"``.
        """
        if self._stream_task is not None:
            self._stream_task.cancel()
            self._stream_task = None
        self._session_id = new_session_id
        self._bound_runner_id = None  # Force re-bind on next send


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _approval as _sib_approval
    from . import _commands as _sib_commands
    from . import _context as _sib_context
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
    from . import _model as _sib_model
    from . import _overview as _sib_overview
    from . import _render as _sib_render
    from . import _startup as _sib_startup
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
    for _key, _value in _sib_render.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_startup.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
