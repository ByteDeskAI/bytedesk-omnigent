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

def _load_startup_theme() -> TerminalTheme:
    """Return the persisted startup theme, or run the interactive picker.

    On first launch (no persisted theme in ``~/.omnigent/config.yaml``),
    shows an interactive arrow-key theme picker before the REPL starts.
    The picker uses OSC 11 detection to pre-select dark or light based on
    the terminal's actual background, then persists the user's choice.

    On subsequent launches, returns the persisted theme directly.

    User config is a convenience preference, not required REPL state. If the
    config is corrupt or unreadable, startup should still succeed with the
    default theme; the user can repair or overwrite the file later with
    ``/theme``.
    """

    try:
        persisted_theme = load_user_config().theme
    except (UserConfigError, ValueError):
        return LIGHT_THEME

    if persisted_theme is not None:
        # Theme was previously saved — use it directly.
        try:
            return get_theme(persisted_theme)
        except ValueError:
            return LIGHT_THEME

    # First launch: no theme persisted yet. Show the interactive picker.
    from omnigent.repl._theme_picker import startup_theme_picker

    return startup_theme_picker()

@dataclass(frozen=True)
class _StartupHeader:
    """Resolved data for the Claude-Code-style startup header box.

    Built by :func:`_build_startup_header` and consumed by
    :func:`_render_startup_banner_ansi`.

    :param folder: The working directory in ``~``-relative form, e.g.
        ``"~/omnigent"``.
    :param description: A one-line agent summary (first sentence of the
        spec ``description``, length-capped), e.g. ``"multi-agent coding
        orchestrator"``; ``None`` when the spec declares none.
    :param model_label: The resolved model id for the launch harness,
        e.g. ``"claude-sonnet-4-6"``; ``None`` when no model is pinned
        (a subscription / Databricks profile picks it at run time).
    :param credential: The launch harness's credential as glyph + label,
        e.g. ``"🧱 Databricks (my-ws)"`` — a subscription renders
        glyphless as ``"Subscription"`` (see :func:`_header_glyph`);
        ``None`` when none resolves (e.g. a remote-URL target with no
        local harness).
    :param creds_line: The per-family creds disclosure shown beneath the
        box for multi-vendor agents, e.g. ``"Claude → Subscription
        ·   Codex → Subscription"``; ``None`` for single-family
        agents (the box's credential row already says it).
    """

    folder: str
    description: str | None
    model_label: str | None
    credential: str | None
    creds_line: str | None

def _display_cwd() -> str:
    """Return the current working directory in ``~``-relative form.

    :returns: The cwd with ``$HOME`` collapsed to ``~`` (e.g.
        ``"~/omnigent"``), or the absolute path when it is not
        under the home directory.
    """
    import os

    cwd = os.getcwd()
    home = os.path.expanduser("~")
    if cwd == home:
        return "~"
    if cwd.startswith(home + os.sep):
        return "~" + cwd[len(home) :]
    return cwd

def _summarize_description(description: str | None) -> str | None:
    """Return a compact one-line summary of an agent's description.

    Collapses whitespace (spec descriptions are often YAML folded
    scalars carrying newlines), takes the first sentence, and caps the
    length so the header box stays compact.

    :param description: The raw spec ``description``, e.g. polly's
        ``"Multi-agent coding orchestrator. polly never …"``; ``None``
        when absent.
    :returns: A trimmed one-liner, e.g. ``"Multi-agent coding
        orchestrator"``, or ``None`` when *description* is empty.
    """
    import re

    if not description:
        return None
    text = re.sub(r"\s+", " ", description).strip()
    if not text:
        return None
    first = text.split(". ")[0].rstrip(".")
    max_len = 60
    if len(first) > max_len:
        first = first[: max_len - 1].rstrip() + "…"
    return first

def _header_glyph(kind: str) -> str:
    """Kind glyph for the startup header's credential labels.

    The header drops the subscription ADMISSION TICKETS glyph — its red
    rendering is too loud for the banner box — while every other kind
    keeps its :func:`kind_glyph`. CLI surfaces (``omnigent setup``, the
    ``/model`` readout) keep the ticket.

    :param kind: The provider kind, e.g. ``"subscription"`` or ``"key"``.
    :returns: The kind's glyph (e.g. ``"🔑"``), or ``""`` for the
        subscription kind.
    """
    from omnigent.onboarding.configure_models import kind_glyph
    from omnigent.onboarding.provider_config import SUBSCRIPTION_KIND

    return "" if kind == SUBSCRIPTION_KIND else kind_glyph(kind)

def _build_startup_header(
    harness: str | None,
    agent_description: str | None,
    used_families: list[str] | None,
) -> _StartupHeader:
    """Resolve the data for the startup header box + creds line.

    Reads the merged provider config to name the launch harness's model
    + credential and, for a multi-vendor agent (more than one family
    across its harnesses + sub-agents), each used family's configured
    credential. This function does no exception handling — a failure is
    the caller's cue to fall back to the plain banner.

    :param harness: The launch harness, e.g. ``"claude-sdk"``; ``None``
        for a remote-URL target with no local harness (then only folder
        + description are populated, no credential).
    :param agent_description: The agent spec's ``description`` (raw),
        e.g. polly's multi-line summary; ``None`` when absent.
    :param used_families: Harness surfaces the agent's harnesses (incl.
        sub-agents) consume, e.g. ``["anthropic", "openai", "pi"]`` for
        polly (a pi brain spawning claude/codex sub-agents); a list of
        length > 1 produces the per-surface creds line. ``None`` / a
        single surface omits it.
    :returns: The resolved :class:`_StartupHeader`.
    """
    from omnigent.onboarding.configure_models import (
        credential_label,
        family_label,
    )
    from omnigent.onboarding.detected import effective_config_with_detected
    from omnigent.onboarding.provider_config import (
        describe_active_credential,
        load_config,
        surface_default_provider,
    )

    config = effective_config_with_detected(load_config())

    model_label: str | None = None
    credential: str | None = None
    if harness is not None:
        cred = describe_active_credential(config, harness)
        if cred is not None:
            model_label = cred.model
            cred_name = credential_label(cred.kind, cred.provider_name)
            credential = f"{_header_glyph(cred.kind)} {cred_name}".strip()

    creds_line: str | None = None
    families = used_families or []
    if len(families) > 1:
        parts: list[str] = []
        for fam in families:
            # Effective per-surface default — for the pi surface this is
            # what the pi harness would actually route through (explicit
            # pi scope, else the cross-family fallback).
            entry = surface_default_provider(config, fam)
            if entry is None:
                label = "not configured"
            else:
                cred_text = credential_label(
                    entry.kind,
                    entry.name,
                    profile=entry.profile,
                    display_name=entry.display_name,
                )
                label = f"{_header_glyph(entry.kind)} {cred_text}".strip()
            parts.append(f"{family_label(fam)} → {label}")
        creds_line = "   ·   ".join(parts)

    return _StartupHeader(
        folder=_display_cwd(),
        description=_summarize_description(agent_description),
        model_label=model_label,
        credential=credential,
        creds_line=creds_line,
    )

def _is_remote_server_url(url: str | None) -> bool:
    """True if *url* points at a host other than loopback.

    A local ``omnigent run`` spawns its own Omnigent server on
    ``http://127.0.0.1:<port>``; surfacing that URL in the
    welcome banner adds noise without information. A user
    running with ``--server <url>`` is talking to a different
    process — possibly on another machine — and showing the URL
    is meaningful context.

    :param url: Base URL string, e.g. ``"http://127.0.0.1:6767"``
        or ``"https://example.databricks.com"``. ``None`` returns
        ``False``.
    :returns: ``True`` when *url* parses to a non-loopback host.
    """
    if not url:
        return False
    import ipaddress
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if not host or host == "localhost":
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Hostname (not an IP literal) — treat as remote.
        return True

def _humanize_agent_name(agent_name: str) -> str:
    """
    Convert an agent's wire name (from the YAML's ``name:``
    field) to the spaces-not-separators form shown in the
    welcome banner.

    Centralized so every banner-rendering site agrees — the
    initial ``run_repl`` banner, the ``/new`` reset banner,
    the ``/switch`` redraw banner, and the
    "Resumed conversation …" line all stay consistent.
    Without this, ``resume_test`` (the wire form) would
    render in some banners and ``resume test`` (humanized)
    in others, producing the visible mismatch reported on
    the user-facing welcome panel.

    :param agent_name: Agent's registered name, e.g.
        ``"resume_test"`` or ``"my-agent"``.
    :returns: The display form, e.g. ``"resume test"`` or
        ``"my agent"``.
    """
    return agent_name.replace("-", " ").replace("_", " ")

async def _maybe_write_session_log(
    client: OmnigentClient,
    session: Session,
    agent_name: str,
    log_dir: pathlib.Path,
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """
    Resolve the active conversation id from the session and write
    its JSON dump to *log_dir*.

    Sessions mode uses the session id as the conversation id, so no
    response lookup is needed.

    :param client: Connected OmnigentClient — REUSED, not opened
        here, so we ride inside the caller's ``async with`` scope.
    :param session: The REPL session object whose
        ``current_response_id`` we read.
    :param agent_name: Agent name to embed in the dump.
    :param log_dir: Directory to write under,
        e.g. ``Path("~/.omnigent/logs").expanduser()``.
    :param host: TerminalHost for surfacing the result line.
    :param fmt: TimedFormatter for muted-text styling.
    """
    from omnigent.repl._session_log import write_session_log

    conversation_id = getattr(session, "session_id", None)
    if not conversation_id:
        # User exited without sending a single message. Nothing to
        # log because the SessionsChat is created lazily on first send.
        return
    try:
        path = await write_session_log(
            client,
            conversation_id,
            agent_name=agent_name,
            log_dir=log_dir,
        )
    except Exception as exc:  # noqa: BLE001 — REPL UI boundary: same rationale as above
        _log.exception("Session log write failed")
        host.output(
            Text.from_markup(
                f"  [{fmt.muted}]session log write failed "
                f"({type(exc).__name__}: {exc})[/{fmt.muted}]"
            )
        )
        return
    host.output(Text.from_markup(f"  [{fmt.muted}]wrote session log to {path}[/{fmt.muted}]"))

def _clear_screen() -> None:
    """Clear visible content by scrolling it off screen."""

    try:
        height = os.get_terminal_size().lines
    except (ValueError, OSError):
        height = 24
    print("\n" * height, end="", flush=True)


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
    from . import _render as _sib_render
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
    for _key, _value in _sib_render.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
