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

def _default_cli_model() -> str:
    """
    Return the model used when neither YAML nor CLI flag picks one.

    Reads ``OMNIGENT_MODEL`` from the environment with
    :data:`_DEFAULT_AD_HOC_MODEL` as the final fallback. The read
    happens at YAML-materialization time so the resolved model
    gets baked into the bundle's executor block — the materialized
    spec is self-contained and independent of any later env state.

    Mirrors :func:`omnigent.inner.cli._default_cli_model` so
    legacy and Omnigent paths agree on the env-var contract.

    :returns: The default model identifier, e.g.
        ``"databricks-gpt-5-4"`` or whatever the user pinned in
        ``OMNIGENT_MODEL``.
    """
    return os.environ.get(_OMNIGENT_MODEL_ENV_VAR, _DEFAULT_AD_HOC_MODEL)

def run_chat(
    target: str,
    client_tools: str | None,
    *,
    server_url: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    prompt: str | None = None,
    system_prompt: str | None = None,
    ephemeral: bool = False,
    resume_conversation_id: str | None = None,
    resume_latest: bool = False,
    resume_picker: bool = False,
    fork_session_id: str | None = None,
    log: bool = False,
    debug_events: bool = False,
    resume_parts: list[str] | None = None,
    auto_open_conversation: bool = False,
) -> None:
    """
    Main entry point for ``omnigent run`` (and the ``attach`` client).

    :param target: Path to an agent directory/bundle, or a server URL.
    :param client_tools: Optional client-side tool set name.
    :param server_url: Optional server URL to use with a local
        agent path, e.g. ``"https://example.databricksapps.com"``.
        ``None`` starts a local server for path targets.
    :param harness: CLI ``--harness`` override, e.g. ``"claude-sdk"``.
        Applied only to local-mode targets (YAML path / directory);
        ignored for remote server URLs.
    :param model: CLI ``--model`` override, e.g.
        ``"databricks-claude-sonnet-4-6"``. Local-mode only.
    :param prompt: CLI ``-p`` / ``--prompt`` — send one user turn,
        print the response, and exit.
    :param system_prompt: CLI ``--system-prompt`` — overrides the
        YAML's top-level ``prompt`` field. Local-mode only.
    :param ephemeral: When ``True``, place the local server's
        SQLite DB and artifacts in a per-run tmpdir instead of
        the persistent ``~/.omnigent`` location. Maps to
        ``--no-session`` on the CLI. Local-mode only — passing
        this with a remote URL target raises
        :class:`click.ClickException` (the remote server owns
        its own persistence).
    :param resume_conversation_id: When set, the REPL opens
        attached to this existing conversation instead of
        creating a fresh one — replays recent items and
        threads new turns onto the existing
        ``previous_response_id`` chain. Maps to
        ``--resume <id>`` on the CLI.
    :param resume_latest: When ``True``, resolve "the most
        recent conversation for this agent" against the
        persistent store after the server boots and attach
        the REPL to it. Maps to ``--continue`` on the CLI.
        Local-mode only — passing this with a remote URL
        target raises :class:`click.ClickException`. Mutually
        exclusive with *resume_conversation_id* — the latter
        takes precedence if both are set.
    :param resume_picker: When ``True``, open the interactive
        stderr/stdin picker after the server boots and let
        the user choose a conversation. Maps to ``--resume``
        / ``-r`` with no value on the CLI. Local-mode only — passing this
        with a remote URL target raises
        :class:`click.ClickException` (the picker has no way
        to scope to a single agent on a multi-agent remote
        server without an explicit hand-off). Precedence:
        ``resume_conversation_id`` wins over ``resume_picker``
        wins over ``resume_latest``; user-cancelled picker
        falls through to a fresh conversation.
    :param fork_session_id: When set, fork this session before
        entering the REPL. The fork creates a deep copy of the
        source session's items into a new session; the REPL then
        opens attached to the fork. Maps to ``--fork ID`` on the
        CLI. Mutually exclusive with ``resume_conversation_id``,
        ``resume_latest``, and ``resume_picker``.
    :param log: When ``True``, write a JSON dump of the active
        conversation to ``~/.omnigent/logs/`` on REPL exit.
        Maps to ``--log`` on the CLI (default-on for the legacy
        path, default-off here so it stays explicit on
        Omnigent mode). See ``omnigent.repl._session_log`` for the
        schema. Local-mode only — passing this with a remote
        URL target raises :class:`click.ClickException`
        (no client-side conversation hand-off to dump).
    :param debug_events: When ``True``, enable the SSE-to-UI debug
        pipeline (event tape overlay via ``Ctrl+E``, JSONL event
        logging to ``~/.omnigent/debug/``, and pipeline stage
        counters in the toolbar). Maps to ``--debug-events`` on the
        CLI.
    :param resume_parts: Pre-built argument list prefix for the
        resume hint, e.g. ``["omnigent", "run", "agent.yaml",
        "--server", "https://example.com"]``.  Built from Click's
        parsed context at CLI dispatch time.  ``None`` omits the
        resume hint on exit.
    :param auto_open_conversation: When ``True``, open the
        browser conversation URL when the session id becomes known.
    """
    # Client-side tools are a CLI/TUI convenience (e.g. shell access
    # for coding agents). They don't affect agent behavior — the spec
    # is self-contained.
    tool_handler = _load_tool_handler(client_tools) if client_tools else None

    overrides = ChatOverrides(
        harness=harness,
        model=model,
        system_prompt=system_prompt,
    )

    if server_url is not None and _is_url(target):
        raise click.ClickException(
            "--server is for binding a local agent YAML to a server. "
            "Pass a YAML path as the target (got a URL)."
        )

    if _is_url(target):
        if any(
            v is not None for v in (overrides.harness, overrides.model, overrides.system_prompt)
        ):
            raise click.ClickException(
                "--harness / --model / --system-prompt only apply to local "
                "agent paths. The remote server controls its own agent registrations."
            )
        # Local-only resume / persistence flags would silently
        # vanish on the remote path (the server owns its own
        # store; we have no client-side conversation id to feed
        # the picker, no client-side log target). Fail loud rather
        # than letting a legacy/AP resume mode mismatch appear to work.
        if ephemeral or resume_latest or resume_picker or log:
            raise click.ClickException(
                "--no-session / --continue / --resume / --log only apply to "
                "local agent paths. "
                "The remote server owns its own persistence and conversation lookup. "
                "Pass --resume <id> with a remote URL to attach to a specific conversation."
            )
        # Discover host-scope skills from cwd so ``/skill-name`` slash
        # commands work even when connecting to a remote server with no
        # local agent spec.
        host_skills = discover_host_skills(Path.cwd(), "all")
        _chat_with_server(
            target,
            tool_handler,
            initial_message=prompt,
            resume_conversation_id=resume_conversation_id,
            fork_session_id=fork_session_id,
            debug_events=debug_events,
            resume_parts=resume_parts,
            skills=host_skills or None,
            auto_open_conversation=auto_open_conversation,
        )
    elif ephemeral:
        # ``--no-session`` keeps the legacy in-process ephemeral server: the
        # daemon-backed server is persistent + shared and has no per-run DB
        # isolation. Not combinable with an explicit ``--server``.
        if server_url:
            raise click.ClickException(
                "--no-session is not supported with --server; the uploaded agent "
                "is already scoped to the CLI session."
            )
        _chat_local(
            target,
            tool_handler,
            overrides=overrides,
            initial_message=prompt,
            ephemeral=True,
            resume_conversation_id=resume_conversation_id,
            resume_latest=resume_latest,
            resume_picker=resume_picker,
            fork_session_id=fork_session_id,
            log=log,
            debug_events=debug_events,
            resume_parts=resume_parts,
            auto_open_conversation=auto_open_conversation,
        )
    else:
        # Non-URL target → the host daemon is the backend. It connects to
        # the given ``--server`` URL, or starts (and connects to) a persistent
        # local Omnigent server when none is provided; this returns that concrete
        # URL. The agent is uploaded as a session and the daemon spawns +
        # *owns* the runner (the CLI only attaches the REPL), matching
        # claude-native.
        from omnigent.cli import _ensure_backend

        base_url = _ensure_backend(server_url)
        _chat_via_daemon(
            target,
            base_url,
            tool_handler,
            overrides=overrides,
            initial_message=prompt,
            resume_conversation_id=resume_conversation_id,
            resume_latest=resume_latest,
            resume_picker=resume_picker,
            fork_session_id=fork_session_id,
            log=log,
            debug_events=debug_events,
            resume_parts=resume_parts,
            auto_open_conversation=auto_open_conversation,
        )

def run_prompt(
    target: str,
    client_tools: str | None,
    *,
    harness: str | None = None,
    model: str | None = None,
    prompt: str,
    system_prompt: str | None = None,
    ephemeral: bool = False,
) -> None:
    """Run one prompt headlessly and print only the assistant text.

    This is the non-interactive sibling of :func:`run_chat` for
    ``omnigent run ... -p``. It deliberately bypasses the
    Rich/prompt-toolkit REPL startup path so ``-p`` behaves like a
    scriptable CLI mode: send one turn, print the assistant response,
    and return.

    :param target: Path to an agent directory/bundle, or a server URL.
    :param client_tools: Optional client-side tool set name.
    :param harness: CLI ``--harness`` override for local targets.
    :param model: CLI ``--model`` override for local targets.
    :param prompt: User prompt to send.
    :param system_prompt: CLI ``--system-prompt`` override for local targets.
    :param ephemeral: When ``True``, use a fresh per-run local
        server database and artifact directory.
    """
    tool_handler = _load_tool_handler(client_tools) if client_tools else None
    overrides = ChatOverrides(
        harness=harness,
        model=model,
        system_prompt=system_prompt,
    )

    if _is_url(target):
        if any(
            v is not None for v in (overrides.harness, overrides.model, overrides.system_prompt)
        ):
            raise click.ClickException(
                "--harness / --model / --system-prompt only apply to local "
                "agent paths. The remote server controls its own agent registrations."
            )
        base_url = target.rstrip("/")
        agent_name = _pick_agent(base_url, quiet=True)
        _run_headless_prompt(
            base_url,
            agent_name,
            tool_handler,
            prompt=prompt,
        )
        return

    _run_local_headless_prompt(
        target,
        tool_handler,
        overrides=overrides,
        prompt=prompt,
        ephemeral=ephemeral,
    )

def run_attach(
    *,
    base_url: str,
    conversation_id: str,
    client_tools: str | None = None,
    debug_events: bool = False,
    auto_open_conversation: bool = False,
    resume_parts: list[str] | None = None,
) -> None:
    """
    Attach the REPL to a LIVE conversation, dispatching to its existing runner.

    ``attach`` is a pure co-drive client: it never launches OR binds a runner.
    Turns post to the runner the host already bound (``POST /v1/sessions/{id}/
    events``, which needs only edit access), exactly like the web UI co-drive,
    and the server routes them to that runner. Binding a runner is owner-only
    server-side — so a teammate attaching to a shared session must NOT re-bind;
    post-only is what makes cross-user co-drive work. A read-only pre-flight
    confirms the session's host runner is online (``attach`` can't start one),
    failing loud otherwise.

    :param base_url: Omnigent server hosting the session, e.g.
        ``"http://127.0.0.1:6767"``.
    :param conversation_id: Live conversation/session id to join, e.g.
        ``"conv_abc123"``.
    :param client_tools: Optional client-side tool set name, e.g. ``"coding"``.
    :param debug_events: When ``True``, enable the SSE debug pipeline overlay.
    :param auto_open_conversation: When ``True``, open the browser conversation
        URL once attached.
    :param resume_parts: Argument-list prefix for the on-exit resume hint, e.g.
        ``["cli", "attach", "conv_abc123", "--server", "http://..."]``.
    :raises click.ClickException: If the session has no online runner (its host
        is offline) — ``attach`` never starts one.
    """
    base_url = base_url.rstrip("/")
    # Pre-flight (read-only): a co-drive client can only run turns if the
    # session's host runner is online; attach never launches one. The same
    # snapshot gives the agent name + harness for an honest banner.
    info = _attach_session_info(base_url=base_url, conversation_id=conversation_id)
    if not info.runner_online:
        raise click.ClickException(
            f"Session {conversation_id} has no online runner on {base_url} — its "
            "host is offline. `attach` never starts a runner; bring the host back "
            "(`omnigent run` locally, or reconnect it with `omnigent host`), "
            "then attach again."
        )

    tool_handler = _load_tool_handler(client_tools) if client_tools else None
    # Post-only co-drive: no runner_id / recover, ``attach_only`` so the REPL
    # adapter never PATCHes the (owner-only) runner binding — turns dispatch to
    # the host's already-bound runner. ``agent_name`` is the session's own (so
    # we skip the server agent-picker + its "Agent: …" echo); ``attach_harness``
    # makes the banner reflect what the host is running.
    _chat_with_server(
        base_url,
        tool_handler,
        agent_name=info.agent_name,
        resume_conversation_id=conversation_id,
        attach_only=True,
        attach_harness=info.harness,
        debug_events=debug_events,
        resume_parts=resume_parts,
        auto_open_conversation=auto_open_conversation,
    )


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _daemon as _sib_daemon
    from . import _helpers as _sib_helpers
    from . import _local as _sib_local
    from . import _native as _sib_native
    from . import _overrides as _sib_overrides
    from . import _remote as _sib_remote
    from . import _repl as _sib_repl
    from . import _server_proc as _sib_server_proc
    from . import _sessions as _sib_sessions
    from . import _types as _sib_types
    for _key, _value in _sib_daemon.__dict__.items():
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
    for _key, _value in _sib_sessions.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_types.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
