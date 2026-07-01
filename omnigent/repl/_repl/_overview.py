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

async def _list_all_conversation_items(
    client: OmnigentClient,
    conv_id: str,
) -> list[dict[str, object]]:
    """
    Fetch every item in *conv_id*, paginating past the
    server's per-request 100-item cap via
    ``GET /v1/sessions/{id}/items``.

    :param client: Agent-plane HTTP client.
    :param conv_id: Session to enumerate, e.g.
        ``"conv_abc123"``.
    :returns: All items in chronological order. Empty when the
        session has no items or every page errored.
    """
    all_items: list[dict[str, object]] = []
    page_size = _LIST_ITEMS_PAGE_SIZE
    after: str | None = None
    while True:
        try:
            raw_page = await client.sessions.list_items(
                conv_id,
                limit=page_size,
                after=after,
                order="asc",
            )
        except Exception:  # noqa: BLE001 — overlay builder: any per-page error falls back to whatever was already fetched; partial sidebar beats no sidebar
            break
        page: list[dict[str, object]] = list(raw_page) if raw_page else []
        if not page:
            break
        all_items.extend(page)
        if len(page) < page_size:
            break
        last_item = page[-1]
        last_id = last_item.get("id") if isinstance(last_item, dict) else None
        if not isinstance(last_id, str):
            break
        after = last_id
    return all_items

async def _collect_overview_targets(
    client: OmnigentClient,
    session: Session,
) -> list[OverlayTarget]:
    """
    Enumerate the debug overview's sidebar targets.

    Always yields a ``main`` entry bound to the current chat's
    conversation. Additionally walks that conversation's items for
    ``function_call_output`` results from ``sys_session_send`` /
    ``sys_session_send`` (continuation path) — the tool outputs include a persistent
    ``conversation_id`` plus ``type`` + ``name`` for every
    sub-agent handle, so we can assemble a sidebar row per
    sub-agent without needing a separate server endpoint. The
    parent-conversation walk tolerates malformed outputs (missing
    fields, non-JSON strings, non-sub-agent tool outputs) by
    skipping them, so unrelated tool calls don't leak into the
    sidebar.

    :param client: Agent-plane HTTP client used for the items
        fetch.
    :param session: REPL session — only
        ``current_response_id`` is consulted; everything else is
        derived from the server response.
    :returns: A list of :class:`OverlayTarget` entries, always
        starting with ``main``. Empty list is never returned;
        when no conversation exists yet, the main target is
        still present with a synthetic placeholder key so the
        sidebar renders correctly from first paint.
    """
    targets: list[OverlayTarget] = [OverlayTarget(key="main", label="main", icon="🤖")]

    # Sessions-API path: read session_id off the adapter rather
    # than round-tripping through responses.get.
    sessions_api_conv_id: str | None = getattr(session, "session_id", None)
    if sessions_api_conv_id is not None:
        conv_id: str | None = sessions_api_conv_id
    else:
        if not session.current_response_id:
            return targets
        try:
            resp = await client.responses.get(session.current_response_id)
            conv_id = resp.conversation.id if resp.conversation else None
        except Exception:  # noqa: BLE001 — overlay builder: any network/server error falls back to the base targets list; the overlay must open even under partial failure
            return targets

    if conv_id is None:
        return targets

    # Store the conversation id on the main target's key so the
    # content builder can fetch the right conversation's items.
    # Recreate rather than mutate — dataclasses are frozen-ish in
    # intent even when the runtime allows assignment.
    targets[0] = OverlayTarget(key=conv_id, label="main", icon="🤖")

    # Paginate past the server's per-request 100-item cap so
    # long sessions (>100 items) still surface every terminal +
    # sub-agent. Without pagination, the user-reported 2026-04-30
    # symptom returns: 17 of 20 terminals visible because the
    # 18th-20th launch outputs landed past position 99.
    try:
        items: list[dict[str, object]] = await _list_all_conversation_items(
            client,
            conv_id,
        )
    except Exception:  # noqa: BLE001 — overlay builder: any network/server error falls back to the base targets list; the overlay must open even under partial failure
        return targets

    # Dedupe by conversation_id — repeated sys_session_send calls (continuation path)
    # to the same handle would otherwise emit duplicate rows. Walk
    # in chronological order (list_items returns oldest-first) so
    # the sidebar order reflects spawn order.
    sub_agent_conv_ids: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item.get("type") != "function_call_output":
            continue
        raw = item.get("output")
        if not isinstance(raw, str):
            continue
        payload = _parse_sub_agent_handle(raw)
        if payload is None:
            continue
        sub_conv = payload.get("conversation_id")
        if not isinstance(sub_conv, str) or sub_conv in seen:
            continue
        sa_agent = payload.get("agent")
        sa_title = payload.get("title")
        if not isinstance(sa_agent, str) or not isinstance(sa_title, str):
            # Malformed payload — skip rather than emit a "?:?"
            # sidebar row that would claim a sub-agent exists
            # but render with no useful identity. If the spawn
            # tool ever ships a ``kind: sub_agent`` output
            # without ``agent`` + ``title``, the sidebar going
            # silent is a clearer signal than a row full of
            # question marks.
            continue
        seen.add(sub_conv)
        sub_agent_conv_ids.append(sub_conv)
        targets.append(
            OverlayTarget(
                key=sub_conv,
                label=f"{sa_agent}:{sa_title}",
                icon="👾",
            ),
        )

    # Terminals — inferred from the agent's tool history rather
    # than fetched from a server-side registry. The legacy
    # non-AP path read ``Session._terminal_instances``
    # directly; without an HTTP endpoint mirroring that, we
    # reconstruct the live set from persisted
    # ``sys_terminal_launch`` / ``sys_terminal_close`` outputs
    # in each conversation's items. Trade-off: a terminal whose
    # process crashed outside the agent's tool surface still
    # appears here. Acceptable for an MVP — see the design
    # discussion in the Layer-1 plan for the supervision gap.
    terminals = await _collect_terminals_for_conversations(
        client,
        [conv_id, *sub_agent_conv_ids],
        seed_items={conv_id: items},
    )
    for info in terminals:
        # ``💻`` (U+1F4BB PERSONAL COMPUTER, "laptop") is
        # wcswidth-wide (2 cells) AND reads as a computer
        # — keeps the visual category consistent with the
        # F20 overview pane the legacy CLI had. Avoid the
        # otherwise-tempting ``🖥`` (U+1F5A5 DESKTOP COMPUTER):
        # Unicode classifies it East-Asian-Width Neutral
        # (wcswidth=1) but every terminal we ship to renders
        # it as 2 cells. wcswidth is the source of truth for
        # both the sidebar's padding AND prompt-toolkit's
        # :class:`Window` containing us, so a wcswidth ↔ render
        # mismatch can't be compensated for from inside the
        # host: it has to be avoided at the icon-pick step.
        # ``💻`` / ``🤖`` / ``👾`` all read 2 cells in both
        # wcswidth and the terminal, so rows stay aligned.
        targets.append(
            OverlayTarget(
                key=_terminal_target_key(info),
                label=f"{info.name}:{info.session}",
                icon="💻",
            ),
        )

    return targets

@dataclass(frozen=True)
class _TerminalInfo:
    """
    Inferred-live terminal reconstructed from conversation items.

    Walking ``sys_terminal_launch`` / ``sys_terminal_close``
    function-call-output pairs gives us the terminal's identity
    plus the tmux coordinates needed to construct an attach
    command. State here is "last-known per the agent's tool
    calls" — a process that crashed outside the agent's tool
    surface still appears here, the same way it would on the
    legacy in-memory ``Session._terminal_instances`` dict
    until the next ``capture-pane`` failure cleared it.

    :param name: Terminal name from the spec /
        ``sys_terminal_launch`` arg, e.g. ``"bash"``.
    :param session: Session key the launch call passed,
        e.g. ``"s1"``. Multiple sessions per terminal name are
        independent tmux sessions of the same configured
        terminal.
    :param socket: Tmux socket path from the launch output —
        the ``-S`` arg an attach command needs.
    :param target: Tmux target. Always ``"main"`` per the
        :class:`TerminalInstance.tmux_target` constant
        (``omnigent/inner/terminal.py:167``); kept on the
        struct for forward compatibility if that constant ever
        becomes per-terminal.
    :param conv_id: The conversation that owns this terminal
        — main agent's conversation for parent-spawned
        terminals, sub-agent's conversation for sub-agent-
        spawned ones. The overlay uses it to label which
        agent the terminal belongs to.
    """

    name: str
    session: str
    socket: str
    target: str
    conv_id: str

def _terminal_target_key(info: _TerminalInfo) -> str:
    """
    Encode a :class:`_TerminalInfo` as an :class:`OverlayTarget`
    key the content builder can decode.

    Format: ``"terminal::<conv_id>::<name>::<session>"``. Socket
    + target are NOT encoded — the builder re-walks the owning
    conversation's items on selection to recover them, keeping
    the key short and the source of truth (the persisted
    function_call_output) authoritative.

    :param info: The terminal info to encode.
    :returns: An opaque key string.
    """
    return f"{_TERMINAL_KEY_PREFIX}{info.conv_id}::{info.name}::{info.session}"

def _decode_terminal_target_key(key: str) -> tuple[str, str, str] | None:
    """
    Reverse :func:`_terminal_target_key`.

    :param key: A target key, possibly a terminal key.
    :returns: ``(conv_id, name, session)`` if *key* is a
        terminal key, ``None`` otherwise — non-terminal keys
        let the caller fall through to the main / sub-agent
        rendering paths.
    """
    if not key.startswith(_TERMINAL_KEY_PREFIX):
        return None
    rest = key[len(_TERMINAL_KEY_PREFIX) :]
    parts = rest.split("::", 2)
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]

def _parse_terminal_tool_output(raw: object) -> dict[str, object] | None:
    """
    Decode a ``sys_terminal_launch`` / ``sys_terminal_close``
    function-call-output payload.

    Mirrors :func:`_parse_sub_agent_handle`'s tolerance for the
    two on-the-wire shapes the workflow persists: raw JSON
    string (default executor, omnigent builtins) and
    MCP-content-parts wrapper (claude-sdk harness). Returning
    ``None`` for anything else lets the reconstructor's loop
    skip cleanly.

    :param raw: The ``function_call_output.output`` value as
        persisted — a string in both cases.
    :returns: The decoded payload dict, or ``None`` when *raw*
        doesn't look like a terminal-tool output.
    """
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        # MCP content-parts wrapper. ``sys_terminal_*`` tools
        # emit one ``text`` part with the JSON envelope inside;
        # walk parts and decode the first match. Same pattern
        # as :func:`_parse_sub_agent_handle`.
        for part in payload:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            try:
                inner = json.loads(text)
            except (ValueError, TypeError):
                continue
            if isinstance(inner, dict):
                return inner
    return None

def _reconstruct_terminals_from_items(
    items: list[dict[str, object]],
    *,
    conv_id: str,
) -> list[_TerminalInfo]:
    """
    Walk function-call/output pairs to infer the live terminal set.

    Replays the conversation's tool history in chronological
    order: each ``sys_terminal_launch`` whose paired output
    includes a ``tmux_socket`` adds an entry; each
    ``sys_terminal_close`` whose paired output reports
    ``status: "closed"`` removes it. The remaining map is the
    set of terminals the agent currently believes are live.

    Failed launches (output has ``error`` field) and
    ``not_found`` closes are ignored. Errors during JSON
    decode skip the item rather than crash — the inferred view
    is best-effort, and one malformed output mustn't kill the
    sidebar.

    :param items: Conversation items in chronological order
        (caller fetched with default ``order="asc"``). Each
        item is the API-shape dict returned by
        ``client.sessions.list_items``.
    :param conv_id: The conversation these items belong to;
        recorded on every emitted :class:`_TerminalInfo` so
        the overlay can label terminals by their owning
        conversation (parent vs. sub-agent).
    :returns: A list of currently-live terminals in launch
        order. Terminals that were launched then closed
        within *items* don't appear.
    """
    live: dict[tuple[str, str], _TerminalInfo] = {}
    pending_calls: dict[str, str] = {}
    for item in items:
        item_type = item.get("type")
        if item_type == "function_call":
            call_id = item.get("call_id")
            tool_name = item.get("name")
            if isinstance(call_id, str) and isinstance(tool_name, str):
                pending_calls[call_id] = tool_name
            continue
        if item_type != "function_call_output":
            continue
        call_id = item.get("call_id")
        if not isinstance(call_id, str):
            continue
        tool_name = pending_calls.pop(call_id, None)
        if tool_name not in {"sys_terminal_launch", "sys_terminal_close"}:
            continue
        payload = _parse_terminal_tool_output(item.get("output"))
        if payload is None or "error" in payload:
            continue
        terminal_name = payload.get("terminal")
        session_key = payload.get("session")
        if not isinstance(terminal_name, str) or not isinstance(session_key, str):
            continue
        key = (terminal_name, session_key)
        if tool_name == "sys_terminal_launch":
            socket = payload.get("tmux_socket")
            if not isinstance(socket, str):
                continue
            # ``status`` may be "launched" (fresh) or
            # "already_running" (idempotent re-launch — same
            # tmux socket); both mean live, both produce one
            # entry under ``key``. Re-launches overwrite which
            # is fine since the key is identical.
            live[key] = _TerminalInfo(
                name=terminal_name,
                session=session_key,
                socket=socket,
                target="main",
                conv_id=conv_id,
            )
        elif tool_name == "sys_terminal_close":
            if payload.get("status") == "closed":
                live.pop(key, None)
            # ``not_found`` closes are no-ops — the LLM tried
            # to close something that wasn't actually live, no
            # state change in our reconstructed map either.
    return list(live.values())

async def _open_terminal_in_tmux(
    target: OverlayTarget,
    *,
    client: OmnigentClient,
    read_only: bool,
) -> None:
    """
    Spawn a fresh tmux window that attaches to *target*'s tmux session.

    Bound on the Ctrl+O overlay's ``O`` (attach) and ``R``
    (attach read-only) keybindings. Mirrors the legacy
    non-AP mode F20-overlay shortcuts at
    ``omnigent/inner/cli.py::_open_current_terminal_window``
    so users with muscle memory from the legacy CLI see the
    same behavior under Omnigent mode.

    Four guards short-circuit cleanly without raising — the
    overlay's exception swallow at the host catches anything
    else, but these four are the common failure modes worth
    surfacing as a clear stderr message:

    1. The selected target isn't a terminal (e.g. user pressed
       ``O`` on the ``main`` row). No-op.
    2. Not running inside tmux (``$TMUX`` unset). The
       ``tmux new-window`` command needs a current session to
       attach the new window to, so this would fail. Print a
       hint instead.
    3. The terminal vanished from the conversation between
       sidebar build and key press. Print a "no longer live"
       message.
    4. The agent's tmux session is dead at runtime (user
       previously attached and exited the bash shell, killing
       the pane → window → session → tmux server). The walker
       still reports the terminal as live because no
       ``sys_terminal_close`` was recorded — the agent didn't
       initiate the teardown. Without this guard, the second
       ``O`` press silently fails: ``tmux new-window`` opens a
       window whose ``tmux attach`` immediately errors and
       closes, looking like a dead key. ``tmux has-session``
       against the recovered socket catches it and prints a
       hint that the agent needs to relaunch.

    On success, the user's tmux client gets a new window
    showing the agent's tmux session. ``-r`` flag adds
    read-only mode; without it both the user and the agent
    can type into the same pane.

    :param target: The selected :class:`OverlayTarget`.
    :param client: Omnigent HTTP client — used to re-walk the
        owning conversation's items so we recover the latest
        socket path (the sidebar's encoded key intentionally
        omits it; see ``_terminal_target_key``).
    :param read_only: When ``True``, pass ``-r`` to ``tmux
        attach`` so the spawned window can't send keys to the
        underlying session.
    """
    decoded = _decode_terminal_target_key(target.key)
    if decoded is None:
        # Not a terminal target — silently ignore. Pressing
        # ``O`` / ``R`` on the main / sub-agent rows is harmless.
        return
    conv_id, name, session = decoded

    if not os.environ.get("TMUX"):
        # Outside tmux there's no host session for the new
        # window to attach to. The user can copy the Attach
        # command from the panel and paste it themselves —
        # surface a hint instead of failing silently.
        import sys as _sys

        print(
            "\nCan't open attach: not running inside tmux. "
            "Copy the Attach command from the panel and run it "
            "in a separate terminal, or start your REPL inside "
            "tmux to enable the O / R hotkeys.\n",
            file=_sys.stderr,
        )
        return

    # Paginate so the attach action finds terminals whose
    # launch outputs land past position 99 — same bug shape as
    # the sidebar enumeration in ``_collect_overview_targets``
    # (the user-reported 2026-04-30 "17 of 20 terminals" case).
    try:
        items = await _list_all_conversation_items(
            client,
            conv_id,
        )
    except Exception:  # noqa: BLE001 — overlay action: a per-conversation fetch error becomes a stderr hint instead of crashing the overlay
        import sys as _sys

        print(
            f"\nCan't open attach for {target.label}: items fetch failed.\n",
            file=_sys.stderr,
        )
        return

    matches = [
        info
        for info in _reconstruct_terminals_from_items(items, conv_id=conv_id)
        if info.name == name and info.session == session
    ]
    if not matches:
        import sys as _sys

        print(
            f"\nCan't open attach for {target.label}: terminal is no longer "
            f"live (closed since the sidebar was built).\n",
            file=_sys.stderr,
        )
        return

    info = matches[0]
    import shlex
    import subprocess as _subprocess
    import sys as _sys

    # Runtime liveness check shared with the Status field
    # rendering in :func:`_build_terminal_overview`. Skipping
    # the new-window spawn when the session is gone means the
    # second ``O`` press doesn't silently fail (a window that
    # opens just to error and close).
    if not _tmux_session_alive(info.socket, info.target):
        print(
            f"\nCan't open attach for {target.label}: tmux session is gone "
            f"(user likely exited the shell on a previous attach, killing "
            f"the agent's pane → window → session → tmux server). The "
            f"sidebar still shows it as live because no sys_terminal_close "
            f"tool call was recorded. Ask the agent to launch a new "
            f"terminal, or close + relaunch the conversation.\n",
            file=_sys.stderr,
        )
        return

    # ``tmux new-window`` runs inside the user's existing tmux
    # session ($TMUX picks it up). The argument is the shell
    # command that the new window's pane will run — we hand
    # tmux another tmux invocation that attaches to the
    # AGENT's tmux server (different socket, distinct from
    # the user's outer session).
    inner = (
        f"tmux -S {shlex.quote(info.socket)} attach"
        f"{' -r' if read_only else ''} -t {shlex.quote(info.target)}"
    )
    try:
        _subprocess.run(
            ["tmux", "new-window", inner],
            check=True,
            timeout=5,
        )
    except (FileNotFoundError, _subprocess.CalledProcessError, _subprocess.TimeoutExpired):
        print(
            f"\nCan't open attach for {target.label}: tmux new-window failed. "
            f"Run manually: {inner}\n",
            file=_sys.stderr,
        )

def _terminal_attach_command(info: _TerminalInfo) -> str:
    """
    Build the shell command that attaches to *info*'s tmux session.

    Matches the legacy CLI's
    :meth:`omnigent.inner.cli._terminal_attach_command`
    output (``cli.py:2196``) so users with muscle memory from
    the non-AP path see the same string. ``shlex.quote``
    keeps the socket path safe for terminals where the path
    contains spaces (uncommon but possible on macOS).

    :param info: The terminal to attach to.
    :returns: A complete shell command,
        e.g. ``"tmux -S /tmp/.../sock attach -t main"``.
    """
    import shlex

    return f"tmux -S {shlex.quote(info.socket)} attach -t {shlex.quote(info.target)}"

async def _build_terminal_overview(
    decoded: tuple[str, str, str],
    *,
    target: OverlayTarget,
    client: OmnigentClient,
    fmt: RichBlockFormatter,
) -> RenderableType:
    """
    Render the content panel for a terminal sidebar target.

    Re-fetches the owning conversation's items and runs
    :func:`_reconstruct_terminals_from_items` again to find the
    matching terminal. This re-walk is what lets the encoded
    key stay short — the socket isn't on the key, it's read
    fresh from the persisted launch output.

    The panel mirrors the legacy CLI's
    :meth:`omnigent.inner.cli._render_overview_terminal_text`
    output (``cli.py:2232``):

      Terminal: <name>:<session>
      Owner: <conv_id>
      Status: live (per tool history)
      Socket: <tmux_socket>
      Attach: tmux -S <socket> attach -t main

    Plus a hint reminding the user that ``Status`` reflects
    the agent's last tool call, not a live process check.

    Failure modes (items fetch errors, terminal not found in
    the re-walk) surface inside the panel as inline text;
    overlays are diagnostic and shouldn't be the thing that
    crashes the REPL.

    :param decoded: ``(conv_id, name, session)`` tuple from
        :func:`_decode_terminal_target_key`.
    :param target: The selected :class:`OverlayTarget` —
        used only for the header label.
    :param client: Agent-plane HTTP client.
    :param fmt: REPL formatter for muted / accent styling.
    :returns: A :class:`rich.console.Group` for the overlay
        host's content area.
    """
    from rich.console import Group
    from rich.text import Text

    conv_id, name, session = decoded
    parts: list[RenderableType] = []
    parts.append(Text.from_markup(f"[bold]Terminal: {target.label}[/bold]"))
    parts.append(
        Text.from_markup(
            f"  [{fmt.muted}]Owner conversation[/{fmt.muted}]: {conv_id}",
        ),
    )

    # Re-walk to find the matching terminal. Paginate so
    # terminals whose launch outputs land past position 99
    # are still findable here — the user-reported 2026-04-30
    # symptom otherwise rendered "not found in conversation
    # history" for s18-s20 even though the launch outputs
    # existed (just past the cap).
    try:
        items = await _list_all_conversation_items(
            client,
            conv_id,
        )
    except Exception as exc:  # noqa: BLE001 — overlay content builder: any items-fetch error surfaces as a diagnostic line; the panel still renders
        parts.append(
            Text.from_markup(
                f"  [{fmt.error}]Failed to fetch conversation items: "
                f"{type(exc).__name__}: {exc}[/{fmt.error}]",
            ),
        )
        return Group(*parts)

    matches = [
        info
        for info in _reconstruct_terminals_from_items(items, conv_id=conv_id)
        if info.name == name and info.session == session
    ]
    if not matches:
        # Terminal isn't in the live set — either it was closed
        # since the sidebar was built, or the agent's tool
        # history doesn't include the launch. Either way the
        # action keys won't work, so we say so explicitly.
        parts.append(
            Text.from_markup(
                f"  [{fmt.muted}]Status[/{fmt.muted}]: "
                f"[{fmt.error}]not found in conversation history[/{fmt.error}]",
            ),
        )
        parts.append(Text(""))
        parts.append(
            Text.from_markup(
                f"[{fmt.muted}]The terminal may have been closed, or this "
                f"sidebar entry is stale. Reopen the overlay (Esc, then "
                f"Ctrl+O) to refresh.[/{fmt.muted}]",
            ),
        )
        return Group(*parts)

    info = matches[0]
    # Runtime liveness check via ``tmux has-session`` against
    # the recovered socket — the inferred-from-tool-history
    # view doesn't catch cases where the user attached and
    # exited the bash shell on a previous attach (which kills
    # the agent's pane → window → session → tmux server). The
    # walker still shows the terminal as live in those cases
    # because no ``sys_terminal_close`` was recorded. Querying
    # tmux directly here gives ground truth: the panel
    # reflects whether the user can ACTUALLY attach right now,
    # not the agent's last-known state.
    is_alive = _tmux_session_alive(info.socket, info.target)
    if is_alive:
        status_markup = f"[{fmt.success}]live[/{fmt.success}]"
    else:
        status_markup = f"[{fmt.error}]dead[/{fmt.error}]"
    parts.append(
        Text.from_markup(
            f"  [{fmt.muted}]Status[/{fmt.muted}]: {status_markup}",
        ),
    )
    parts.append(
        Text.from_markup(
            f"  [{fmt.muted}]Socket[/{fmt.muted}]: {info.socket}",
        ),
    )
    attach = _terminal_attach_command(info)
    parts.append(
        Text.from_markup(
            f"  [{fmt.muted}]Attach[/{fmt.muted}]: [{fmt.accent}]{attach}[/{fmt.accent}]",
        ),
    )
    parts.append(Text(""))
    if is_alive:
        snapshot = _tmux_pane_snapshot(info.socket, info.target)
        parts.append(Text.from_markup(f"[{fmt.muted}]Screen snapshot[/{fmt.muted}]:"))
        if snapshot is None:
            parts.append(
                Text.from_markup(
                    f"  [{fmt.error}]unavailable (tmux capture-pane failed)[/{fmt.error}]",
                ),
            )
        elif not snapshot.strip():
            parts.append(
                Text.from_markup(
                    f"  [{fmt.muted}](empty terminal screen)[/{fmt.muted}]",
                ),
            )
        else:
            for line in snapshot.rstrip("\n").splitlines():
                parts.append(Text(f"  {line}"))
        parts.append(Text(""))
        parts.append(
            Text.from_markup(
                f"[{fmt.muted}]Press [/{fmt.muted}]"
                f"[{fmt.accent}]O[/{fmt.accent}]"
                f"[{fmt.muted}] to attach in a new tmux window, "
                f"or [/{fmt.muted}]"
                f"[{fmt.accent}]R[/{fmt.accent}]"
                f"[{fmt.muted}] to attach read-only. You can also "
                f"copy the Attach command above and run it manually.[/{fmt.muted}]",
            ),
        )
    else:
        parts.append(
            Text.from_markup(
                f"[{fmt.muted}]The agent's tmux session is gone (e.g. the "
                f"shell exited on a previous attach, killing the pane). The "
                f"agent doesn't know — no ``sys_terminal_close`` was "
                f"recorded — so the sidebar still shows the row. Ask the "
                f"agent to launch a new terminal to recover.[/{fmt.muted}]",
            ),
        )
    return Group(*parts)

def _tmux_session_alive(socket: str, target: str) -> bool:
    """
    Probe whether ``tmux has-session`` succeeds against *socket*.

    Used by :func:`_build_terminal_overview` to surface the real
    runtime liveness of an agent-launched tmux session — the
    inferred-from-tool-history view alone can't catch sessions
    the agent didn't formally close (e.g. the user attached,
    typed ``exit`` in the bash pane, killing the pane → window
    → session → tmux server). The agent never knew, so no
    ``sys_terminal_close`` ended up in the conversation, so the
    walker still shows the row as live.

    Best-effort: any subprocess error (tmux missing, timeout,
    permission glitch on the socket) returns ``False`` so the
    panel surfaces "dead" rather than crashing the overlay.
    The Attach command stays printed regardless — the user can
    still try it manually.

    :param socket: Tmux socket path the agent's
        :class:`TerminalInstance` opened on, e.g.
        ``"/tmp/omnigent-terminal-xyz/tmux.sock"``.
    :param target: Tmux session/target name. Always
        ``"main"`` per
        :class:`omnigent.inner.terminal.TerminalInstance.tmux_target`.
    :returns: ``True`` only when ``tmux has-session`` exits
        zero — i.e. the session is reachable on the socket.
    """
    import subprocess as _subprocess

    try:
        result = _subprocess.run(
            ["tmux", "-S", socket, "has-session", "-t", target],
            capture_output=True,
            timeout=5,
        )
    except (FileNotFoundError, _subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0

def _tmux_pane_snapshot(socket: str, target: str) -> str | None:
    """
    Capture the current visible tmux pane text for a terminal overview.

    The Ctrl+O debug overlay calls this only after
    :func:`_tmux_session_alive` reports that the terminal is reachable.
    The helper still treats every subprocess failure as non-fatal
    because the overlay is diagnostic: stale sockets, missing tmux, or
    permission errors should render an inline "unavailable" line rather
    than crashing the REPL.

    :param socket: Tmux socket path the agent's
        :class:`TerminalInstance` opened on, e.g.
        ``"/tmp/omnigent-terminal-xyz/tmux.sock"``.
    :param target: Tmux session/target name, e.g. ``"main"``.
    :returns: The current visible pane text from ``tmux capture-pane
        -p``, decoded as UTF-8 with replacement for invalid bytes.
        Returns ``None`` when tmux cannot capture the pane.
    """
    import subprocess as _subprocess

    try:
        result = _subprocess.run(
            ["tmux", "-S", socket, "capture-pane", "-t", target, "-p"],
            capture_output=True,
            timeout=5,
        )
    except (FileNotFoundError, _subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")

def _parse_sub_agent_handle(raw: str) -> dict[str, object] | None:
    """
    Extract a sys_session_send handle dict from a function_call_output.

    Native omnigent builtins (``sys_session_send`` /
    ``sys_session_send`` continuation on the builtin path) persist the output
    as a raw JSON string of the handle dict —
    ``{"kind": "sub_agent", "conversation_id": ..., ...}``.

    Harnesses that route tools through an MCP server — notably the
    claude-sdk harness's MCP bridge — wrap the same payload as an
    MCP content-part list before persistence:
    ``[{"type": "text", "text": "<handle-json-string>"}]``. Without
    the second branch here, the overlay silently drops every
    sub-agent row on that harness, which manifests as zero
    sub-agent tabs even while ``list_tasks`` reports the children
    live (reported against coding_supervisor_with_forks on 2026-04-22).

    Both shapes are tolerated; anything else returns ``None`` so
    the caller's loop can skip the item cleanly.

    :param raw: The ``function_call_output.output`` string as
        persisted by the workflow, e.g. a handle JSON string or an
        MCP content-parts JSON string.
    :returns: The handle dict when *raw* parses to one of the two
        recognized shapes AND carries ``kind == "sub_agent"``;
        ``None`` otherwise.
    """
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(payload, dict):
        return payload if payload.get("kind") == "sub_agent" else None
    if isinstance(payload, list):
        # MCP content-parts wrapper. Walk parts, parse the first
        # ``text`` part that decodes to a sub_agent handle. Multiple
        # text parts on a single tool result are valid per the MCP
        # spec, but sys_session_send emits exactly one — return the
        # first match rather than aggregating.
        for part in payload:
            if not isinstance(part, dict):
                continue
            if part.get("type") != "text":
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            try:
                inner = json.loads(text)
            except (ValueError, TypeError):
                continue
            if isinstance(inner, dict) and inner.get("kind") == "sub_agent":
                return inner
    return None

async def _build_debug_overview(
    target: OverlayTarget,
    *,
    client: OmnigentClient,
    session: Session,
    agent_name: str,
    fmt: RichBlockFormatter,
    server_log_path: pathlib.Path | None = None,
    runner_log_path: pathlib.Path | None = None,
    event_log_path: pathlib.Path | None = None,
    cli_log_path: pathlib.Path | None = None,
) -> RenderableType:
    """
    Assemble the Ctrl+O debug overview for the REPL.

    The overview intentionally mirrors the ``omnigent run``
    debug panel: a "Session: main" header with session id /
    agent / response / conversation metadata, followed by an
    indexed event stream where every conversation item is
    printed as ``[N] type=...`` with its fields on the
    following indented lines. This keeps the two CLIs visually
    consistent when comparing behavior across harnesses.

    Sections (in order):

    1. **Session header** — ``Session: main``, Session ID
       (conversation id), Agent, Model, Response, Messages
       count. Matches :func:`_render_overview_session_text`
       in ``omnigent/cli.py``.
    2. **Event stream** — all items from the conversation,
       paginated via :func:`_list_all_conversation_items`,
       re-rendered via
       :func:`_render_overview_event` into the same
       ``[N] type=...`` shape omnigent uses. Responses API
       items map onto the omnigent event vocabulary as
       follows: ``message`` (user) → ``user_message``;
       ``message`` (assistant) → ``assistant_message``;
       ``function_call`` → ``tool_call_request``;
       ``function_call_output`` → ``tool_call_complete``.
       Reasoning items are shown as ``reasoning``.
    3. **Fallback** — when no conversation exists yet (fresh
       REPL, no turns), a one-liner explaining the state.

    Errors from the ``/v1/sessions`` fetch surface inside
    the overlay as a red line rather than propagating — Ctrl+O is
    a debug surface, not a critical path, and a transient server
    hiccup shouldn't kill the overlay.

    :param client: The omnigent client used for the items fetch.
    :param session: The client ``Session`` tracking
        ``current_response_id``.
    :param agent_name: Registered agent name for the header, e.g.
        ``"coding_supervisor"``.
    :param fmt: The REPL's :class:`RichBlockFormatter` — reused so
        the overview uses the same palette (muted / accent / error
        colors) as the scrollback.
    :param server_log_path: Optional path to the local server log.
    :param event_log_path: Optional path to the JSONL event log.
    :param cli_log_path: Optional path to the always-on CLI
        diagnostics log (``~/.omnigent/logs/cli-*.log``).
    :returns: A Rich :class:`Group` suitable for passing to
        :meth:`TerminalHost.add_overlay`'s ``builder`` contract.
    """
    from rich.console import Group
    from rich.text import Text

    # Terminal targets short-circuit to a dedicated renderer —
    # they're not conversations and the items-fetch / event-stream
    # path below doesn't apply. The decoded key carries everything
    # needed (conv_id, name, session); the renderer re-walks the
    # owning conversation's items to recover the socket and emit
    # an attach command.
    if target is not None:
        decoded = _decode_terminal_target_key(target.key)
        if decoded is not None:
            return await _build_terminal_overview(
                decoded,
                target=target,
                client=client,
                fmt=fmt,
            )

    # Branch on the selected sidebar target. The ``main`` target's
    # key holds either the current conversation id (once a turn
    # has started) or the sentinel string ``"main"`` (fresh REPL).
    # Sub-agent targets' keys are always real conversation ids by
    # construction in :func:`_collect_overview_targets`.
    is_main = target is None or target.label == "main"
    resolve_error: str | None = None
    conv_id: str | None = None
    response_id = session.current_response_id if is_main else None

    if is_main:
        if target is not None and target.key != "main":
            # Sidebar already resolved the conversation id when
            # building the target list — skip the extra round-trip.
            conv_id = target.key
        elif response_id:
            try:
                resp = await client.responses.get(response_id)
                conv_id = resp.conversation.id if resp.conversation else None
            except Exception as exc:  # noqa: BLE001 — overlay content builder: capture any lookup error as a displayable string; the overlay panel must still render even under partial failure
                resolve_error = f"{type(exc).__name__}: {exc}"
        else:
            # Sessions-API path: no response_id but the adapter
            # may already have a session_id (== conv_id). Read it
            # off rather than leaving the overlay empty.
            _sid = getattr(session, "session_id", None)
            if isinstance(_sid, str):
                conv_id = _sid
    else:
        conv_id = target.key

    parts: list[RenderableType] = []
    header = target.label if target is not None else "main"
    parts.append(Text.from_markup(f"[bold]Session: {header}[/bold]"))
    parts.append(
        Text.from_markup(
            f"  [{fmt.muted}]Session ID[/{fmt.muted}]: {conv_id or '(no conversation yet)'}",
        ),
    )
    if is_main:
        parts.append(
            Text.from_markup(
                f"  [{fmt.muted}]Agent[/{fmt.muted}]: {agent_name}",
            ),
        )
        parts.append(
            Text.from_markup(
                f"  [{fmt.muted}]Model[/{fmt.muted}]: {session.model}",
            ),
        )
        parts.append(
            Text.from_markup(
                f"  [{fmt.muted}]Response[/{fmt.muted}]: {response_id or '(none yet)'}",
            ),
        )
        if server_log_path is not None:
            parts.append(
                Text.from_markup(
                    f"  [{fmt.muted}]Server log[/{fmt.muted}]: {server_log_path}",
                ),
            )
        if runner_log_path is not None:
            parts.append(
                Text.from_markup(
                    f"  [{fmt.muted}]Runner log[/{fmt.muted}]: {runner_log_path}",
                ),
            )
        if event_log_path is not None:
            parts.append(
                Text.from_markup(
                    f"  [{fmt.muted}]Event log[/{fmt.muted}]: {event_log_path}",
                ),
            )
        if cli_log_path is not None:
            parts.append(
                Text.from_markup(
                    f"  [{fmt.muted}]CLI log[/{fmt.muted}]: {cli_log_path}",
                ),
            )

    if resolve_error is not None:
        parts.append(
            Text.from_markup(
                f"  [{fmt.error}]Failed to resolve conversation: {resolve_error}[/{fmt.error}]",
            ),
        )

    if conv_id is None:
        parts.append(Text(""))
        parts.append(
            Text.from_markup(
                f"[{fmt.muted}]No conversation yet. Send a message to start one.[/{fmt.muted}]",
            ),
        )
        return Group(*parts)

    # Fetch conversation-level metadata (labels) alongside items so
    # the overlay can render guardrails label state — the legacy
    # Ctrl+G overview shows ``Labels: key=val, ...`` on every
    # session, and Ctrl+O should match. Failure here is non-fatal:
    # the overlay is diagnostic and should still render the event
    # stream if the labels fetch hiccups.
    labels: dict[str, str] = {}
    labels_error: str | None = None
    items: list[dict[str, object]] = []
    items_error: str | None = None
    try:
        snap = await client.sessions.get(conv_id)
        labels = snap.labels
    except Exception as exc:  # noqa: BLE001 — overlay content builder
        labels_error = f"{type(exc).__name__}: {exc}"
    try:
        items = await _list_all_conversation_items(
            client,
            conv_id,
        )
    except Exception as exc:  # noqa: BLE001 — overlay content builder
        items_error = f"{type(exc).__name__}: {exc}"
    if items_error is not None:
        parts.append(
            Text.from_markup(
                f"\n[{fmt.error}]Failed to fetch conversation items: {items_error}[/{fmt.error}]",
            ),
        )
        return Group(*parts)

    # Render ``Labels: key=val, ...`` (sorted by key, ``(none)`` when
    # empty) directly after the session header to mirror the legacy
    # Ctrl+G overview's ``_format_session_labels`` output line for
    # line. Placed before ``Messages`` so the label state is visible
    # even when the conversation has many items.
    if labels_error is not None:
        parts.append(
            Text.from_markup(
                f"  [{fmt.error}]Labels fetch failed: {labels_error}[/{fmt.error}]",
            ),
        )
    else:
        rendered = ", ".join(f"{k}={v}" for k, v in sorted(labels.items())) if labels else "(none)"
        parts.append(Text.from_markup(f"  [{fmt.muted}]Labels[/{fmt.muted}]: {rendered}"))
    parts.append(Text.from_markup(f"  [{fmt.muted}]Messages[/{fmt.muted}]: {len(items)}"))
    parts.append(Text(""))

    if not items:
        parts.append(
            Text.from_markup(
                f"[{fmt.muted}](no messages yet)[/{fmt.muted}]",
            ),
        )
        return Group(*parts)

    # Pre-pass: function_call items hold the tool name, but the
    # matching function_call_output only carries ``call_id`` +
    # ``output``. Index names by call_id so the event stream
    # renders ``name=Bash`` on both sides of the request/complete
    # pair — matching the omnigent event view. When an output
    # arrives without a prior request (e.g. the server trimmed
    # the head of the history), we fall back to ``"?"`` in the
    # renderer.
    call_id_to_name: dict[str, str] = {}
    for item in items:
        if item.get("type") == "function_call":
            call_id = item.get("call_id")
            name = item.get("name")
            if isinstance(call_id, str) and isinstance(name, str):
                call_id_to_name[call_id] = name

    for idx, item in enumerate(items, start=1):
        parts.extend(_render_overview_event(idx, item, call_id_to_name, fmt))

    return Group(*parts)

def _render_overview_event(
    idx: int,
    item: dict[str, object],
    call_id_to_name: dict[str, str],
    fmt: RichBlockFormatter,
) -> list[RenderableType]:
    """
    Render one conversation item as an omnigent-style event.

    Produces a header line ``[N] type=<event>`` followed by
    indented field lines (``name: ...``, ``args: ...``,
    ``status: ...``, ``result: ...``). The per-type field set
    matches what ``omnigent/cli.py::_render_overview_item``
    emits so the two overviews read identically.

    :param idx: 1-based index for the ``[N]`` header.
    :param item: Raw conversation-items dict as returned by the
        ``/v1/sessions/<id>/items`` endpoint. Expected
        fields depend on ``item["type"]``: ``message`` carries
        ``role`` + ``content`` parts; ``function_call`` carries
        ``name`` + ``arguments`` + ``call_id``;
        ``function_call_output`` carries ``call_id`` + ``output``;
        ``reasoning`` carries ``summary``.
    :param call_id_to_name: Precomputed ``call_id → tool_name``
        lookup built from the same items list — used to print
        the tool name on ``tool_call_complete`` lines where the
        raw item only has a call_id.
    :param fmt: The REPL formatter used for color styling.
    :returns: A list of Rich renderables (one per line) ready to
        append to the overview :class:`Group`.
    """
    from rich.text import Text

    # Missing ``type`` is an API violation — every conversation
    # item ships with a discriminator. Render it as ``(unknown)``
    # so the sidebar surfaces the broken row rather than silently
    # swallowing it; a fresh server-side type that this switch
    # doesn't recognise falls into the same branch.
    itype = item.get("type")
    if itype == "message":
        return _render_overview_message_event(idx, item, fmt)
    if itype == "function_call":
        name = item.get("name")
        if not isinstance(name, str):
            # ``name`` is required for function_call per API.md.
            # A missing name means the item is malformed; render
            # the event so scroll context isn't lost, but flag
            # the missing field explicitly.
            name = "(missing name)"
        lines: list[RenderableType] = [
            Text.from_markup(
                f"[{fmt.accent}][{idx}][/{fmt.accent}] "
                f"[bold]type[/bold]=tool_call_request "
                f"[{fmt.muted}]name={name}[/{fmt.muted}]",
            ),
        ]
        args = item.get("arguments")
        if args:
            lines.append(Text.from_markup(f"    [{fmt.muted}]args[/{fmt.muted}]: {args}"))
        return lines
    if itype == "function_call_output":
        call_id = item.get("call_id")
        # ``call_id`` is required per API.md, so a missing entry
        # means the item is malformed. Skip the name-lookup in
        # that case — ``(missing call_id)`` tells the reader the
        # item couldn't be correlated to its request.
        if isinstance(call_id, str):
            name = call_id_to_name.get(call_id) or "(unknown)"
        else:
            name = "(missing call_id)"
        lines = [
            Text.from_markup(
                f"[{fmt.accent}][{idx}][/{fmt.accent}] "
                f"[bold]type[/bold]=tool_call_complete "
                f"[{fmt.muted}]name={name}[/{fmt.muted}]",
            ),
        ]
        status = item.get("status")
        if status:
            lines.append(Text.from_markup(f"    [{fmt.muted}]status[/{fmt.muted}]: {status}"))
        output = item.get("output")
        if output:
            text = str(output)
            preview = text[:400]
            if len(text) > 400:
                preview += "…"
            for line in preview.split("\n"):
                lines.append(Text.from_markup(f"    [{fmt.muted}]{line}[/{fmt.muted}]"))
        return lines
    if itype == "reasoning":
        # ``summary`` and ``content`` are both optional on
        # reasoning items (different providers populate
        # different fields). Use ``None`` as the absence
        # sentinel, not ``""``, so the truthy check below
        # doesn't conflate an empty string with a missing
        # field.
        summary = item.get("summary") or item.get("content")
        lines = [
            Text.from_markup(
                f"[{fmt.accent}][{idx}][/{fmt.accent}] [bold]type[/bold]=reasoning",
            ),
        ]
        if summary:
            text = str(summary)[:400]
            for line in text.split("\n"):
                if line.strip():
                    lines.append(Text.from_markup(f"    [{fmt.muted}]{line}[/{fmt.muted}]"))
        return lines
    # Unknown item type — surface it rather than silently dropping,
    # so new server-side types become visible instead of invisible.
    label = itype if isinstance(itype, str) and itype else "(unknown)"
    return [
        Text.from_markup(
            f"[{fmt.accent}][{idx}][/{fmt.accent}] [bold]type[/bold]={label}",
        ),
    ]

def _render_overview_message_event(
    idx: int,
    item: dict[str, object],
    fmt: RichBlockFormatter,
) -> list[RenderableType]:
    """
    Render a ``message`` item as an omnigent event.

    Splits on role so the header reads ``user_message`` or
    ``assistant_message`` (matching omnigent' event vocabulary)
    instead of the raw Responses API ``message`` label. The
    content parts are flattened to text and printed on indented
    continuation lines, preview-capped at 400 chars so one
    long system-synthesized message doesn't dominate the pane.

    :param idx: 1-based index for the ``[N]`` header.
    :param item: Conversation item with ``role`` and ``content``.
    :param fmt: Formatter for color styling.
    :returns: Rich renderables — header + continuation lines.
    """
    from rich.text import Text

    # ``role`` is required on message items per API.md. A message
    # with a missing role has no meaningful event vocabulary to
    # map onto; fall through to the assistant form (the more
    # visually-obvious side) so the user sees the broken row.
    role = item.get("role")
    event_type = "user_message" if role == "user" else "assistant_message"
    content = item.get("content") or []
    text_parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("input_text", "output_text"):
                # ``text`` is the payload of every text content
                # part; missing it means the block is malformed.
                # Render the non-text blocks as empty rather than
                # as the literal string "None" by skipping here.
                block_text = block.get("text")
                if isinstance(block_text, str):
                    text_parts.append(block_text)
    text = " ".join(text_parts)
    header = f"[{fmt.accent}][{idx}][/{fmt.accent}] [bold]type[/bold]={event_type}"
    if role == "assistant":
        model = item.get("model")
        if isinstance(model, str) and model:
            header += f" [{fmt.muted}]model={model}[/{fmt.muted}]"
    lines: list[RenderableType] = [Text.from_markup(header)]
    preview = text[:400]
    if len(text) > 400:
        preview += "…"
    for line in preview.split("\n"):
        if line.strip():
            lines.append(Text.from_markup(f"    [{fmt.muted}]{line}[/{fmt.muted}]"))
    return lines


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _adapter as _sib_adapter
    from . import _approval as _sib_approval
    from . import _commands as _sib_commands
    from . import _context as _sib_context
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
    from . import _model as _sib_model
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
    for _key, _value in _sib_entry.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_helpers.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_model.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_render.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_startup.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
