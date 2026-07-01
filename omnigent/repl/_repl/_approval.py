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

class _ApprovalVerdict(enum.Enum):
    """
    How the user answered a policy approval prompt.

    Three-way rather than boolean so the REPL can distinguish
    "approve just this one" from "approve and stop asking for
    the rest of this session". Mirrors the Claude Code model
    (y / A / n); same muscle memory transfers.

    - ``APPROVE_ONCE`` — allow this one request only.
    - ``APPROVE_ALWAYS`` — allow this request AND remember the
      decision for the rest of the REPL session. Future asks
      from the same policy at the same phase auto-approve
      without prompting.
    - ``REFUSE`` — refuse this request. Fail-closed default
      per POLICIES.md §13; anything not explicitly approve is
      a refusal.
    """

    APPROVE_ONCE = "approve_once"
    APPROVE_ALWAYS = "approve_always"
    REFUSE = "refuse"

def _parse_approval_input(text: str) -> _ApprovalVerdict:
    """
    Classify a line of user input as one of the three verdicts.

    Case-insensitive, whitespace-stripped. ALWAYS tokens are
    checked before ONCE tokens so the lone letter ``a`` is
    treated as "always" rather than ambiguously falling
    through to the refuse default.

    :param text: Raw user input from the main REPL prompt.
    :returns: The parsed verdict.
    """
    normalized = text.strip().lower()
    if normalized in _APPROVE_ALWAYS_TOKENS:
        return _ApprovalVerdict.APPROVE_ALWAYS
    if normalized in _APPROVE_ONCE_TOKENS:
        return _ApprovalVerdict.APPROVE_ONCE
    return _ApprovalVerdict.REFUSE

class _ApprovalState:
    """
    Per-REPL holder for pending approvals and the session
    auto-approve cache.

    Owning an object (rather than module globals) keeps
    multiple REPL sessions in the same process isolated —
    tests can spin up two :func:`run_repl` invocations and
    their state doesn't collide.

    Two pieces of state:

    1. The currently-pending approval :class:`asyncio.Future`
       (``None`` when no ASK is in flight). The hook creates
       it via :meth:`begin`; the main input loop resolves it
       via :meth:`resolve`. Using a future avoids the stdin /
       ``patch_stdout`` fight that a direct ``input()`` call
       produced.
    2. The session auto-approve cache: a set of
       ``(policy_name, phase)`` pairs the user said "always"
       to. Future ASKs matching one of these entries skip the
       prompt and auto-approve. Scoped to this REPL run —
       restart wipes the cache.
    """

    def __init__(self) -> None:
        """Start with no pending approval and an empty cache."""
        self._future: asyncio.Future[bool] | None = None
        # Current ASK's identity — captured on ``begin`` so
        # ``resolve_verdict`` can stash the pair on an
        # APPROVE_ALWAYS without the caller having to re-pass
        # ctx fields.
        self._current_policy: str | None = None
        self._current_phase: str | None = None
        # When True, the current approval is URL-mode-only and
        # keyboard input (y/a/n) should be rejected.
        self._url_mode: bool = False
        # (policy_name, phase) → "approve always" cache.
        # ``phase`` comes from the server as a string
        # (``"request"``, ``"tool_call"``, ...) so storing the
        # pair as-is avoids any re-parsing overhead.
        self._always: set[tuple[str, str]] = set()

    @property
    def pending(self) -> bool:
        """:returns: ``True`` iff an approval is awaiting a verdict."""
        return self._future is not None and not self._future.done()

    def is_pre_approved(self, policy_name: str, phase: str) -> bool:
        """
        Look up an earlier "always" decision.

        Called by the approval hook BEFORE rendering anything —
        a pre-approved ASK must produce no UI noise. The cache
        key is specifically ``(policy_name, phase)``; different
        policies or different phases still prompt even if the
        user approved a related one.

        :param policy_name: Deciding policy's name from the
            :class:`ElicitationRequestCtx`.
        :param phase: Phase string from the ctx (``"request"`` /
            ``"tool_call"`` / etc.).
        :returns: ``True`` iff the user previously answered
            "always" for this policy+phase pair.
        """
        return (policy_name, phase) in self._always

    def remember_always(self, policy_name: str, phase: str) -> None:
        """
        Cache an "approve always" decision for the rest of the
        session.

        Idempotent — adding a duplicate entry is a no-op. The
        cache is NEVER persisted to disk; closing ``omnigent chat``
        clears it, so the next session starts from a clean
        slate. That matches what users expect from
        session-scoped approvals in other tools.

        :param policy_name: Deciding policy's name.
        :param phase: Phase string.
        """
        self._always.add((policy_name, phase))

    def begin(
        self, policy_name: str, phase: str, *, url_mode: bool = False
    ) -> asyncio.Future[bool]:
        """
        Start a new approval — create the future the hook awaits.

        Records the identity of the ASK so
        :meth:`resolve_verdict` can cache an "always" decision
        against the right ``(policy_name, phase)`` pair
        without the caller having to re-pass them.

        If a previous approval's future is still open (the user
        never answered before a new ASK arrived), refuse the
        old one fail-closed and replace it. In practice the
        server only has one parked workflow per REPL at a
        time, so this is defense-in-depth.

        :param policy_name: Deciding policy's name from the
            :class:`ElicitationRequestCtx`.
        :param phase: Phase string from the ctx.
        :returns: The future to await. Resolves to ``True`` on
            approve (one or always) and ``False`` on refuse.
        """
        if self._future is not None and not self._future.done():
            self._future.set_result(False)
        self._current_policy = policy_name
        self._current_phase = phase
        self._url_mode = url_mode
        self._future = asyncio.get_running_loop().create_future()
        return self._future

    def resolve_verdict(self, verdict: _ApprovalVerdict) -> bool:
        """
        Resolve a pending approval with a three-way verdict.

        On :attr:`_ApprovalVerdict.APPROVE_ALWAYS`, caches
        ``(current_policy, current_phase)`` so subsequent
        ASKs for that pair auto-approve without prompting.
        On any other verdict, the cache is untouched.

        :param verdict: The user's answer.
        :returns: ``True`` iff a pending approval existed and
            was resolved. ``False`` when there was nothing to
            resolve (the caller should route input normally).
        """
        if self._future is None or self._future.done():
            return False
        approved = verdict != _ApprovalVerdict.REFUSE
        if (
            verdict == _ApprovalVerdict.APPROVE_ALWAYS
            and self._current_policy is not None
            and self._current_phase is not None
        ):
            self.remember_always(self._current_policy, self._current_phase)
        self._future.set_result(approved)
        self._future = None
        self._current_policy = None
        self._current_phase = None
        return True

    def cancel(self) -> None:
        """
        Cancel any pending approval — refuse fail-closed.

        Called on REPL teardown or when the user ``/cancel``s
        an in-progress response to avoid leaking an unresolved
        future. Does NOT clear the "always" cache — that
        persists for the REPL session.
        """
        if self._future is not None and not self._future.done():
            self._future.set_result(False)
        self._future = None
        self._current_policy = None
        self._current_phase = None

def _build_elicitation_content_from_schema(
    schema: dict[str, object],
) -> dict[str, object] | None:
    """
    Delegate to the shared schema auto-fill utility.

    See :func:`omnigent.tools._elicitation_schema.build_accept_content_from_schema`
    for the full algorithm and docstring.

    :param schema: The ``requestedSchema`` dict from the
        elicitation event. May be empty ``{}``.
    :returns: A flat ``{field: value}`` dict, or ``None``.
    """
    from omnigent.tools._elicitation_schema import build_accept_content_from_schema

    return build_accept_content_from_schema(schema)  # type: ignore[arg-type]

def _make_elicitation_prompt(
    host: TerminalHost,
    fmt: RichBlockFormatter,
    state: _ApprovalState,
    server_url: str | None = None,
) -> Callable[[ElicitationRequestCtx], Awaitable[bool]]:
    """
    Build the ``on_elicitation_request`` hook for the REPL.

    When the server emits an MCP-shape elicitation
    (``response.elicitation_request`` SSE event — today the
    primary producer is the policy ASK flow), the SDK routes
    it to this hook. Two paths:

    - Pre-approved: the user previously said "always" for this
      ``(policy_name, phase)`` pair. Skip all UI, auto-accept.
      Print a short muted line so the transcript records that
      an auto-accept fired — silent auto-acceptance would be
      security-hostile (user forgets they once said "always").
    - Fresh elicitation: render the preview, offer three
      options (``y`` / ``a`` / ``n``), await a future resolved
      by the main input loop.

    This hook does NOT touch stdin or call :func:`input` —
    under the REPL's active ``prompt_toolkit`` session, any
    direct stdin read fights ``patch_stdout`` and produces
    the "characters disappear / auto-delete" jank. Reusing
    the main input loop means typing the verdict works
    exactly like typing any other message. See POLICIES.md
    §7 + ``designs/SERVER_HARNESS_CONTRACT.md`` §"Universal
    API additions".

    The bool return is collapsed to MCP ``action`` by the SDK:
    ``True`` → ``"accept"``, ``False`` → ``"decline"``. The
    REPL's three-way verdict (once / always / refuse) maps to
    bool the same way — "always" still accepts the current
    elicitation; the difference is purely the session-cache
    write.

    :param host: The active :class:`TerminalHost` whose
        output channel we render the request on.
    :param fmt: Formatter whose accent / muted styles we
        reuse for visual consistency with the rest of the
        REPL.
    :param state: Shared :class:`_ApprovalState` that couples
        this hook to the main input loop and holds the
        session auto-approve cache.
    :returns: Async callable suitable for
        :attr:`StreamHooks.on_elicitation_request`.
    """

    async def _on_elicitation_request(ctx: ElicitationRequestCtx) -> bool:
        """
        Render the elicitation and await the main loop's verdict.

        :param ctx: Parsed elicitation carrying the message
            (combined reason from deciding policies), deciding
            policy name, phase, and a truncated preview of the
            gated content.
        :returns: ``True`` on user accept (one or always);
            ``False`` otherwise.
        """
        if state.is_pre_approved(ctx.policy_name, ctx.phase):
            # Audit line — don't be silent when auto-approving,
            # the user might have forgotten they flipped it on.
            host.output(
                Text.from_markup(
                    f"   [{fmt.muted}]auto-approved · "
                    f"{ctx.policy_name} · {ctx.phase}[/{fmt.muted}]",
                ),
            )
            return True

        host.output(
            Text.from_markup(
                f"\n [{fmt.warning}]⚠ approval required · {ctx.phase}[/{fmt.warning}]",
            ),
        )
        host.output(
            Text.from_markup(
                f"   [{fmt.muted}]policy: {ctx.policy_name}[/{fmt.muted}]",
            ),
        )
        if ctx.message:
            host.output(
                Text.from_markup(
                    f"   [{fmt.muted}]reason: {ctx.message}[/{fmt.muted}]",
                ),
            )
        if ctx.content_preview:
            preview = ctx.content_preview
            if len(preview) > 200:
                preview = preview[:200] + "…"
            host.output(
                Text.from_markup(
                    f"   [{fmt.muted}]preview:[/{fmt.muted}] {preview}",
                ),
            )
        _is_external_url = (
            ctx.mode == "url"
            and isinstance(ctx.url, str)
            and not ctx.url.startswith("/approve/")
            and server_url
        )
        if _is_external_url:
            # External URL (OAuth, MCP server, etc.) — show the link,
            # block keyboard approval.
            full_url = f"{server_url.rstrip('/')}{ctx.url}"
            host.output(
                Text.from_markup(f"   [{fmt.accent}]approve:[/{fmt.accent}]"),
            )
            host.output(Text(full_url))
        else:
            # Our own URL or form mode — use keyboard y/a/n.
            host.output(
                Text.from_markup(
                    f"   [{fmt.accent}]y = approve once, "
                    f"a = approve always (this session), "
                    f"n = refuse[/{fmt.accent}]",
                ),
            )
        future = state.begin(ctx.policy_name, ctx.phase, url_mode=bool(_is_external_url))
        return await future

    return _on_elicitation_request


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _adapter as _sib_adapter
    from . import _commands as _sib_commands
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
