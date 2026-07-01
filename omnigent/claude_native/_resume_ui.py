"""Native Claude Code terminal wrapper for the Omnigent CLI.

The wrapper deliberately treats Claude Code as a terminal-first
program. It creates or binds an Omnigent session, launches ``claude``
through the existing runner terminal resource API, then attaches the
local TTY to the existing terminal WebSocket protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import signal
import sys
import termios
import tty
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import IO, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnigent.onboarding.provider_config import ProviderEntry
    from omnigent.spec.types import AgentSpec

import click
import httpx
import yaml
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, WebSocketException
from websockets.frames import Close

from omnigent._native_resume_hint import echo_native_resume_hint
from omnigent._runner_startup import RunnerStartupProgress, runner_startup_progress
from omnigent._startup_profile import StartupProfiler
from omnigent._terminal_picker_theme import (
    PICKER_ACCENT as _PICKER_ACCENT,
)
from omnigent._terminal_picker_theme import (
    PICKER_MUTED as _PICKER_MUTED,
)
from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE as _WRAPPER_LABEL_VALUE,
)
from omnigent._wrapper_labels import (
    WRAPPER_LABEL_KEY as _WRAPPER_LABEL_KEY,
)
from omnigent.claude_native_bridge import (
    BRIDGE_ID_LABEL_KEY,
    augment_claude_args,
    bridge_dir_for_bridge_id,
    prepare_bridge_dir,
    read_active_session_id,
    read_user_effort_level,
    url_component,
)
from omnigent.claude_native_forwarder import (
    reset_transcript_forward_state,
    supervise_forwarder,
)
from omnigent.claude_native_state import (
    read_launch_state,
    redirect_launch_state,
    write_launch_state,
)
from omnigent.conversation_browser import conversation_url, open_conversation_link_if_enabled
from omnigent.entities.session_resources import terminal_resource_id
from omnigent.host.daemon_launch import (
    DAEMON_POLL_INTERVAL_S,
    error_text,
    launch_or_reuse_daemon_runner,
    wait_for_host_online,
    wait_for_runner_online,
)
from omnigent.native_terminal import (
    DAEMON_HOST_ONLINE_TIMEOUT_S as _DAEMON_HOST_ONLINE_TIMEOUT_S,
)
from omnigent.native_terminal import (
    DAEMON_RUNNER_ONLINE_TIMEOUT_S as _DAEMON_RUNNER_ONLINE_TIMEOUT_S,
)
from omnigent.native_terminal import (
    DAEMON_TERMINAL_READY_TIMEOUT_S as _DAEMON_TERMINAL_READY_TIMEOUT_S,
)
from omnigent.native_terminal import (
    bind_session_runner as _bind_session_runner,
)
from omnigent.native_terminal import (
    terminal_attach_url as _attach_url,
)
from omnigent.terminals.ws_bridge import (
    WS_CLOSE_TERMINAL_DETACHED,
    WS_CLOSE_TERMINAL_NOT_FOUND,
)

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

def _prompt_resume_workspace_action(
    *,
    recorded_path: Path,
    current: Path,
    redirect_available: bool,
) -> str:
    """
    Ask how to handle a Claude resume cwd mismatch.

    :param recorded_path: Recorded launch cwd, already resolved.
    :param current: Current cwd, already resolved.
    :param redirect_available: Whether a local Claude transcript for
        the session was found and can be moved into *current*.
    :returns: One of ``"switch"``, ``"move"``, or ``"leave"``.
    """
    options = _resume_workspace_action_options(
        recorded_path=recorded_path,
        current=current,
        redirect_available=redirect_available,
    )
    if _stream_is_tty(sys.stdin):
        return _pick_resume_workspace_action_prompt_toolkit(
            options,
            recorded_path=recorded_path,
            current=current,
            out=sys.stderr,
            in_=sys.stdin,
        )
    return _prompt_resume_workspace_action_text(
        options,
        recorded_path=recorded_path,
        current=current,
    )

def _resume_workspace_action_options(
    *,
    recorded_path: Path,
    current: Path,
    redirect_available: bool,
) -> list[_ResumeWorkspaceActionOption]:
    """
    Build the valid actions for a cwd-mismatched resume.

    :param recorded_path: Recorded launch cwd, already resolved.
    :param current: Current cwd, already resolved.
    :param redirect_available: Whether a local Claude transcript for
        the session was found and can be moved into *current*.
    :returns: Action options in display order.
    """
    recorded_exists = recorded_path.is_dir()
    options: list[_ResumeWorkspaceActionOption] = []
    if recorded_exists:
        options.append(
            _ResumeWorkspaceActionOption(
                action=_RESUME_ACTION_SWITCH,
                label=f"Switch working directory to {recorded_path}",
            )
        )
    if redirect_available:
        options.append(
            _ResumeWorkspaceActionOption(
                action=_RESUME_ACTION_MOVE,
                label=f"Move conversation to {current}",
            )
        )
    options.append(
        _ResumeWorkspaceActionOption(
            action=_RESUME_ACTION_LEAVE,
            label="Leave",
        )
    )
    return options

def _prompt_resume_workspace_action_text(
    options: list[_ResumeWorkspaceActionOption],
    *,
    recorded_path: Path,
    current: Path,
) -> str:
    """
    Ask for a workspace action using Click's text prompt fallback.

    :param options: Selectable workspace actions in display order.
    :param recorded_path: Recorded launch cwd, already resolved.
    :param current: Current cwd, already resolved.
    :returns: Selected action, e.g. ``"switch"``.
    """
    click.echo(f"\nSession was started in: {recorded_path}", err=True)
    click.echo(f"Current working directory: {current}", err=True)
    click.echo(
        "Claude resume is directory-scoped. Choose an action:",
        err=True,
    )
    for option in options:
        click.echo(f"  {option.action:<6} - {option.label}", err=True)
    return click.prompt(
        "Resume action",
        type=click.Choice([option.action for option in options]),
        default=options[0].action,
        show_choices=True,
        err=True,
    )

def _pick_resume_workspace_action_prompt_toolkit(
    options: list[_ResumeWorkspaceActionOption],
    *,
    recorded_path: Path,
    current: Path,
    out: IO[str],
    in_: IO[str],
) -> str:
    """
    Run the interactive workspace action selector.

    :param options: Selectable workspace actions in display order.
    :param recorded_path: Recorded launch cwd, already resolved.
    :param current: Current cwd, already resolved.
    :param out: Output stream for prompt-toolkit rendering.
    :param in_: Input stream for prompt-toolkit keypresses.
    :returns: Selected action, e.g. ``"move"``.
    :raises KeyboardInterrupt: Propagated when the user presses
        Ctrl+C.
    """
    state = _ResumeWorkspaceActionPickerState(options=options)
    app = _resume_workspace_action_application(
        state,
        recorded_path=recorded_path,
        current=current,
        out=out,
        in_=in_,
    )
    return app.run(
        handle_sigint=False,
        set_exception_handler=False,
        in_thread=_has_running_event_loop(),
    )

def _resume_workspace_action_application(
    state: _ResumeWorkspaceActionPickerState,
    *,
    recorded_path: Path,
    current: Path,
    out: IO[str],
    in_: IO[str],
) -> Any:
    """
    Build the prompt-toolkit application for the action selector.

    :param state: Mutable picker state.
    :param recorded_path: Recorded launch cwd, already resolved.
    :param current: Current cwd, already resolved.
    :param out: Output stream for prompt-toolkit rendering.
    :param in_: Input stream for prompt-toolkit keypresses.
    :returns: A :class:`prompt_toolkit.application.Application`.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.input.defaults import create_input
    from prompt_toolkit.layout import Layout, Window
    from prompt_toolkit.output.defaults import create_output

    control = _resume_workspace_action_control(
        state,
        recorded_path=recorded_path,
        current=current,
    )
    return Application(
        layout=Layout(Window(content=control, wrap_lines=True, always_hide_cursor=True)),
        key_bindings=_resume_workspace_action_key_bindings(state),
        style=_resume_workspace_action_style(),
        include_default_pygments_style=False,
        full_screen=False,
        erase_when_done=False,
        input=create_input(stdin=in_),
        output=create_output(stdout=out),
    )

def _resume_workspace_action_control(
    state: _ResumeWorkspaceActionPickerState,
    *,
    recorded_path: Path,
    current: Path,
) -> Any:
    """
    Build the formatted-text control for the action selector.

    :param state: Mutable picker state.
    :param recorded_path: Recorded launch cwd, already resolved.
    :param current: Current cwd, already resolved.
    :returns: A :class:`prompt_toolkit.layout.controls.FormattedTextControl`.
    """
    from prompt_toolkit.layout.controls import FormattedTextControl

    return FormattedTextControl(
        lambda: _resume_workspace_action_fragments(
            state,
            recorded_path=recorded_path,
            current=current,
        ),
        focusable=True,
    )

def _resume_workspace_action_key_bindings(state: _ResumeWorkspaceActionPickerState) -> Any:
    """
    Build keybindings for the workspace action selector.

    :param state: Mutable picker state.
    :returns: A :class:`prompt_toolkit.key_binding.KeyBindings`
        instance.
    """
    from prompt_toolkit.key_binding import KeyBindings

    key_bindings = KeyBindings()
    _bind_resume_workspace_action_navigation(key_bindings, state)
    _bind_resume_workspace_action_completion(key_bindings, state)
    _bind_resume_workspace_action_interrupt(key_bindings)
    return key_bindings

def _bind_resume_workspace_action_navigation(
    key_bindings: Any,
    state: _ResumeWorkspaceActionPickerState,
) -> None:
    """
    Add movement keys to the workspace action selector.

    :param key_bindings: prompt-toolkit keybinding registry.
    :param state: Mutable picker state.
    :returns: None.
    """

    @key_bindings.add("up")
    @key_bindings.add("k")
    def _move_up(event: Any) -> None:
        """
        Move the highlighted action upward.

        :param event: prompt-toolkit key event.
        :returns: None.
        """
        state.move_selection(-1)
        event.app.invalidate()

    @key_bindings.add("down")
    @key_bindings.add("j")
    def _move_down(event: Any) -> None:
        """
        Move the highlighted action downward.

        :param event: prompt-toolkit key event.
        :returns: None.
        """
        state.move_selection(1)
        event.app.invalidate()

def _bind_resume_workspace_action_completion(
    key_bindings: Any,
    state: _ResumeWorkspaceActionPickerState,
) -> None:
    """
    Add selection and cancellation keys to the action selector.

    :param key_bindings: prompt-toolkit keybinding registry.
    :param state: Mutable picker state.
    :returns: None.
    """

    @key_bindings.add("enter")
    def _select(event: Any) -> None:
        """
        Select the highlighted action.

        :param event: prompt-toolkit key event.
        :returns: None.
        """
        event.app.exit(result=state.selected_action())

    @key_bindings.add("q")
    @key_bindings.add("escape")
    @key_bindings.add("c-d")
    def _leave(event: Any) -> None:
        """
        Leave without resuming.

        :param event: prompt-toolkit key event.
        :returns: None.
        """
        event.app.exit(result=_RESUME_ACTION_LEAVE)

def _bind_resume_workspace_action_interrupt(key_bindings: Any) -> None:
    """
    Add Ctrl+C handling to the action selector.

    :param key_bindings: prompt-toolkit keybinding registry.
    :returns: None.
    """

    @key_bindings.add("c-c")
    def _interrupt(event: Any) -> None:
        """
        Propagate Ctrl+C as KeyboardInterrupt.

        :param event: prompt-toolkit key event.
        :returns: None.
        """
        event.app.exit(exception=KeyboardInterrupt)

def _resume_workspace_action_style() -> Any:
    """
    Build prompt-toolkit styles for the workspace action selector.

    :returns: A :class:`prompt_toolkit.styles.Style` instance.
    """
    from prompt_toolkit.styles import Style

    return Style.from_dict(
        {
            "accent": _PICKER_ACCENT,
            "accent-bold": f"{_PICKER_ACCENT} bold",
            "muted": _PICKER_MUTED,
            "selected": f"{_PICKER_ACCENT} bold",
            "title": "bold",
        }
    )

def _resume_workspace_action_fragments(
    state: _ResumeWorkspaceActionPickerState,
    *,
    recorded_path: Path,
    current: Path,
) -> list[tuple[str, str]]:
    """
    Render the workspace action selector as prompt-toolkit fragments.

    :param state: Mutable picker state.
    :param recorded_path: Recorded launch cwd, already resolved.
    :param current: Current cwd, already resolved.
    :returns: ``(style, text)`` fragments for prompt-toolkit.
    """
    fragments: list[tuple[str, str]] = []
    _append_resume_workspace_action_header(
        fragments,
        recorded_path=recorded_path,
        current=current,
    )
    _append_resume_workspace_action_options(fragments, state)
    _append_resume_workspace_action_footer(fragments)
    return fragments

def _append_resume_workspace_action_header(
    fragments: list[tuple[str, str]],
    *,
    recorded_path: Path,
    current: Path,
) -> None:
    """
    Append the action selector header.

    :param fragments: Fragment list being built.
    :param recorded_path: Recorded launch cwd, already resolved.
    :param current: Current cwd, already resolved.
    :returns: None.
    """
    fragments.extend(
        [
            ("class:title", "Resume from another directory\n"),
            ("class:muted", "Started in: "),
            ("", f"{recorded_path}\n"),
            ("class:muted", "Current:    "),
            ("", f"{current}\n\n"),
        ]
    )

def _append_resume_workspace_action_options(
    fragments: list[tuple[str, str]],
    state: _ResumeWorkspaceActionPickerState,
) -> None:
    """
    Append selectable action rows.

    :param fragments: Fragment list being built.
    :param state: Mutable picker state.
    :returns: None.
    """
    for index, option in enumerate(state.options):
        selected = index == state.selected_index
        marker_style = "class:accent-bold" if selected else "class:muted"
        text_style = "class:selected" if selected else ""
        fragments.extend(
            [
                (marker_style, "> " if selected else "  "),
                (text_style, option.label),
                ("", "\n"),
            ]
        )

def _append_resume_workspace_action_footer(fragments: list[tuple[str, str]]) -> None:
    """
    Append the action selector keybinding footer.

    :param fragments: Fragment list being built.
    :returns: None.
    """
    fragments.extend(
        [
            ("", "\n"),
            ("class:muted", "Keys: "),
            ("class:accent-bold", "↑"),
            ("class:muted", "/"),
            ("class:accent-bold", "↓"),
            ("class:muted", " move  ·  "),
            ("class:accent-bold", "Enter"),
            ("class:muted", " select  ·  "),
            ("class:accent-bold", "Esc"),
            ("class:muted", "/"),
            ("class:accent-bold", "q"),
            ("class:muted", " leave\n"),
        ]
    )

def _has_running_event_loop() -> bool:
    """
    Return whether the current thread is already running asyncio.

    prompt-toolkit's synchronous runner calls :func:`asyncio.run` by
    default, so nested use from an active async caller has to run the
    prompt-toolkit application in a worker thread.

    :returns: ``True`` when an asyncio loop is active in this thread.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True

def _stream_is_tty(stream: IO[str]) -> bool:
    """
    Return whether *stream* is attached to a terminal.

    :param stream: Text stream to inspect, e.g. ``sys.stdin``.
    :returns: ``True`` when ``stream.isatty()`` reports a TTY.
    """
    return bool(stream.isatty())


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _cold_resume as _sib_cold_resume
    from . import _config as _sib_config
    from . import _cwd as _sib_cwd
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
    from . import _local_server as _sib_local_server
    from . import _remote_server as _sib_remote_server
    from . import _terminal as _sib_terminal
    from . import _transcript as _sib_transcript
    from . import _types as _sib_types
    for _key, _value in _sib_cold_resume.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_config.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_cwd.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_entry.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_helpers.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_local_server.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_remote_server.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_terminal.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_transcript.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_types.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
