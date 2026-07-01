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

def _bundle_agent(agent_path: Path) -> bytes:
    """
    Build a gzipped agent bundle for ``POST /v1/sessions``.

    Keeps the import of the CLI bundler local to avoid loading the
    full click command tree at module import time.

    :param agent_path: Local YAML file or agent directory.
    :returns: Gzipped tarball bytes suitable for the sessions
        multipart ``bundle`` part.
    :raises OmnigentError: If bundling fails, for example due to
        unresolved environment variables.
    """
    from omnigent.cli import _bundle

    return _bundle(agent_path)

def _chat_local(
    agent_path: str,
    tool_handler: ToolHandler | None,
    *,
    overrides: ChatOverrides | None = None,
    initial_message: str | None = None,
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
    Start a local server with the agent and open the REPL.

    The spec is parsed and validated in-process before launching the
    server subprocess so that config errors (unresolved env vars,
    invalid YAML, missing required fields) surface with the real
    exception message instead of being lost to the subprocess's
    silenced stderr.

    When *overrides* has any non-None field (or the YAML declares
    neither harness nor model), the source spec is materialized into
    a temp directory with the overrides + default-model fallback
    baked into its ``executor`` block, and the server is pointed at
    that copy. The user's source YAML is never mutated.

    :param agent_path: Path to the agent directory or bundle.
    :param tool_handler: Optional client-side tool handler.
    :param overrides: CLI overrides to bake into the spec before
        starting the server. ``None`` means no override (same shape
        as ``ChatOverrides()`` with all-None fields).
    :param initial_message: If set without a resume target, run one
        request and exit. If set with a resume target, auto-send on
        REPL start.
    :param ephemeral: When ``True``, point the local server at a
        fresh per-run tmpdir for its data store. ``False``
        (default) uses the persistent ``~/.omnigent``
        location so prior conversations remain reachable —
        see designs/RUN_OMNIGENT_SESSION_RESUMPTION.md.
    :param resume_conversation_id: When set, open the REPL
        attached to this existing conversation rather than
        creating a fresh one.
    :param resume_latest: When ``True``, resolve "the most
        recent conversation for this agent" via the API
        after the server boots and use it as the resume
        target. Ignored when *resume_conversation_id* is
        already set. Maps to ``--continue`` on the CLI.
    :param resume_picker: When ``True``, open the
        interactive picker after the server boots. Maps to
        ``--resume`` / ``-r`` with no value on the CLI.
    :param log: When ``True``, write a JSON dump of the active
        conversation to ``~/.omnigent/logs/`` on REPL exit.
        Maps to ``--log`` on the CLI.
    :param debug_events: When ``True``, enable the SSE-to-UI debug
        pipeline. Forwarded to ``_chat_with_server``.
    :param auto_open_conversation: When ``True``, open the
        browser conversation URL when the session id becomes known.
    """
    path = Path(agent_path)
    if not path.exists():
        raise click.ClickException(f"Agent path not found: {agent_path}")
    path = _canonicalize_local_agent_path(path)

    effective_overrides = overrides if overrides is not None else ChatOverrides()
    spec_path = _materialize_override_bundle(path, effective_overrides)
    try:
        # Parse once: validate + extract name + skills in a single pass.
        # Wraps the same exceptions as _validate_agent_spec so config
        # errors surface as clean ClickExceptions.
        try:
            agent_spec = load_spec(spec_path)
        except (OmnigentError, FileNotFoundError) as exc:
            raise click.ClickException(str(exc)) from exc
        agent_name = agent_spec.name or _fallback_label(spec_path)
        all_skills = _merge_host_skills(agent_spec, spec_path)
        port = _find_free_port()
        server = _start_local_server(
            spec_path,
            port,
            ephemeral=ephemeral,
        )

        try:
            _wait_for_server(port, server)
            base_url = f"http://127.0.0.1:{port}"
            _web_ui_dist = Path(__file__).parent / "server" / "static" / "web-ui"
            if _web_ui_dist.is_dir() and (_web_ui_dist / "index.html").is_file():
                console.print(f"\n  Web UI: [bold]{base_url}[/bold]")
                console.print("  Open in your browser for a visual interface\n")
            effective_resume_id = _resolve_resume_target(
                base_url=base_url,
                agent_name=agent_name,
                resume_conversation_id=resume_conversation_id,
                resume_latest=resume_latest,
                resume_picker=resume_picker,
            )
            bundle_bytes = _bundle_agent(spec_path)
            _chat_with_server(
                base_url,
                tool_handler,
                agent_name=agent_name,
                initial_message=initial_message,
                resume_conversation_id=effective_resume_id,
                runner_id=server.runner_id,
                fork_session_id=fork_session_id,
                log=log,
                agent_yaml=spec_path,
                session_bundle=bundle_bytes,
                ephemeral=ephemeral,
                debug_events=debug_events,
                server_log_path=server.log_path,
                resume_parts=resume_parts,
                skills=all_skills or None,
                auto_open_conversation=auto_open_conversation,
            )
        finally:
            _stop_local_server(server)
    finally:
        _cleanup_materialized_override_bundle(spec_path)

def _run_local_headless_prompt(
    agent_path: str,
    tool_handler: ToolHandler | None,
    *,
    overrides: ChatOverrides,
    prompt: str,
    ephemeral: bool = False,
) -> None:
    """
    Start a local server, run one prompt, print response, and stop.

    :param agent_path: Local YAML file or agent directory.
    :param tool_handler: Optional client-side tool handler.
    :param overrides: CLI overrides to bake into the spec.
    :param prompt: User prompt for the single turn.
    :param ephemeral: When ``True``, use a fresh per-run local
        server database and artifact directory.
    :returns: None.
    """
    path = Path(agent_path)
    if not path.exists():
        raise click.ClickException(f"Agent path not found: {agent_path}")
    path = _canonicalize_local_agent_path(path)

    spec_path = _materialize_override_bundle(path, overrides)
    try:
        _validate_agent_spec(spec_path)

        agent_name = _extract_agent_name(spec_path)
        port = _find_free_port()
        server = _start_local_server(
            spec_path,
            port,
            ephemeral=ephemeral,
        )

        try:
            _wait_for_server(port, server)
            _run_headless_prompt(
                f"http://127.0.0.1:{port}",
                agent_name,
                tool_handler,
                prompt=prompt,
                runner_id=server.runner_id,
                session_bundle=_bundle_agent(spec_path),
            )
        finally:
            _stop_local_server(server)
    finally:
        _cleanup_materialized_override_bundle(spec_path)

def _run_headless_prompt(
    base_url: str,
    agent_name: str,
    tool_handler: ToolHandler | None,
    *,
    prompt: str,
    runner_id: str | None = None,
    session_bundle: bytes | None = None,
    session_bundle_filename: str = "agent.tar.gz",
) -> None:
    """
    POST one prompt through the SDK and print the final assistant text.

    Uses the sessions API: create the session, bind the runner,
    send one message, print text, and return.

    :param base_url: Server base URL, e.g.
        ``"http://127.0.0.1:8123"``.
    :param agent_name: Agent display name, e.g. ``"hello_world"``.
    :param tool_handler: Optional client-side tool handler.
    :param prompt: User prompt for the single turn.
    :param runner_id: Registered runner id, e.g.
        ``"runner_0123456789abcdef"``. Required with
        *session_bundle*.
    :param session_bundle: Optional gzipped agent bundle bytes.
    :param session_bundle_filename: Multipart filename, e.g.
        ``"agent.tar.gz"``.
    :raises SystemExit: Exits with code 1 after printing the server
        error text when the stream emits ``response.error`` or
        returns ``ResponseFailed`` without output text.
    :returns: None.
    """

    async def _main() -> None:
        async with OmnigentClient(
            base_url=base_url,
            headers=_server_headers(runner_id=runner_id),
            auth=_server_auth(server_url=base_url),
        ) as client:
            if session_bundle is not None:
                result_text = await _query_sessions_once(
                    client=client,
                    agent_name=agent_name,
                    tool_handler=tool_handler,
                    prompt=prompt,
                    session_bundle=session_bundle,
                    session_bundle_filename=session_bundle_filename,
                    runner_id=runner_id,
                )
                if result_text:
                    print(result_text)
                return

            session = client.session(model=agent_name, tool_handler=tool_handler)
            chunks: list[str] = []
            terminal_text: str | None = None
            error_text: str | None = None
            async for event in session.send(prompt):
                if isinstance(event, TextDelta):
                    chunks.append(event.delta)
                elif isinstance(event, ErrorEvent):
                    error_text = event.error.message or event.error.code
                elif isinstance(
                    event,
                    ResponseCompleted | ResponseFailed | ResponseIncomplete | ResponseCancelled,
                ):
                    terminal_text = _response_output_text(event.response.output)

            streamed_text = "".join(chunks)
            # Prefer the real error from a response.error SSE event over the
            # generic terminal-event message ("Failed to retrieve final response")
            # that _build_terminal_event substitutes when it can't read the task.
            if streamed_text:
                print(streamed_text)
            elif error_text:
                print(f"Error: {error_text}", file=sys.stderr)
                raise SystemExit(1)
            elif terminal_text:
                print(terminal_text)

    try:
        asyncio.run(_main())
    except ClientOmnigentError as exc:
        # SETUP-phase failure: SessionsChat.send raises on a terminal
        # ``session.status: failed`` (no response.failed is emitted).
        # Surface it the same way as a response.error event so headless
        # ``-p`` exits non-zero with the real message.
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _daemon as _sib_daemon
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
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
    for _key, _value in _sib_entry.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_helpers.__dict__.items():
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
