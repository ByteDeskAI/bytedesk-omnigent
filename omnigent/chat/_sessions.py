"""Implementation of the ``omnigent chat`` command.

The CLI always ends by connecting an Omnigent client to a server URL. For
path targets it first ensures the agent is registered on that server
(a local subprocess by default, or ``--server`` when supplied). URL
targets skip setup and use the existing server's registered agents.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeAlias

import click
import httpx
import yaml
from omnigent_client import (
    OmnigentClient,
    SessionToolCallInfo,
    ToolCallable,
    ToolCallInfo,
    ToolHandler,
)
from omnigent_client import (
    OmnigentError as ClientOmnigentError,
)
from omnigent_client._events import (
    ErrorEvent,
    ResponseCancelled,
    ResponseCompleted,
    ResponseFailed,
    ResponseIncomplete,
    TextDelta,
)
from rich.console import Console

from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE as _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE,
)
from omnigent._wrapper_labels import (
    WRAPPER_LABEL_KEY as _CLAUDE_NATIVE_WRAPPER_LABEL_KEY,
)
from omnigent.conversation_browser import open_conversation_link_if_enabled
from omnigent.errors import OmnigentError
from omnigent.harness_aliases import canonicalize_harness
from omnigent.inner.databricks_executor import _DatabricksBearerAuth, _read_databrickscfg
from omnigent.native_coding_agents import native_coding_agent_for_wrapper_label
from omnigent.spec import load as load_spec
from omnigent.spec._omnigent_compat import OMNIGENT_EXECUTOR_TYPE
from omnigent.spec.parser import discover_host_skills
from omnigent.spec.types import AgentSpec, SkillSpec

if TYPE_CHECKING:
    from omnigent._runner_startup import RunnerStartupProgress

console = Console()

# YAML mapping shape — heterogeneous JSON-shaped values
# (strings, ints, lists, nested dicts) so ``Any`` is the
# narrowest safe element type. Used as the parsed-spec
# return / input shape across this module's helpers.
_YamlMapping: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

logger = logging.getLogger(__name__)

# Local server readiness polling: use a short initial interval so
# freshly-launched ``omnigent run`` sessions don't burn a
# fixed 500 ms before noticing the server is ready, then back off
# slightly while still remaining responsive on slower cold starts.
_SERVER_READY_INITIAL_POLL_SECONDS = 0.05
_SERVER_READY_BACKOFF_POLL_SECONDS = 0.1
_SERVER_READY_FAST_POLL_WINDOW_SECONDS = 1.0

# Remote ``--server`` runners are disposable subprocesses created for
# the CLI session. A one-second grace gives SIGTERM enough time to
# flush runner logs and unregister without noticeably slowing CLI exit.
# Grace period before the CLI escalates SIGTERM → SIGKILL on the
# runner subprocess. Must be long enough for the runner's shutdown
# chain to complete: cancel async tasks → app.router.shutdown() →
# _stop_pm() → _terminal_registry.shutdown() → tmux kill-server
# per session → pm.shutdown() → SIGTERM each harness. 1 s was too
# short — the runner was SIGKILL'd before tmux sessions were reaped,
# leaving zombie codex/claude processes.
_REMOTE_RUNNER_STOP_GRACE_SECONDS = 8.0

# Fallback model when the YAML declares neither ``executor.model``
# nor ``executor.harness`` AND no ``--model`` / ``--harness``
# override is supplied. Mirrors the legacy argparse CLI's
# ``_DEFAULT_AD_HOC_MODEL`` so ``omnigent run examples/hello_world.yaml``
# (a spec with no executor block) launches cleanly instead of
# failing the strict omnigent validator with a cryptic
# "executor.config.harness: required" error.
_DEFAULT_AD_HOC_MODEL = "databricks-gpt-5-4"

# How many of the NEWEST transcript items ``_persisted_turn_text``
# fetches when reconciling a headless ``-p`` turn against the durable
# store. The current turn's items are always the newest, and no single
# one-shot turn emits anywhere near this many items, so the latest turn
# is fully captured regardless of how long a resumed session's history
# is. Fetched ``order="desc"`` (newest first) precisely so the window
# tracks the end of the conversation, not its start.
_RECONCILE_ITEMS_LIMIT = 100

# Optional bearer token for remote omnigent servers that sit
# behind an auth proxy (for example Databricks Apps). When set, the
# CLI sends ``Authorization: Bearer <value>`` on every HTTP request it
# makes to the remote server.
_REMOTE_AUTH_TOKEN_ENV = "OMNIGENT_REMOTE_AUTH_TOKEN"

# Env-var override name. ``OMNIGENT_MODEL=foo`` lets a user
# pin a default model per shell session without needing to pass
# ``--model foo`` on every invocation. Resolved once at spec
# materialization time (not at runtime), so the materialized
# bundle stays self-contained — identical behavior on any host
# that runs the bundle, regardless of that host's env. Mirrors
# the legacy ``_default_cli_model`` at
# ``omnigent/inner/cli.py:344``.
_OMNIGENT_MODEL_ENV_VAR = "OMNIGENT_MODEL"
_OPENAI_API_KEY_ENV_VAR = "OPENAI_API_KEY"
_OPENAI_BASE_URL_ENV_VAR = "OPENAI_BASE_URL"
_OPENAI_AGENTS_HARNESSES = frozenset({"openai-agents", "openai-agents-sdk"})
_MATERIALIZED_OVERRIDE_DIRS: dict[Path, Path] = {}


def _import_package_bindings() -> None:
    from . import _constants as _pkg_constants
    from . import _state as _pkg_state
    g = globals()
    for _mod in (_pkg_constants, _pkg_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_package_bindings()

def _attach_session_info(
    *,
    base_url: str,
    conversation_id: str,
) -> _AttachSessionInfo:
    """
    Read the facts ``attach`` needs from one ``GET /v1/sessions/{id}``.

    ``attach`` dispatches co-drive turns to the host's already-bound runner
    (never launching one), so it only needs to know the runner is live plus
    the agent name + harness for an honest banner. ``runner_online`` is
    ``True`` only when a ``runner_id`` is present and the session snapshot
    does not report it offline. When ``runner_online`` is absent (older
    servers), attach stays optimistic and lets turn dispatch surface any real
    liveness failure; probing ``/v1/runners/{id}/status`` would be wrong here
    because that endpoint intentionally reports other users' runners as
    offline, while attach must support cross-user co-drive on shared sessions.
    A missing/unreachable session yields all-empty facts and the caller fails
    loud.

    :param base_url: Omnigent server base URL, e.g. ``"http://127.0.0.1:6767"``.
    :param conversation_id: Conversation/session id, e.g. ``"conv_abc123"``.
    :returns: The session facts; ``runner_online=False`` on any failure.
    """
    empty = _AttachSessionInfo(runner_online=False, agent_name=None, harness=None)
    try:
        resp = httpx.get(
            f"{base_url}/v1/sessions/{conversation_id}",
            headers=_remote_headers(server_url=base_url),
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        logger.warning("session probe failed for %s on %s: %s", conversation_id, base_url, exc)
        return empty
    if resp.status_code != 200:
        return empty
    try:
        body = resp.json()
    except ValueError:
        return empty
    if not isinstance(body, dict):
        return empty
    runner_id = body.get("runner_id")
    snapshot_online = body.get("runner_online")
    if not isinstance(runner_id, str) or not runner_id:
        runner_online = False
    elif isinstance(snapshot_online, bool):
        runner_online = snapshot_online
    else:
        # Older servers omit runner_online on the single-session snapshot. Stay
        # optimistic rather than falling back to the owner-scoped runner-status
        # endpoint, which reports a teammate's live runner as offline by design.
        runner_online = True
    agent_name = body.get("agent_name")
    harness = body.get("harness")
    return _AttachSessionInfo(
        runner_online=runner_online,
        agent_name=agent_name if isinstance(agent_name, str) and agent_name else None,
        harness=harness if isinstance(harness, str) and harness else None,
    )

def _pick_agent(base_url: str, *, quiet: bool = False) -> str:
    """
    Discover agent names from existing sessions and let the user pick.

    If only one agent name is found, selects it automatically.
    Falls back to requiring the user to specify ``--agent`` if no
    sessions exist yet.

    :param base_url: Server base URL.
    :param quiet: When ``True``, suppress interactive prompts and
        auto-select the first available agent.
    :returns: The chosen agent name.
    :raises click.ClickException: If no sessions exist or no
        agent name can be discovered.
    """
    resp = httpx.get(
        f"{base_url}/v1/sessions",
        headers=_remote_headers(server_url=base_url),
        params={"limit": 100},
        timeout=10.0,
    )
    resp.raise_for_status()
    sessions = resp.json()["data"]

    # Collect unique agent names from sessions.
    names: list[str] = []
    seen: set[str] = set()
    for s in sessions:
        name = s.get("agent_name")
        if name and name not in seen:
            names.append(name)
            seen.add(name)

    if not names:
        raise click.ClickException(
            "No sessions found on the server. Start a session first "
            "or specify the agent with --agent."
        )

    if len(names) == 1:
        if not quiet:
            click.echo(f"\n  Agent: {names[0]}")
        return names[0]

    click.echo("\n  Available agents:\n")
    for i, name in enumerate(names, 1):
        click.echo(f"    {i}. {name}")

    while True:
        raw = str(click.prompt("\n  Agent", default="1"))
        try:
            choice = int(raw)
            if 1 <= choice <= len(names):
                return names[choice - 1]
        except ValueError:
            if raw.strip() in seen:
                return raw.strip()
        click.echo(f"  Enter a number between 1 and {len(names)}.")

async def _query_sessions_once(
    *,
    client: OmnigentClient,
    agent_name: str,
    tool_handler: ToolHandler | None,
    prompt: str,
    session_bundle: bytes,
    session_bundle_filename: str,
    runner_id: str | None,
    resume_conversation_id: str | None = None,
    on_session_ready: Callable[[str], None] | None = None,
) -> str | None:
    """
    Create, bind, and query a sessions-API session for headless ``-p``.

    :param client: Connected SDK client.
    :param agent_name: Agent display name, e.g. ``"hello_world"``.
        Used only for tool-handler validation messages.
    :param tool_handler: Optional client-side tool handler.
    :param prompt: User prompt for the single turn.
    :param session_bundle: Gzipped agent tarball bytes.
    :param session_bundle_filename: Multipart filename, e.g.
        ``"agent.tar.gz"``.
    :param runner_id: Registered runner id, e.g.
        ``"runner_0123456789abcdef"``.
    :param resume_conversation_id: When set, resumes an existing
        session instead of creating a new one, e.g.
        ``"conv_abc123"``. ``None`` creates a fresh session.
    :param on_session_ready: Optional callback invoked after the
        session has been created/resumed and bound to the runner.
    :returns: Final assistant text, or ``None`` when no text was
        emitted.
    :raises RuntimeError: If no runner id was supplied.
    """
    from omnigent_client import SessionsChat

    if runner_id is None:
        raise RuntimeError(
            "Sessions API headless prompt requires a registered runner id. "
            "Start through `omnigent run <agent>` or pass --server so the CLI "
            "can launch and bind a runner."
        )
    tool_callables = _sessions_tool_callables(tool_handler, agent_name)
    if resume_conversation_id is not None:
        bound = await client.sessions.get(resume_conversation_id)
        await client.sessions.bind_runner(resume_conversation_id, runner_id=runner_id)
    else:
        created = await client.sessions.create(
            session_bundle,
            filename=session_bundle_filename,
            # Record CLI cwd so the Web UI can show "ran locally
            # in <workspace>" for one-shot sessions. CLI sessions
            # don't set host_id; this column is purely informational.
            workspace=os.getcwd(),
        )
        bound = await client.sessions.bind_runner(created.id, runner_id=runner_id)
    if on_session_ready is not None:
        on_session_ready(bound.id)
    session_files = client.files.for_session(bound.id)
    chat = SessionsChat(
        namespace=client.sessions,
        files_uploader=session_files.upload,
        files_getter=session_files.get,
        session=bound,
        tool_callables=tool_callables,
        agent_tools_getter=client._fetch_agent_tools,
    )
    del agent_name
    # A transport-level runner disconnect publishes ``session.status:
    # failed`` for every session pinned to that runner (server
    # ``_on_runner_disconnect``), even when the turn already completed
    # and its assistant response was persisted. ``SessionsChat.send``
    # raises ``OmnigentError`` on that ``failed`` status, and the
    # no-replay SSE subscription can additionally miss the terminal
    # ``response.completed`` event (subscribe-after-post race), leaving
    # the collected text empty. In both cases the runner has still
    # persisted the assistant message server-side, so reconcile against
    # the transcript before surfacing a failure — only a turn that
    # produced no output is a genuine error worth raising. The
    # interactive REPL is immune by construction (it renders a ``failed``
    # status as a transient error and polls the snapshot as a backstop),
    # so this brings headless ``-p`` to parity.
    try:
        result = await chat.query(prompt)
    except ClientOmnigentError:
        reconciled = await _persisted_turn_text(client, bound.id)
        if reconciled is not None:
            return reconciled
        raise
    if result.text:
        return result.text
    reconciled = await _persisted_turn_text(client, bound.id)
    if reconciled is not None:
        return reconciled
    # No assistant text for this turn. If the runner persisted a terminal
    # ``error`` item (e.g. a harness start failure like the cursor SDK's
    # invalid-model rejection), surface it instead of returning ``None`` —
    # otherwise the headless caller renders a failed turn as a silent,
    # exit-0 empty success. The callers wrap this in ``except
    # ClientOmnigentError`` and print the message to stderr + exit non-zero.
    turn_error = await _persisted_turn_error(client, bound.id)
    if turn_error is not None:
        raise ClientOmnigentError(turn_error)
    return None

def _sessions_tool_callables(
    tool_handler: ToolHandler | None,
    agent_name: str,
) -> dict[str, ToolCallable] | None:
    """
    Convert a legacy tool handler into sessions-API callables.

    :param tool_handler: Optional legacy client-side tool handler.
    :param agent_name: Agent display name for legacy tool context,
        e.g. ``"coding_supervisor"``.
    :returns: Mapping from declared tool name to callable, or
        ``None`` when no handler is configured.
    """
    if tool_handler is None:
        return None
    adapter = _SessionToolAdapter(tool_handler=tool_handler, agent_name=agent_name)
    callables: dict[str, ToolCallable] = {}
    for schema in tool_handler.schemas:
        raw_name = schema.get("name")
        if not isinstance(raw_name, str):
            continue
        callables[raw_name] = adapter
    return callables

def _response_output_text(output: _ResponseOutput) -> str | None:
    """Extract assistant text from an Omnigent response ``output`` list."""
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"output_text", "text"}:
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts) if parts else None

async def _persisted_turn_text(
    client: OmnigentClient,
    session_id: str,
) -> str | None:
    """
    Read the latest turn's persisted assistant text from a session.

    The headless ``-p`` path consumes a turn over a single no-replay
    SSE subscription. Two failure modes leave that subscription without
    the turn's text even though the runner persisted an assistant
    response server-side:

    * A transport-level runner disconnect publishes
      ``session.status: failed`` for the session (server
      ``_on_runner_disconnect``) after the turn completed;
      :meth:`SessionsChat.send` raises ``OmnigentError`` on it.
    * The subscriber misses the terminal ``response.completed`` event
      (subscribe-after-post race), so the collected text is empty.

    This reconciles against the durable transcript via
    ``GET /v1/sessions/{id}/items``. It anchors on the most recent
    user message and returns the concatenated ``output_text`` of every
    ``completed`` assistant message that follows it, so a resumed
    session's earlier-turn output is never mistaken for the current
    turn. Only ``completed`` assistant items count: a turn that truly
    errored mid-stream persists a non-``completed`` partial item, and
    masking that as success would swallow a genuine failure — whereas
    the target bug (a completed turn flipped to ``failed`` by a
    transport disconnect) always persists a ``completed`` item.

    :param client: Connected SDK client bound to the session's server.
    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :returns: The current turn's assistant text, or ``None`` when this
        turn persisted no ``completed`` assistant text (a genuine
        failure the caller should surface).
    """
    try:
        # ``order="desc"`` (newest first) so the window tracks the END
        # of the transcript — the current turn — not its start. A long
        # resumed session can have far more than the limit of history;
        # fetching ``asc`` would return the oldest items and miss this
        # turn entirely.
        recent: _ResponseOutput = await client.sessions.list_items(
            session_id, limit=_RECONCILE_ITEMS_LIMIT, order="desc"
        )
    except ClientOmnigentError as exc:
        # The reconcile read is itself best-effort: if the items
        # endpoint is unreachable, fall back to the original outcome
        # (the caller re-raises the turn error or prints nothing). Log
        # for observability rather than swallowing silently.
        logger.debug("reconcile transcript read failed for %s: %r", session_id, exc)
        return None
    # Walk newest → oldest, collecting ``completed`` assistant messages
    # until the current turn's user message is reached. This isolates
    # THIS turn's output: a prior turn's assistant text sits on the far
    # side of the current user message and is never collected.
    this_turn_assistant: _ResponseOutput = []
    for item in recent:
        if item.get("type") != "message":
            continue
        role = item.get("role")
        if role == "user":
            break  # reached the start of the current turn
        if role == "assistant" and item.get("status") == "completed":
            this_turn_assistant.append(item)
    # Restore chronological order so multi-message output joins correctly.
    this_turn_assistant.reverse()
    return _response_output_text(this_turn_assistant)

async def _persisted_turn_error(
    client: OmnigentClient,
    session_id: str,
) -> str | None:
    """Read the latest turn's persisted terminal error message, if any.

    Companion to :func:`_persisted_turn_text`. When a turn produced no
    ``completed`` assistant text, the runner may still have persisted a
    terminal ``error`` item — e.g. a harness *start* failure such as the
    cursor SDK rejecting an unknown model. Without this, the headless ``-p``
    path renders that as a silent, exit-0 empty success; returning the message
    lets the caller surface it and exit non-zero.

    Mirrors :func:`_persisted_turn_text`'s walk: newest → oldest, stopping at
    the current turn's user message, so a prior turn's error is never
    attributed to this turn.

    :param client: Connected SDK client bound to the session's server.
    :param session_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
    :returns: The current turn's terminal error message, or ``None``.
    """
    try:
        recent: _ResponseOutput = await client.sessions.list_items(
            session_id, limit=_RECONCILE_ITEMS_LIMIT, order="desc"
        )
    except ClientOmnigentError as exc:
        logger.debug("reconcile error read failed for %s: %r", session_id, exc)
        return None
    for item in recent:
        if item.get("type") == "message" and item.get("role") == "user":
            break  # reached the start of the current turn
        if item.get("type") == "error":
            message = item.get("message")
            if isinstance(message, str) and message:
                return message
    return None

def _resolve_resume_target(
    *,
    base_url: str,
    agent_name: str,
    resume_conversation_id: str | None,
    resume_latest: bool,
    resume_picker: bool = False,
    headers: dict[str, str] | None = None,
) -> str | None:
    """
    Decide which conversation the REPL should resume from.

    Doing this here (vs. inside ``run_repl``) gives a clean
    fail-fast when ``--continue`` finds no prior conversation:
    raise ``ClickException`` before the REPL renders anything,
    matching the native shape at
    ``omnigent/inner/cli.py:3082-3084`` ("No saved sessions
    to continue.").

    Precedence (highest to lowest):

    1. ``resume_conversation_id`` (``--resume <id>``) —
       explicit pin always wins.
    2. ``resume_picker`` (``--resume`` / ``-r`` with no value) —
       interactive picker. Returns ``None`` when the user cancels
       (treated as "start fresh"); raises when no conversations
       exist.
    3. ``resume_latest`` (``--continue`` / ``-c``) —
       silent auto-pick of the newest. Raises when no prior.
    4. None of the above — fresh conversation.

    :param base_url: Server base URL the SDK should target for
        the lookup, e.g. ``"http://127.0.0.1:9123"`` or
        ``"https://example.databricksapps.com"``.
    :param agent_name: The agent's registered name from the
        YAML's ``name:`` field.
    :param resume_conversation_id: An explicit
        ``--resume <id>``.
    :param resume_latest: ``True`` when ``--continue`` was
        passed.
    :param resume_picker: ``True`` when bare ``--resume`` was
        passed. Runs the interactive picker on the agent's
        conversations and returns the user's choice. ``None``
        return means user cancelled — caller should treat as
        "start fresh" rather than a hard error.
    :param headers: Optional auth headers for the server,
        e.g. ``{"Authorization": "Bearer <token>"}``. Required
        for remote servers; ``None`` for localhost.
    :returns: The conversation_id to attach to, or ``None``
        when no resumption flag matched (or the picker was
        cancelled).
    :raises click.ClickException: When ``--continue`` /
        ``--resume`` was requested but the agent has no prior
        conversation.
    """
    if resume_conversation_id is not None:
        # Fail loud on a bogus id instead of booting the REPL, swallowing
        # the failed attach, and silently starting fresh (which loses the
        # thread the user meant to resume). Mirrors the --continue path.
        _assert_resume_conversation_exists(
            base_url=base_url,
            conversation_id=resume_conversation_id,
            headers=headers,
        )
        return resume_conversation_id
    if resume_picker:
        # ``None`` from the picker is a clean cancel — pass it
        # through so the REPL opens fresh. The empty-list case is
        # raised explicitly inside ``_run_picker`` so it lands as
        # a ClickException with a parity message.
        return _run_picker(
            base_url=base_url,
            agent_name=agent_name,
            headers=headers,
        )
    if not resume_latest:
        return None
    resolved = _resolve_latest_conversation_id(
        base_url=base_url,
        agent_name=agent_name,
        headers=headers,
    )
    if resolved is None:
        raise click.ClickException(f"No prior conversation for agent {agent_name!r}.")
    return resolved

def _assert_resume_conversation_exists(
    *,
    base_url: str,
    conversation_id: str,
    headers: dict[str, str] | None = None,
) -> None:
    """
    Fail fast when an explicit ``--resume <id>`` names a conversation
    that does not exist on the server.

    :param base_url: Server base URL the lookup targets.
    :param conversation_id: The id passed to ``--resume``.
    :param headers: Optional auth headers for the server.
    :raises click.ClickException: When the id is not found (404). Other
        errors propagate so a transient failure isn't mislabeled.
    """

    async def _lookup() -> None:
        async with OmnigentClient(base_url=base_url, headers=headers) as client:
            await client.sessions.get(conversation_id)

    try:
        asyncio.run(_lookup())
    except ClientOmnigentError as exc:
        if exc.status_code == 404:
            raise click.ClickException(f"Conversation {conversation_id!r} not found.") from exc
        raise

def _run_picker(
    *,
    base_url: str,
    agent_name: str,
    headers: dict[str, str] | None = None,
) -> str | None:
    """
    Drive the ``--resume`` picker against a server.

    Looks up this agent's id (so the picker only shows THIS
    agent's conversations, not pooled across agents that share
    the persistent store), fetches the conversation list via the
    SDK, and runs the stderr/stdin picker.

    :param base_url: Server base URL,
        e.g. ``"http://127.0.0.1:9123"`` or
        ``"https://example.databricksapps.com"``.
    :param agent_name: Agent's registered name.
    :param headers: Optional auth headers for the server,
        e.g. ``{"Authorization": "Bearer <token>"}``. Required
        for remote servers; ``None`` for localhost.
    :returns: Selected conversation_id, or ``None`` if the user
        cancelled.
    :raises click.ClickException: When the agent has no prior
        conversations — ``--resume`` should fail-loud rather
        than silently open a picker the user can only cancel
        out of.
    """
    from omnigent.repl._resume_picker import pick_conversation_from_sdk

    async def _lookup() -> str | None:
        async with OmnigentClient(base_url=base_url, headers=headers) as client:
            # Multipart ``omnigent run <yaml>`` uploads now create a
            # fresh session-scoped agent for every session so users who
            # choose the same YAML ``name:`` never share a bundle. Resume
            # lookup therefore scopes by the user-authored name across
            # those distinct session agents rather than by a template
            # agent id returned from ``agents.get_by_name``.
            return await pick_conversation_from_sdk(
                client,
                agent_name=agent_name,
                agent_id=None,
                agent_name_filter=agent_name,
            )

    return asyncio.run(_lookup())

def _resolve_latest_conversation_id(
    *,
    base_url: str,
    agent_name: str,
    headers: dict[str, str] | None = None,
) -> str | None:
    """
    Find the most-recent conversation for *agent_name* on a
    server.

    Used to translate ``--continue`` into a concrete
    ``conversation_id`` after the server is reachable. The
    server-side filter joins through ``Task.agent_id``, so the
    returned conversation is guaranteed to belong to *this
    agent* (not pooled across agents that happen to share the
    DB).

    :param base_url: Server base URL,
        e.g. ``"http://127.0.0.1:9123"`` or
        ``"https://example.databricksapps.com"``.
    :param agent_name: The agent's registered name from the
        YAML's ``name:`` field.
    :param headers: Optional auth headers for the server,
        e.g. ``{"Authorization": "Bearer <token>"}``. Required
        for remote servers; ``None`` for localhost.
    :returns: The conversation_id, or ``None`` if no session
        with this agent name has prior conversations.
    """

    async def _lookup() -> str | None:
        async with OmnigentClient(base_url=base_url, headers=headers) as client:
            return await _resolve_latest_conversation_id_async(
                client=client,
                agent_name=agent_name,
            )

    return asyncio.run(_lookup())

async def _resolve_latest_conversation_id_async(
    *,
    client: OmnigentClient,
    agent_name: str,
) -> str | None:
    """
    Async core of :func:`_resolve_latest_conversation_id`.

    Factored out so tests can drive it against an in-process
    ASGI test client without spawning a subprocess server +
    re-opening a real HTTP connection. The sync entry point
    above wraps this with ``asyncio.run`` and an
    ``OmnigentClient`` connected to a real URL — the path
    used in production by ``_chat_local``.

    :param client: A connected :class:`OmnigentClient`.
    :param agent_name: The agent's registered name.
    :returns: The conversation_id of the agent's most-recent
        conversation, or ``None`` when the agent has no prior
        conversations (first-ever run, or a fresh persistent
        store).
    """
    sessions = await client.sessions.list(
        agent_name=agent_name,
        limit=1,
        order="desc",
        sort_by="updated_at",
    )
    if not sessions:
        return None
    return sessions[0].id


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _daemon as _sib_daemon
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
    from . import _local as _sib_local
    from . import _native as _sib_native
    from . import _overrides as _sib_overrides
    from . import _remote as _sib_remote
    from . import _repl as _sib_repl
    from . import _server_proc as _sib_server_proc
    from . import _types as _sib_types
    for _key, _value in _sib_daemon.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_entry.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_helpers.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_local.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_native.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_overrides.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_remote.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_repl.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_server_proc.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_types.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
