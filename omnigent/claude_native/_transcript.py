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

def _fetch_external_session_id_for_redirect(
    *,
    base_url: str | None,
    headers: dict[str, str],
    session_id: str,
) -> str | None:
    """
    Fetch Claude's external session id for optional redirect.

    Redirect is an optional convenience layered on top of the normal
    resume path. If the lookup fails, return ``None`` and leave the
    regular switch / leave behavior available; the later cold resume
    path still performs the authoritative server validation.

    :param base_url: Omnigent server base URL, or ``None`` when unavailable.
    :param headers: HTTP headers for the Omnigent request.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :returns: Claude session id, e.g.
        ``"02857840-6362-408f-b41f-309e396ed7c6"``, or ``None``.
    """
    if base_url is None:
        return None
    try:
        with httpx.Client(base_url=base_url, headers=headers, timeout=10.0) as client:
            resp = client.get(f"/v1/sessions/{url_component(session_id)}")
        if resp.status_code >= 400:
            return None
        payload = resp.json()
    except Exception:  # noqa: BLE001 - optional redirect preflight
        _logger.warning(
            "failed to fetch external Claude session id for redirect; session=%s",
            session_id,
            exc_info=True,
        )
        return None
    external_session_id = payload.get("external_session_id") if isinstance(payload, dict) else None
    if not isinstance(external_session_id, str) or not external_session_id:
        return None
    return external_session_id

def _redirect_claude_transcript_to_current_project(
    *,
    session_id: str,
    external_session_id: str,
    current: Path,
) -> Path:
    """
    Move a Claude transcript into the current cwd's Claude project.

    The moved JSONL gets top-level ``cwd`` fields rewritten to
    *current* so Claude sees the session as belonging to the current
    project directory. The old transcript file is removed after the
    new file is safely in place; a Claude session id has exactly one
    local project owner.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param external_session_id: Claude session id / transcript stem,
        e.g. ``"02857840-6362-408f-b41f-309e396ed7c6"``.
    :param current: Current cwd, already resolved.
    :returns: Path to the moved transcript.
    :raises click.ClickException: If the source transcript is missing
        or unsafe.
    """
    target_dir = _claude_project_dir_for_cwd(current)
    target = target_dir / f"{external_session_id}.jsonl"
    source = _find_claude_transcript(external_session_id, exclude=target)
    if source is None and target.is_file():
        redirect_launch_state(session_id, str(current))
        return target
    if source is None:
        raise click.ClickException(
            f"Claude transcript {external_session_id!r} was not found under "
            f"{_CLAUDE_PROJECTS_DIR}."
        )
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = target.with_suffix(".jsonl.tmp")
    try:
        _copy_transcript_with_cwd(source=source, target=tmp, current=current)
        os.replace(tmp, target)
        source.unlink()
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    redirect_launch_state(session_id, str(current))
    click.echo(f"Moved Claude transcript to: {target}", err=True)
    return target

def _copy_transcript_with_cwd(
    *, source: Path, target: Path, current: Path, new_session_id: str | None = None
) -> None:
    """
    Copy *source* JSONL to *target* while rewriting top-level cwd.

    :param source: Existing Claude transcript JSONL.
    :param target: Temporary output path.
    :param current: Current cwd to write into top-level ``cwd`` fields.
    :param new_session_id: When set, also rewrite each record's
        top-level ``sessionId`` to this value, e.g.
        ``"ca414b0e-..."``. Used by :func:`_clone_claude_transcript`
        (forked clone) so the copied transcript belongs to the
        clone's own Claude session id rather than the source's. ``None``
        (the cwd-only redirect/move path) leaves ``sessionId`` untouched.
        The ``uuid`` / ``parentUuid`` chain is preserved verbatim in
        either case.
    :returns: None.
    :raises click.ClickException: If a transcript line is malformed.
    """
    current_text = str(current)
    with source.open("r", encoding="utf-8") as src, target.open("w", encoding="utf-8") as dst:
        for line_number, line in enumerate(src, start=1):
            if not line.strip():
                dst.write(line)
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise click.ClickException(
                    f"Cannot redirect malformed Claude transcript {source}: "
                    f"line {line_number} is not valid JSON."
                ) from exc
            if isinstance(payload, dict):
                if isinstance(payload.get("cwd"), str):
                    payload["cwd"] = current_text
                if new_session_id is not None and isinstance(payload.get("sessionId"), str):
                    payload["sessionId"] = new_session_id
            dst.write(json.dumps(payload, separators=(",", ":")) + "\n")

def _clone_claude_transcript(
    *,
    source_external_session_id: str,
    target_external_session_id: str,
    clone_workspace: Path,
) -> Path | None:
    """
    Clone a source Claude transcript into the clone's project dir.

    Used to carry a forked claude-native session's history into the
    clone. We copy the source transcript ourselves into the clone's OWN
    project dir (``~/.claude/projects/<enc(clone_workspace)>/``) under a
    uuid we assign, rewriting per-record ``sessionId`` →
    *target_external_session_id* and ``cwd`` → *clone_workspace* (the
    ``uuid`` / ``parentUuid`` chain is preserved). The clone then
    launches plain ``--resume <target_external_session_id>``. Writing
    the file ourselves (rather than asking Claude to branch the source
    via ``--fork-session``) is what makes the worktree case work and
    avoids a double-render: the file is fully written before launch, so
    the forwarder's ``start_at_end`` seeks past the copied prefix, and
    it lives in the clone's own project dir, so cwd-scoped ``--resume``
    finds it regardless of which dir/worktree the clone runs in. See
    designs/FORK_SESSION_UX.md.

    :param source_external_session_id: The SOURCE session's Claude id /
        transcript stem to copy from, e.g.
        ``"d39070df-e10a-4de9-b078-a11b35d5b1fc"``.
    :param target_external_session_id: The uuid to assign the clone's
        copied transcript, e.g. ``"ca414b0e-..."``. Must be a safe
        transcript stem; the clone's ``external_session_id`` is set to
        this so a later relaunch resumes it via the normal cold-resume
        path.
    :param clone_workspace: The resolved directory the clone will run
        in (its worktree or same dir). Determines the destination
        project dir and the rewritten ``cwd`` value. Pass an
        already-resolved path (symlinks collapsed) so the project-dir
        encoding matches what Claude computes.
    :returns: Path to the written clone transcript, or ``None`` when the
        target id is unsafe or the source transcript can't be found on
        this host (caller launches fresh in that case).
    :raises click.ClickException: If the source transcript is malformed
        or the clone can't be written.
    """
    if not _CLAUDE_SESSION_ID_RE.fullmatch(target_external_session_id):
        return None
    source = _find_claude_transcript(source_external_session_id)
    if source is None:
        return None
    target_dir = _claude_project_dir_for_cwd(clone_workspace)
    target = target_dir / f"{target_external_session_id}.jsonl"
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = target.with_suffix(".jsonl.tmp")
    try:
        _copy_transcript_with_cwd(
            source=source,
            target=tmp,
            current=clone_workspace,
            new_session_id=target_external_session_id,
        )
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    return target

def _find_claude_transcript(
    external_session_id: str, *, exclude: Path | None = None
) -> Path | None:
    """
    Find a local Claude transcript by session id.

    :param external_session_id: Claude session id / transcript stem,
        e.g. ``"02857840-6362-408f-b41f-309e396ed7c6"``.
    :param exclude: Transcript path to ignore, e.g. the redirect
        target for the current cwd. ``None`` means include all matches.
    :returns: Transcript path, or ``None`` when absent.
    """
    if not _CLAUDE_SESSION_ID_RE.fullmatch(external_session_id):
        return None
    if not _CLAUDE_PROJECTS_DIR.is_dir():
        return None
    matches: list[Path] = []
    filename = f"{external_session_id}.jsonl"
    excluded = exclude.resolve() if exclude is not None else None
    for project_dir in _CLAUDE_PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / filename
        if candidate.is_file() and (excluded is None or candidate.resolve() != excluded):
            matches.append(candidate)
    if not matches:
        return None
    matches.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return matches[0]

def _claude_project_dir_for_cwd(cwd: Path) -> Path:
    """
    Return Claude's project transcript directory for *cwd*.

    Claude Code stores transcripts under
    ``~/.claude/projects/<sanitized-cwd>/``. The observed sanitizer
    replaces non-alphanumeric path characters with ``-``.

    :param cwd: Absolute cwd, e.g. ``Path("/home/me/repo")``.
    :returns: Claude project transcript directory.
    """
    return _CLAUDE_PROJECTS_DIR / _sanitize_claude_project_name(str(cwd))

def _sanitize_claude_project_name(path: str) -> str:
    """
    Sanitize an absolute path the way Claude names project dirs.

    :param path: Absolute path, e.g. ``"/home/me/repo"``.
    :returns: Sanitized name, e.g. ``"-home-me-repo"``.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", path)

async def _attach_with_transcript_forwarder(
    *,
    base_url: str,
    headers: dict[str, str],
    prepared: PreparedClaudeTerminal,
    agent_name: str,
    attach_url: str,
    attach: Callable[..., Any],
    recover: Callable[[], Awaitable[None]] | None = None,
    auth: httpx.Auth | None = None,
    run_transcript_forwarder: bool = True,
    startup_profiler: StartupProfiler | None = None,
) -> _AttachOutcome:
    """
    Attach to the terminal and optionally mirror Claude transcript output.

    The attach is wrapped in :func:`_attach_with_reconnect` so a
    server bounce does not end the session — the local runner +
    tmux survive the bounce, and the runner's tunnel reconnects on
    its own backoff. On exit the forwarder is cancelled and the
    AP-side terminal resource is best-effort marked stopped (skipped
    on reattach — the launcher owns teardown).

    :param base_url: Omnigent server base URL.
    :param headers: Static HTTP auth headers for Omnigent requests. For
        long-lived remote sessions, ``auth`` (not ``headers``) is the
        authoritative source of the bearer token so OAuth tokens
        refresh transparently per request.
    :param prepared: Prepared terminal details.
    :param agent_name: Agent/model name for mirrored Claude output.
    :param attach_url: Terminal WebSocket URL.
    :param attach: Async attach callable, usually
        :func:`attach_local_terminal`.
    :param recover: Optional async callback invoked between attach
        attempts. ``None`` disables reconnect (local-server flow).
    :param auth: Optional httpx Auth that mints a fresh bearer token
        per request, e.g. ``_server_auth(profile)``. Forwarded to the
        transcript forwarder's HTTP client so Omnigent posts continue to
        authenticate after Databricks OAuth token expiry (~1h).
    :param run_transcript_forwarder: Whether this attach process owns
        Claude transcript forwarding. ``False`` for daemon/runner-owned
        launches, where the runner already started the forwarder for
        the same bridge and a second tailer would duplicate messages.
    :param startup_profiler: Optional startup profiler for timing
        marks. ``None`` disables output.
    :returns: How the session ended — :attr:`_AttachOutcome.DETACHED`
        when the user detached from tmux (runner kept alive), else
        :attr:`_AttachOutcome.EXITED`.
    """
    startup_profiler = startup_profiler or StartupProfiler(name="omnigent claude", enabled=False)
    # ``start_at_end`` covers both reattach (terminal still live,
    # transcript JSONL still growing) and cold resume (new terminal
    # but ``claude --resume <sid>`` reopens the prior transcript so
    # offset 0 contains turns Omnigent already has from the previous run).
    # See ``PreparedClaudeTerminal.cold_resumed`` for the duplicate-
    # broadcast hazard this avoids.
    skip_existing_transcript = prepared.reattached or prepared.cold_resumed
    forwarder: asyncio.Task[None] | None = None
    if run_transcript_forwarder:
        forwarder = asyncio.create_task(
            supervise_forwarder(
                base_url=base_url,
                headers=headers,
                session_id=prepared.session_id,
                bridge_dir=prepared.bridge_dir,
                agent_name=agent_name,
                start_at_end=skip_existing_transcript,
                auth=auth,
            ),
            name="claude-native-transcript-forwarder",
        )
        startup_profiler.mark("transcript forwarder started")
    else:
        startup_profiler.mark("transcript forwarder skipped")
    outcome = _AttachOutcome.EXITED
    try:
        if _can_attach_direct_tmux(prepared):
            # Same machine as the runner: attach straight to its tmux
            # pane for a lower-latency TTY than the WebSocket PTY relay.
            # Transcript forwarding is owned by whichever process launched
            # the terminal; this attach path only handles the TTY.
            # A remote runner's socket won't exist locally, so we take
            # the WebSocket path instead.
            if prepared.tmux_socket is None or prepared.tmux_target is None:
                # Unreachable — ``_can_attach_direct_tmux`` already
                # checked both — but narrows the types for the call below.
                raise click.ClickException("Claude tmux attach metadata was incomplete.")
            startup_profiler.mark(
                "opening direct tmux attach",
                detail=f"target={prepared.tmux_target}",
            )
            outcome = await _attach_direct_tmux(
                prepared.tmux_socket,
                prepared.tmux_target,
                startup_profiler=startup_profiler,
            )
        else:
            startup_profiler.mark("opening websocket terminal attach")
            outcome = await _attach_with_reconnect(
                attach=attach,
                attach_url=attach_url,
                headers=headers,
                recover=recover,
                base_url=base_url,
                session_id=prepared.session_id,
                terminal_id=prepared.terminal_id,
                bridge_dir=prepared.bridge_dir,
                close_attach_on_terminal_gone=attach is attach_local_terminal,
            )
    finally:
        if forwarder is not None:
            forwarder.cancel()
            try:
                await forwarder
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — cleanup must run regardless
                # The forwarder is best-effort mirroring. A bug there
                # (corrupt transcript JSONL, file-system error, anything
                # uncaught in the parser) must not skip the Omnigent terminal
                # stop call below — otherwise the web UI shows a phantom
                # live terminal after the wrapper exits.
                _logger.warning(
                    "claude-native transcript forwarder raised on shutdown",
                    exc_info=True,
                )
        # On detach the tmux session — and Claude — is still running, so
        # the Omnigent terminal resource must stay live (the web UI keeps
        # rendering it). Only mark it stopped on a real exit.
        if not prepared.reattached and outcome is not _AttachOutcome.DETACHED:
            active_session_id = read_active_session_id(prepared.bridge_dir) or prepared.session_id
            await _close_claude_terminal(
                base_url=base_url,
                headers=headers,
                session_id=active_session_id,
                terminal_id=prepared.terminal_id,
            )
    return outcome

async def _ensure_local_claude_resume_transcript(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    external_session_id: str,
    workspace: Path,
) -> Path | None:
    """
    Refresh Claude Code's local JSONL transcript for cold resume.

    Cross-machine resume has the Omnigent conversation and Claude external
    session id on the server, but not Claude Code's local
    ``~/.claude/projects/<cwd>/<sid>.jsonl`` file. Claude's
    ``--resume <sid>`` consults that local project transcript. The
    wrapper always rewrites it from committed Omnigent items before launch so
    Omnigent remains the source of truth when a previous local Claude JSONL
    has diverged.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param external_session_id: Claude-native session id, e.g.
        ``"02857840-6362-408f-b41f-309e396ed7c6"``.
    :param workspace: Resolved directory Claude will run in — its
        ``~/.claude/projects/<encoded-workspace>/`` is where the
        transcript must land for ``--resume`` to find it. The CLI
        passes ``Path.cwd()``; a runner-side launch passes its
        ``OMNIGENT_RUNNER_WORKSPACE``. Pass an already-resolved
        path (symlinks collapsed) so the project-dir encoding matches
        what Claude computes.
    :returns: Path to the local transcript that was written; ``None`` if
        *external_session_id* is not a safe transcript stem, or if the AP
        history yields no resumable records (an empty transcript would make
        ``claude --resume`` exit instead of start, so the caller must launch
        fresh).
    :raises click.ClickException: If Omnigent history cannot be fetched or
        the transcript cannot be written.
    """
    if not _CLAUDE_SESSION_ID_RE.fullmatch(external_session_id):
        return None
    current = workspace
    target_dir = _claude_project_dir_for_cwd(current)
    target = target_dir / f"{external_session_id}.jsonl"

    items = await _fetch_all_session_items_for_claude_resume(client, session_id)
    records = _claude_transcript_records_from_session_items(
        items,
        session_id=session_id,
        external_session_id=external_session_id,
        cwd=current,
    )
    # Empty transcript → ``claude --resume`` exits fatally ("No conversation
    # found"), killing the terminal-as-agent. Return None so the caller
    # launches fresh instead of resuming nothing.
    if not records:
        return None
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = target.with_suffix(".jsonl.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        os.replace(tmp, target)
    except OSError as exc:
        raise click.ClickException(
            f"Failed to write Claude resume transcript {target}: {exc}"
        ) from exc
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    return target

def _claude_transcript_records_from_session_items(
    items: list[dict[str, Any]],
    *,
    session_id: str,
    external_session_id: str,
    cwd: Path,
) -> list[dict[str, Any]]:
    """
    Convert Omnigent session items into Claude Code transcript records.

    :param items: Flat Omnigent item dicts in chronological order, e.g.
        ``{"type": "message", "role": "user", "content": [...]}``.
    :param session_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``. Used as part of deterministic synthetic
        UUID generation.
    :param external_session_id: Claude-native session id, e.g.
        ``"02857840-6362-408f-b41f-309e396ed7c6"``.
    :param cwd: Working directory to write into each transcript
        record, e.g. ``Path("/home/me/repo")``.
    :returns: Claude JSONL record dictionaries.
    """
    records: list[dict[str, Any]] = []
    parent_uuid: str | None = None
    tool_parent_by_call_id: dict[str, str] = {}
    for index, item in enumerate(items):
        record_uuid = _synthetic_claude_transcript_uuid(
            session_id=session_id,
            external_session_id=external_session_id,
            item=item,
            index=index,
        )
        record = _claude_transcript_record_from_session_item(
            item,
            session_id=external_session_id,
            record_uuid=record_uuid,
            parent_uuid=tool_parent_by_call_id.get(str(item.get("call_id"))) or parent_uuid,
            cwd=cwd,
        )
        if record is None:
            continue
        records.append(record)
        if item.get("type") == "function_call":
            call_id = item.get("call_id")
            if isinstance(call_id, str) and call_id:
                tool_parent_by_call_id[call_id] = record_uuid
        parent_uuid = record_uuid
    return records

def _claude_transcript_record_from_session_item(
    item: dict[str, Any],
    *,
    session_id: str,
    record_uuid: str,
    parent_uuid: str | None,
    cwd: Path,
) -> dict[str, Any] | None:
    """
    Convert one Omnigent item into one Claude transcript record.

    :param item: Flat Omnigent item dict, e.g.
        ``{"type": "function_call", "name": "Read", ...}``.
    :param session_id: Claude-native session id for the transcript,
        e.g. ``"02857840-6362-408f-b41f-309e396ed7c6"``.
    :param record_uuid: Deterministic UUID for this synthetic
        transcript line.
    :param parent_uuid: Previous transcript record UUID, or ``None``
        for the first line.
    :param cwd: Current working directory to record, e.g.
        ``Path("/home/me/repo")``.
    :returns: Claude transcript record, or ``None`` for unsupported or
        empty Omnigent items.
    """
    item_type = item.get("type")
    message: dict[str, Any] | None = None
    record_type: str | None = None
    extra: dict[str, Any] = {}
    if item_type == "message":
        role = item.get("role")
        if role == "user":
            content = _claude_user_content_from_api_blocks(item.get("content"))
            if content is None:
                return None
            record_type = "user"
            message = {"role": "user", "content": content}
        elif role == "assistant":
            content = _claude_assistant_content_from_api_blocks(item.get("content"))
            if content is None:
                return None
            record_type = "assistant"
            message = {"role": "assistant", "content": content}
            model = item.get("model")
            if isinstance(model, str) and model:
                message["model"] = model
        else:
            return None
    elif item_type == "function_call":
        name = item.get("name")
        call_id = item.get("call_id")
        if not isinstance(name, str) or not name:
            return None
        if not isinstance(call_id, str) or not call_id:
            return None
        record_type = "assistant"
        message = {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": name,
                    "input": _json_object_from_string(item.get("arguments")),
                }
            ],
        }
        model = item.get("model")
        if isinstance(model, str) and model:
            message["model"] = model
    elif item_type == "function_call_output":
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return None
        output = item.get("output")
        if not isinstance(output, str):
            output = "" if output is None else json.dumps(output, separators=(",", ":"))
        record_type = "user"
        message = {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": output,
                }
            ],
        }
        extra["toolUseResult"] = output
    else:
        return None
    return {
        "type": record_type,
        "uuid": record_uuid,
        "parentUuid": parent_uuid,
        "isSidechain": False,
        "userType": "external",
        "sessionId": session_id,
        "cwd": str(cwd),
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "message": message,
        **extra,
    }

def _synthetic_claude_transcript_uuid(
    *,
    session_id: str,
    external_session_id: str,
    item: dict[str, Any],
    index: int,
) -> str:
    """
    Build a stable UUID for one synthesized transcript record.

    :param session_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param external_session_id: Claude-native session id, e.g.
        ``"02857840-6362-408f-b41f-309e396ed7c6"``.
    :param item: Omnigent item dict. ``id`` is used when present.
    :param index: Zero-based fallback index.
    :returns: UUID string, e.g.
        ``"d4ffea8e-87dc-5c7b-8f86-3dece5760a22"``.
    """
    item_id = item.get("id")
    stable_item_id = item_id if isinstance(item_id, str) and item_id else f"index-{index}"
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"omnigent-claude-resume:{session_id}:{external_session_id}:{stable_item_id}",
        )
    )

def _claude_user_content_from_api_blocks(content: object) -> str | list[dict[str, Any]] | None:
    """
    Convert Omnigent user message blocks into Claude message content.

    :param content: Omnigent ``content`` value, e.g.
        ``[{"type": "input_text", "text": "hello"}]``.
    :returns: A string for simple text prompts, a Claude content block
        list for multi-block prompts, or ``None`` when no text exists.
    """
    blocks = _claude_text_blocks_from_api_content(content, api_type="input_text")
    if not blocks:
        return None
    if len(blocks) == 1:
        return str(blocks[0]["text"])
    return blocks

def _claude_assistant_content_from_api_blocks(content: object) -> list[dict[str, Any]] | None:
    """
    Convert Omnigent assistant message blocks into Claude text blocks.

    :param content: Omnigent ``content`` value, e.g.
        ``[{"type": "output_text", "text": "hello"}]``.
    :returns: Claude ``text`` content blocks, or ``None`` when no
        assistant text exists.
    """
    blocks = _claude_text_blocks_from_api_content(content, api_type="output_text")
    return blocks or None

def _claude_text_blocks_from_api_content(
    content: object,
    *,
    api_type: str,
) -> list[dict[str, Any]]:
    """
    Extract text blocks from an Omnigent content array.

    :param content: Omnigent content array, e.g.
        ``[{"type": "input_text", "text": "hello"}]``.
    :param api_type: Omnigent block type to include, e.g.
        ``"input_text"`` or ``"output_text"``.
    :returns: Claude ``{"type": "text", "text": ...}`` blocks.
    """
    if not isinstance(content, list):
        return []
    blocks: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != api_type:
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            blocks.append({"type": "text", "text": text})
    return blocks

def _json_object_from_string(value: object) -> dict[str, Any]:
    """
    Parse a JSON object string, returning ``{}`` on non-object input.

    :param value: JSON string from an Omnigent function-call item, e.g.
        ``"{\"file_path\":\"README.md\"}"``.
    :returns: Parsed object suitable for a Claude ``tool_use.input``
        field.
    """
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _cold_resume as _sib_cold_resume
    from . import _config as _sib_config
    from . import _cwd as _sib_cwd
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
    from . import _local_server as _sib_local_server
    from . import _remote_server as _sib_remote_server
    from . import _resume_ui as _sib_resume_ui
    from . import _terminal as _sib_terminal
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
    for _key, _value in _sib_resume_ui.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_terminal.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_types.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
