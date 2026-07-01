"""Native Codex TUI wrapper for the Omnigent CLI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import click
import httpx
import yaml

from omnigent._native_resume_hint import echo_native_resume_hint
from omnigent._runner_startup import RunnerStartupProgress, runner_startup_progress
from omnigent._wrapper_labels import (
    CODEX_NATIVE_WRAPPER_VALUE as _WRAPPER_LABEL_VALUE,
)
from omnigent._wrapper_labels import WRAPPER_LABEL_KEY as _WRAPPER_LABEL_KEY
from omnigent.claude_native import (
    _attach_with_reconnect,
    attach_local_terminal,
)
from omnigent.claude_native_bridge import url_component
from omnigent.codex_native_app_server import (
    CodexAppServerClient,
    CodexNativeAppServer,
    build_codex_native_server,
    build_codex_remote_args,
    client_for_transport,
    codex_session_meta_model_provider,
    codex_terminal_env,
    preload_codex_thread_for_resume,
    resolve_native_codex_launch,
)
from omnigent.codex_native_bridge import (
    CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
    CodexNativeBridgeState,
    bridge_dir_for_bridge_id,
    clear_bridge_state,
    codex_home_for_bridge_dir,
    prepare_bridge_dir,
    read_bridge_state,
    socket_path_for_bridge_dir,
    write_bridge_state,
)
from omnigent.codex_native_forwarder import supervise_forwarder
from omnigent.codex_native_state import read_launch_state, write_launch_state
from omnigent.conversation_browser import conversation_url, open_conversation_link_if_enabled
from omnigent.entities.session_resources import terminal_resource_id
from omnigent.host.daemon_launch import (
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

def _find_codex_rollout(codex_home: Path, thread_id: str) -> Path | None:
    """
    Find a Codex rollout file by thread id within a ``CODEX_HOME``.

    Codex persists each thread's history as a single append-only JSONL
    rollout at
    ``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ISO-ts>-<thread_id>.jsonl``,
    where the trailing ``<thread_id>`` matches the thread's
    ``session_meta.id``. We locate it by that filename suffix.

    :param codex_home: A per-session private ``CODEX_HOME``, e.g.
        ``Path("~/.omnigent/codex-native/<hash>/codex-home")``.
    :param thread_id: Codex thread id / rollout stem, e.g.
        ``"019e96aa-0be2-7343-8d3b-6f914d60936b"``.
    :returns: Path to the most recent matching rollout, or ``None`` when
        none exists on this host.
    """
    if not _CODEX_THREAD_ID_RE.fullmatch(thread_id):
        return None
    sessions = codex_home / "sessions"
    if not sessions.is_dir():
        return None
    matches = [p for p in sessions.glob(f"**/rollout-*-{thread_id}.jsonl") if p.is_file()]
    if not matches:
        return None
    matches.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return matches[0]

def _copy_rollout_with_cwd(
    *, source: Path, target: Path, clone_workspace: Path, new_thread_id: str
) -> None:
    """
    Copy a Codex rollout JSONL, rewriting only the structural id/cwd.

    A rollout interleaves *structural* fields (the live thread settings
    Codex reads on resume) with *historical* content (recorded shell
    commands, file paths, messages — facts about what already happened).
    Only two structural fields carry the working directory —
    ``session_meta.payload.cwd`` and each ``turn_context.payload.cwd`` —
    plus the thread id at ``session_meta.payload.id``. Those are rewritten
    to the clone's id / workspace; every other line (and every other
    ``cwd`` mention, which lives inside message/tool bodies) is copied
    verbatim, so the clone's history stays truthful about the source run.

    :param source: Existing source rollout JSONL.
    :param target: Temporary output path (atomically renamed by the
        caller).
    :param clone_workspace: The resolved directory the clone runs in,
        written into the structural ``cwd`` fields, e.g.
        ``Path("/home/me/repo-worktrees/fork")``.
    :param new_thread_id: The clone's thread id, written into
        ``session_meta.payload.id``, e.g.
        ``"019e96aa-0be2-7343-8d3b-6f914d60936b"``.
    :returns: None.
    :raises click.ClickException: If a rollout line is not valid JSON.
    """
    workspace_text = str(clone_workspace)
    with source.open("r", encoding="utf-8") as src, target.open("w", encoding="utf-8") as dst:
        for line_number, line in enumerate(src, start=1):
            if not line.strip():
                dst.write(line)
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise click.ClickException(
                    f"Cannot clone malformed Codex rollout {source}: "
                    f"line {line_number} is not valid JSON."
                ) from exc
            record_type = record.get("type") if isinstance(record, dict) else None
            if record_type not in ("session_meta", "turn_context"):
                # Historical record — write the original bytes back unchanged
                # so message/tool bodies (and their incidental cwd mentions)
                # are preserved exactly, including whitespace and key order.
                dst.write(line)
                continue
            payload = record.get("payload")
            if isinstance(payload, dict):
                if record_type == "session_meta":
                    if isinstance(payload.get("id"), str):
                        payload["id"] = new_thread_id
                    if isinstance(payload.get("cwd"), str):
                        payload["cwd"] = workspace_text
                elif isinstance(payload.get("cwd"), str):  # turn_context
                    payload["cwd"] = workspace_text
            dst.write(json.dumps(record, separators=(",", ":")) + "\n")

def _clone_codex_rollout(
    *,
    source_session_id: str,
    source_thread_id: str,
    target_thread_id: str,
    clone_codex_home: Path,
    clone_workspace: Path,
) -> Path | None:
    """
    Clone a source Codex rollout into the clone's own ``CODEX_HOME``.

    Used to carry a forked codex-native session's history into the clone.
    Codex's resume reads the rollout from the app-server's ``CODEX_HOME``,
    which is per-session-private (keyed by the conversation id), so the
    source rollout must be copied into the *clone's* ``CODEX_HOME`` under a
    thread id we assign. We rewrite ``session_meta.payload.id`` →
    *target_thread_id* and the two structural ``cwd`` fields →
    *clone_workspace* (see :func:`_copy_rollout_with_cwd`), preserving the
    record order and all historical content. The clone then launches
    ``codex resume <target_thread_id>``. Writing the file ourselves before
    launch (rather than pointing resume at the source's home) is what makes
    the worktree case work and keeps the clone's history isolated from the
    source. This is the codex-native mirror of
    :func:`omnigent.claude_native._clone_claude_transcript`. See
    designs/FORK_SESSION_UX.md.

    :param source_session_id: The SOURCE conversation id, used to locate
        the source's ``CODEX_HOME``, e.g. ``"conv_abc123"``.
    :param source_thread_id: The SOURCE Codex thread id / rollout stem to
        copy from, e.g. ``"019e96aa-0be2-7343-8d3b-6f914d60936b"``.
    :param target_thread_id: The thread id to assign the clone's copied
        rollout, e.g. ``"019eaa11-...."``. Must be a safe rollout stem; the
        clone's ``external_session_id`` is set to this so a later relaunch
        resumes it via the normal path.
    :param clone_codex_home: The clone's per-session private ``CODEX_HOME``,
        e.g. ``Path("~/.omnigent/codex-native/<hash>/codex-home")``.
    :param clone_workspace: The resolved directory the clone will run in
        (its worktree or same dir). Written into the structural ``cwd``
        fields. Pass an already-resolved path.
    :returns: Path to the written clone rollout, or ``None`` when the ids
        are unsafe or the source rollout can't be found on this host
        (caller launches fresh in that case).
    :raises click.ClickException: If the source rollout is malformed.
    """
    if not _CODEX_THREAD_ID_RE.fullmatch(source_thread_id):
        return None
    if not _CODEX_THREAD_ID_RE.fullmatch(target_thread_id):
        return None
    source_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id(source_session_id))
    source = _find_codex_rollout(source_home, source_thread_id)
    if source is None:
        return None
    # Preserve the source's ``sessions/<YYYY>/<MM>/<DD>/`` layout; only swap
    # the thread id embedded in the rollout filename so the clone lands in
    # its own CODEX_HOME under the assigned id.
    rel_dir = source.parent.relative_to(source_home)
    target_dir = clone_codex_home / rel_dir
    target = target_dir / source.name.replace(source_thread_id, target_thread_id)
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = target.with_suffix(".jsonl.tmp")
    try:
        _copy_rollout_with_cwd(
            source=source,
            target=tmp,
            clone_workspace=clone_workspace,
            new_thread_id=target_thread_id,
        )
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    return target

async def _ensure_local_codex_resume_rollout(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    external_session_id: str,
    codex_home: Path,
    workspace: Path,
    model_provider: str,
    codex_path: str | None,
) -> Path:
    """
    Ensure Codex has a local rollout JSONL for cold resume.

    Cross-machine resume has the Omnigent conversation and Codex thread id on
    the server, but not necessarily the app-server's local
    ``$CODEX_HOME/sessions/.../rollout-*-<thread>.jsonl`` file. Codex
    ``resume <thread>`` reads that local rollout, so before launching a
    known-thread terminal we synthesize the rollout from committed AP
    items when the local rollout is missing. Existing local rollout files
    are left untouched because Codex treats them as append-only runtime
    state, not a cache that Omnigent should rewrite.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param external_session_id: Codex thread id, e.g.
        ``"019e96aa-0be2-7343-8d3b-6f914d60936b"``.
    :param codex_home: Per-session private ``CODEX_HOME`` whose
        ``sessions`` directory Codex app-server reads.
    :param workspace: Resolved directory Codex will run in, e.g.
        ``Path("/home/me/repo")``. Pass an already-resolved path so
        structural rollout cwd fields match the terminal cwd.
    :param model_provider: Provider id this session's launch routes through,
        e.g. ``"omnigent_databricks"`` (see
        :func:`omnigent.codex_native_app_server.codex_session_meta_model_provider`).
        Written into ``session_meta`` so codex's thread-store backfill can
        resolve the provider when it indexes the rollout.
    :param codex_path: Codex CLI executable used to stamp the real
        ``cli_version`` into ``session_meta``, e.g. ``"/usr/local/bin/codex"``.
        ``None`` (or an unparseable version probe) falls back to ``"0.0.0"`` —
        codex >= 0.133 requires the field to be *present* to parse the
        rollout, but treats the value as informational, so a flaky probe
        must not cost the carried history.
    :returns: Path to the existing or written rollout.
    :raises click.ClickException: If Omnigent history cannot be fetched or the
        rollout cannot be written, or if the persisted Codex thread id is
        unsafe for use in a rollout filename.
    """
    if not _CODEX_THREAD_ID_RE.fullmatch(external_session_id):
        raise click.ClickException(
            f"Cannot resume Codex session {session_id!r}: persisted thread id "
            f"{external_session_id!r} is not a safe Codex rollout id."
        )
    existing = _find_codex_rollout(codex_home, external_session_id)
    if existing is not None:
        return existing
    target = _codex_resume_rollout_path(codex_home, external_session_id)
    items = await _fetch_all_session_items_for_codex_resume(client, session_id)
    cli_version = None
    if codex_path is not None:
        from omnigent.inner.codex_executor import _codex_cli_version

        version_tuple = await _codex_cli_version(codex_path)
        if version_tuple is not None:
            cli_version = ".".join(str(part) for part in version_tuple)
    records = _codex_rollout_records_from_session_items(
        items,
        session_id=session_id,
        external_session_id=external_session_id,
        cwd=workspace,
        model_provider=model_provider,
        cli_version=cli_version or "0.0.0",
    )
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = target.with_suffix(".jsonl.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        os.replace(tmp, target)
    except OSError as exc:
        raise click.ClickException(
            f"Failed to write Codex resume rollout {target}: {exc}"
        ) from exc
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    return target

def _codex_resume_rollout_path(codex_home: Path, external_session_id: str) -> Path:
    """
    Return the rollout path to write for a Codex cold resume.

    Reuses the most recent existing rollout for the thread when present,
    otherwise creates a date-partitioned path matching Codex's on-disk
    layout.

    :param codex_home: Per-session private ``CODEX_HOME``, e.g.
        ``Path("~/.omnigent/codex-native/x/codex-home")``.
    :param external_session_id: Codex thread id / rollout stem, e.g.
        ``"019e96aa-0be2-7343-8d3b-6f914d60936b"``.
    :returns: Rollout JSONL path to overwrite or create.
    """
    existing = _find_codex_rollout(codex_home, external_session_id)
    if existing is not None:
        return existing
    now = datetime.now(timezone.utc)
    partition = (
        codex_home / "sessions" / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
    )
    stamp = now.strftime("%Y-%m-%dT%H-%M-%S")
    return partition / f"rollout-{stamp}-{external_session_id}.jsonl"

def _codex_rollout_records_from_session_items(
    items: list[dict[str, Any]],
    *,
    session_id: str,
    external_session_id: str,
    cwd: Path,
    model_provider: str,
    cli_version: str,
) -> list[dict[str, Any]]:
    """
    Convert Omnigent session items into Codex rollout JSONL records.

    The generated records follow Codex's rollout shape: one
    ``session_meta`` record, a ``turn_context`` before each Omnigent response
    group, Responses-style ``response_item`` payloads for user, assistant,
    and tool history, and an ``event_msg`` mirror after each user/assistant
    message. All three session_meta extras and the event_msg mirrors are
    load-bearing on codex >= 0.133 (verified against 0.136.0): a
    ``session_meta`` without ``timestamp`` + ``cli_version`` fails rollout
    parse ("does not start with session metadata"), an absent
    ``model_provider`` breaks ``thread/resume`` config load once the
    thread-store backfill indexes the rollout, and without ``event_msg``
    records codex reconstructs zero visible turns — the resume "succeeds"
    but the thread opens empty.

    :param items: Flat Omnigent item dicts in chronological order, e.g.
        ``{"type": "message", "role": "user", "content": [...]}``.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
        Used for deterministic synthetic turn ids.
    :param external_session_id: Codex thread id, e.g.
        ``"019e96aa-0be2-7343-8d3b-6f914d60936b"``.
    :param cwd: Working directory to write into structural rollout fields,
        e.g. ``Path("/home/me/repo")``.
    :param model_provider: Provider id for ``session_meta.model_provider``,
        e.g. ``"omnigent_databricks"``.
    :param cli_version: Codex CLI version string for
        ``session_meta.cli_version``, e.g. ``"0.136.0"``.
    :returns: Codex rollout record dictionaries.
    """
    timestamp = _codex_rollout_timestamp()
    records: list[dict[str, Any]] = [
        {
            "timestamp": timestamp,
            "type": "session_meta",
            "payload": {
                "id": external_session_id,
                "timestamp": timestamp,
                "cwd": str(cwd),
                "originator": "omnigent",
                "cli_version": cli_version,
                "model_provider": model_provider,
            },
        }
    ]
    seen_turn_ids: set[str] = set()
    interrupted_response_ids = _interrupted_response_ids_from_session_items(items)
    for index, item in enumerate(items):
        if _session_item_response_id(item) in interrupted_response_ids:
            continue
        payload = _codex_response_item_from_session_item(item)
        if payload is None:
            continue
        turn_id = _codex_turn_id_for_session_item(
            session_id=session_id,
            external_session_id=external_session_id,
            item=item,
            index=index,
        )
        if turn_id not in seen_turn_ids:
            records.append(
                {
                    "timestamp": timestamp,
                    "type": "turn_context",
                    "payload": {
                        "turn_id": turn_id,
                        "cwd": str(cwd),
                        "approval_policy": "on-request",
                    },
                }
            )
            seen_turn_ids.add(turn_id)
        records.append(
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": payload,
            }
        )
        event_msg = _codex_event_msg_record_for_message(payload, timestamp=timestamp)
        if event_msg is not None:
            records.append(event_msg)
    return records

def _codex_event_msg_record_for_message(
    payload: dict[str, Any],
    *,
    timestamp: str,
) -> dict[str, Any] | None:
    """
    Build the ``event_msg`` mirror record for a message ``response_item``.

    Codex reconstructs a resumed thread's *visible* turns from ``event_msg``
    records (``user_message`` / ``agent_message``), not from the
    ``response_item`` history that feeds the model context. A synthesized
    rollout without these mirrors resumes "successfully" but renders an
    empty thread (zero turns) on codex 0.136.0, so the carried history is
    invisible in the TUI and web UI.

    :param payload: A ``response_item`` payload already emitted into the
        rollout, e.g. ``{"type": "message", "role": "user", "content": [...]}``.
    :param timestamp: Rollout record timestamp, e.g.
        ``"2026-06-12T08:00:00.000Z"``.
    :returns: An ``event_msg`` record for user/assistant messages, or
        ``None`` for tool-call payloads (codex shows those via dedicated
        event types that are not needed for turn reconstruction).
    """
    if payload.get("type") != "message":
        return None
    text = " ".join(
        block.get("text", "") for block in payload.get("content", []) if isinstance(block, dict)
    ).strip()
    if not text:
        return None
    role = payload.get("role")
    if role == "user":
        event_payload: dict[str, Any] = {
            "type": "user_message",
            "message": text,
            "images": [],
            "local_images": [],
            "text_elements": [],
        }
    elif role == "assistant":
        event_payload = {
            "type": "agent_message",
            "message": text,
            "phase": "final_answer",
            "memory_citation": None,
        }
    else:
        return None
    return {"timestamp": timestamp, "type": "event_msg", "payload": event_payload}

def _interrupted_response_ids_from_session_items(items: list[dict[str, Any]]) -> set[str]:
    """
    Return response ids for Omnigent turns that ended interrupted.

    A Codex interrupted turn is persisted in Omnigent as visible transcript text
    plus an ``interrupted`` assistant marker. For native resume, the whole
    response group must be skipped so Codex does not restore the cancelled
    user request, partial assistant answer, or any partial tool history.

    :param items: Flat Omnigent item dicts in chronological order, e.g.
        ``[{"response_id": "codex_turn_123", "interrupted": True}]``.
    :returns: Response ids to exclude from synthesized Codex rollout
        history, e.g. ``{"codex_turn_123"}``.
    """
    response_ids: set[str] = set()
    for item in items:
        if not _is_interrupted_assistant_session_item(item):
            continue
        response_id = _session_item_response_id(item)
        if response_id is not None:
            response_ids.add(response_id)
    return response_ids

def _codex_response_item_from_session_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """
    Convert one Omnigent item into one Codex ``response_item`` payload.

    :param item: Flat Omnigent item dict, e.g.
        ``{"type": "function_call", "name": "shell", ...}``.
    :returns: Responses-style item payload, or ``None`` for unsupported
        or empty Omnigent items.
    """
    payload = _codex_response_item_payload(item)
    if payload is None:
        return None
    item_id = item.get("id")
    if isinstance(item_id, str) and item_id:
        payload["id"] = item_id
    return payload

def _codex_response_item_payload(item: dict[str, Any]) -> dict[str, Any] | None:
    """
    Convert one supported Omnigent item into a Codex response payload body.

    :param item: Flat Omnigent item dict.
    :returns: Payload without the optional item id, or ``None`` for
        unsupported / empty Omnigent items.
    """
    item_type = item.get("type")
    if item_type == "message":
        return _codex_message_payload_from_session_item(item)
    if item_type == "function_call":
        return _codex_function_call_payload_from_session_item(item)
    if item_type == "function_call_output":
        return _codex_function_call_output_payload_from_session_item(item)
    return None

def _codex_message_payload_from_session_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """
    Convert an Omnigent message item into a Codex message payload.

    :param item: Omnigent message item.
    :returns: Codex message payload, or ``None`` for unsupported roles
        or empty text content.
    """
    role = item.get("role")
    if role == "user":
        api_type = "input_text"
    elif role == "assistant":
        api_type = "output_text"
    else:
        return None
    content = _codex_content_blocks_from_api_content(item.get("content"), api_type=api_type)
    if not content:
        return None
    return {"type": "message", "role": role, "content": content}

def _codex_function_call_payload_from_session_item(
    item: dict[str, Any],
) -> dict[str, Any] | None:
    """
    Convert an Omnigent function call item into a Codex function call payload.

    :param item: Omnigent function call item.
    :returns: Codex function call payload, or ``None`` when optional
        routing fields are absent.
    :raises click.ClickException: If the Omnigent item violates required tool
        history fields.
    """
    name = item.get("name")
    call_id = item.get("call_id")
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(call_id, str) or not call_id:
        return None
    arguments = item.get("arguments")
    if not isinstance(arguments, str):
        item_id = item.get("id")
        raise click.ClickException(
            "Cannot synthesize Codex resume rollout: Omnigent function_call "
            f"{item_id!r} has non-string arguments."
        )
    return {
        "type": "function_call",
        "name": name,
        "arguments": arguments,
        "call_id": call_id,
    }

def _codex_function_call_output_payload_from_session_item(
    item: dict[str, Any],
) -> dict[str, Any] | None:
    """
    Convert an Omnigent function output item into a Codex function output payload.

    :param item: Omnigent function output item.
    :returns: Codex function output payload, or ``None`` when optional
        routing fields are absent.
    :raises click.ClickException: If the Omnigent item violates required tool
        output fields.
    """
    call_id = item.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return None
    output = item.get("output")
    if not isinstance(output, str):
        item_id = item.get("id")
        raise click.ClickException(
            "Cannot synthesize Codex resume rollout: Omnigent function_call_output "
            f"{item_id!r} has non-string output."
        )
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": output,
    }

def _codex_content_blocks_from_api_content(
    content: object,
    *,
    api_type: str,
) -> list[dict[str, Any]]:
    """
    Extract text blocks from an Omnigent content array for Codex rollout items.

    :param content: Omnigent content array, e.g.
        ``[{"type": "input_text", "text": "hello"}]``.
    :param api_type: Omnigent block type to include, e.g.
        ``"input_text"`` or ``"output_text"``.
    :returns: Codex/OpenAI content blocks preserving *api_type*.
    """
    if not isinstance(content, list):
        return []
    blocks: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != api_type:
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            blocks.append({"type": api_type, "text": text})
    return blocks

def _codex_turn_id_for_session_item(
    *,
    session_id: str,
    external_session_id: str,
    item: dict[str, Any],
    index: int,
) -> str:
    """
    Return a Codex turn id for an Omnigent item.

    Codex-native forwarder stores Omnigent ``response_id`` as
    ``"codex_<turn_id>"`` for mirrored items. When that prefix is not
    present, build a deterministic synthetic turn id from stable inputs.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param external_session_id: Codex thread id, e.g.
        ``"019e96aa-0be2-7343-8d3b-6f914d60936b"``.
    :param item: Flat Omnigent item dict.
    :param index: Zero-based fallback item index.
    :returns: Codex turn id, e.g. ``"turn_abc123"``.
    """
    response_id = item.get("response_id")
    if isinstance(response_id, str) and response_id.startswith("codex_"):
        turn_id = response_id.removeprefix("codex_")
        if turn_id:
            return turn_id
    stable = item.get("response_id") or item.get("id") or f"index-{index}"
    return (
        "turn_"
        + uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"omnigent-codex-resume:{session_id}:{external_session_id}:{stable}",
        ).hex
    )

def _codex_rollout_timestamp() -> str:
    """
    Return a UTC timestamp string for synthesized Codex rollout records.

    :returns: ISO-8601 timestamp with ``Z`` suffix, e.g.
        ``"2026-06-08T12:34:56.789Z"``.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
    from . import _local_server as _sib_local_server
    from . import _remote_server as _sib_remote_server
    from . import _resume_ui as _sib_resume_ui
    from . import _session_items as _sib_session_items
    from . import _terminal as _sib_terminal
    from . import _types as _sib_types
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
    for _key, _value in _sib_session_items.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_terminal.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_types.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
