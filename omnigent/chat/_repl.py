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

def _spec_used_families(agent_yaml: Path | None) -> list[str]:
    """Best-effort: the harness surfaces a local agent's harnesses consume.

    Walks the agent's executor harness plus every sub-agent's harness and
    maps each to its surface, so the REPL startup header can show a
    per-surface creds line for a multi-vendor agent (e.g. polly's
    ``claude-sdk`` brain + ``claude-native`` / ``codex-native`` sub-agents
    yield ``["anthropic", "openai"]``; polly's ``pi`` brain adds the
    ``pi`` surface). Parsing is done WITHOUT env expansion — only harness
    names are needed, so unresolved secrets must not make this fail — and
    any error degrades to an empty list (the header simply omits the
    creds line).

    :param agent_yaml: Path to the agent directory, its ``config.yaml``,
        or a standalone YAML file; ``None`` for remote-URL targets.
    :returns: Sorted unique surface names, e.g. ``["anthropic", "openai",
        "pi"]``; empty when *agent_yaml* is ``None``, points at a
        standalone file (no sub-agents), or parsing fails.
    """
    # parse() reads an agent *directory* and discovers sub-agents under
    # ``<root>/agents/``. Resolve the root whether the caller passed the
    # directory itself or its ``config.yaml``. A standalone single-file agent
    # has no sub-agent directory to walk, so the launch harness alone drives
    # the header — skip it here.
    if agent_yaml is None:
        return []
    if agent_yaml.is_dir():
        root = agent_yaml
    elif agent_yaml.name in ("config.yaml", "config.yml"):
        root = agent_yaml.parent
    else:
        return []
    try:
        from omnigent.onboarding.provider_config import PI_SURFACE, harness_family
        from omnigent.spec import parse

        spec = parse(root, expand_env=False)
    except Exception:  # noqa: BLE001 — best-effort startup-header hint: a spec parse must never break `run`
        logger.debug("startup-header family parse failed for %s", agent_yaml, exc_info=True)
        return []

    families: set[str] = set()

    def _walk(node: AgentSpec) -> None:
        """Accumulate the surface for *node*'s harness and recurse into sub-agents."""
        harness = canonicalize_harness(node.executor.harness_kind) or node.executor.harness_kind
        fam = harness_family(harness)
        if fam is not None:
            families.add(fam)
        elif harness == PI_SURFACE:
            # pi spans both model families, so it has no single family —
            # it contributes its own surface, and the header resolves that
            # surface's effective credential (explicit pi default, else
            # the cross-family fallback).
            families.add(PI_SURFACE)
        for child in node.sub_agents:
            _walk(child)

    _walk(spec)
    return sorted(families)

def _run_repl(
    base_url: str,
    agent_name: str,
    tool_handler: ToolHandler | None,
    *,
    initial_message: str | None = None,
    resume_conversation_id: str | None = None,
    fork_session_id: str | None = None,
    log: bool = False,
    agent_yaml: Path | None = None,
    runner_id: str | None = None,
    runner_recover: Callable[[], str] | None = None,
    session_bundle: bytes | None = None,
    session_bundle_filename: str = "agent.tar.gz",
    ephemeral: bool = False,
    debug_events: bool = False,
    server_log_path: Path | None = None,
    runner_log_path: Path | None = None,
    resume_parts: list[str] | None = None,
    skills: list[SkillSpec] | None = None,
    auto_open_conversation: bool = False,
    attach_only: bool = False,
    attach_harness: str | None = None,
) -> None:
    """
    Open the REPL connected to the server.

    :param base_url: Server base URL.
    :param agent_name: Agent name to chat with.
    :param tool_handler: Optional client-side tool handler.
    :param initial_message: If set, auto-send on REPL start (maps
        to ``run_repl``'s ``initial_message`` kwarg, which the REPL
        treats as the first user turn — same hook the onboarding
        flow uses to auto-greet the user).
    :param resume_conversation_id: When set, the REPL opens
        attached to this existing conversation (replays recent
        items, threads new turns onto the existing
        ``previous_response_id``) rather than starting a fresh
        one. Resolved upstream from ``--continue`` /
        ``--resume <id>``.
    :param fork_session_id: When set, fork this session before
        entering the REPL. The REPL opens attached to the fork.
        Resolved upstream from ``--fork ID``.
    :param log: When ``True``, write a JSON dump of the active
        conversation to ``~/.omnigent/logs/`` on REPL exit.
        Maps to ``--log`` on the CLI.
    :param agent_yaml: Path to the agent spec on the local
        filesystem, when known. Threaded through to the tmux
        pane-integration helper so a sibling pane spawned via
        ``prefix + <split-key>`` can re-launch the same agent.
        ``None`` for remote-URL targets (the spec lives on the
        server, not locally) — the chooser falls back to
        ``OPT_LAUNCH_ARGV`` in that case.
    :param runner_id: Optional preferred runner id to send on
        requests, e.g. ``"runner_0123456789abcdef"``.
    :param runner_recover: Optional callback that restarts the
        local runner if it has exited and returns the live runner
        id.
    :param session_bundle: Optional gzipped agent bundle bytes used
        to create a fresh sessions-API session. Required for fresh
        sessions.
    :param session_bundle_filename: Multipart filename, e.g.
        ``"agent.tar.gz"``.
    :param ephemeral: When ``True``, suppress the resume hint on
        exit — the session data lives in a tmpdir that's gone
        after the process exits, so the hint would be misleading.
    :param debug_events: When ``True``, enable the SSE-to-UI debug
        pipeline (event tape overlay, JSONL log, toolbar counters).
        Maps to ``--debug-events`` on the CLI.
    :param server_log_path: Path to the local server's
        stdout/stderr log file, e.g.
        ``Path("~/.omnigent/logs/server/server-abc123.log")``. Shown in the
        Ctrl+O debug overview. ``None`` for remote servers.
    :param runner_log_path: Path to the local runner's
        stdout/stderr log file, e.g.
        ``Path("~/.omnigent/logs/runner/runner-abc123.log")``. Shown in the
        Ctrl+O debug overview. ``None`` when no local runner is used.
    :param resume_parts: Pre-built argument list prefix for the
        resume command shown on exit, e.g.
        ``["omnigent", "run", "agent.yaml", "--harness", "codex"]``.
        ``None`` uses the current process argv.
    :param skills: Parsed skill list from the agent spec, e.g.
        ``[SkillSpec(name="code-review", ...)]``. Each skill is
        registered as a ``/<name>`` slash command in the REPL.
        ``None`` (default) means no skill commands are registered.
    :param auto_open_conversation: When ``True``, open the
        browser conversation URL when the session id becomes known.
    """
    from omnigent.repl import run_repl
    from omnigent.repl._session_log import DEFAULT_LOG_DIR
    from omnigent.repl._tmux_pane import register_pane

    log_dir = DEFAULT_LOG_DIR if log else None

    # Mark this tmux pane as an omnigent context source and wrap
    # the user's prefix-table split bindings. No-op outside tmux.
    # ``resume_conversation_id`` (if present) becomes the pane's
    # initial conv id; otherwise a placeholder is used until the
    # first conversation is created.
    register_pane(
        conv_id=resume_conversation_id,
        agent_name=agent_name,
        agent_yaml=agent_yaml,
        launch_argv=list(sys.argv),
        server_url=base_url,
    )

    conversation_id: str | None = None

    async def _main() -> None:
        nonlocal conversation_id

        # Route unhandled asyncio exceptions (fire-and-forget tasks,
        # "Task exception was never retrieved") to the CLI diagnostics
        # log instead of stderr noise.
        from omnigent.cli_diagnostics import install_asyncio_exception_handler

        install_asyncio_exception_handler(asyncio.get_running_loop())

        # Derive the launch harness from the local spec so the REPL's
        # `/model` readout knows the right harness (and thus the right
        # provider family) before the first turn binds the session. ``None``
        # for URL targets / bundles, where the snapshot's harness fills it in.
        launch_harness: str | None = None
        agent_description: str | None = None
        if agent_yaml is not None:
            # Resolve the spec's config.yaml whether the user passed the agent
            # directory, its config.yaml, or a standalone single-file YAML — so
            # the startup header (harness → model + credential, summary)
            # populates in every case (a bare directory path would otherwise
            # peek at a directory and yield nothing).
            spec_config = agent_yaml / "config.yaml" if agent_yaml.is_dir() else agent_yaml
            raw_spec = _load_yaml_if_single_file(spec_config)
            executor = raw_spec.get("executor") if isinstance(raw_spec, dict) else None
            if isinstance(executor, dict):
                harness_name = executor.get("harness")
                if not harness_name and isinstance(executor.get("config"), dict):
                    harness_name = executor["config"].get("harness")
                if isinstance(harness_name, str) and harness_name:
                    launch_harness = canonicalize_harness(harness_name) or harness_name
            # One-line summary for the startup header (folded scalars are
            # normalized to a single line by the header builder).
            if isinstance(raw_spec, dict):
                desc = raw_spec.get("description")
                if isinstance(desc, str) and desc.strip():
                    agent_description = desc

        # Families the agent's harnesses (incl. sub-agents) consume — drives
        # the per-family creds line in the startup header for multi-vendor
        # agents like polly. Best-effort; empty on any failure.
        used_families = _spec_used_families(agent_yaml)

        # Attach has no local spec; the host's harness comes from the session
        # snapshot so the (lean) attach banner reflects what's actually running.
        if attach_harness is not None:
            launch_harness = attach_harness

        async with OmnigentClient(
            base_url=base_url,
            headers=_server_headers(runner_id=runner_id),
            auth=_server_auth(server_url=base_url),
        ) as client:
            # When --fork is set, call the fork endpoint before
            # entering the REPL so the user lands in the fork.
            effective_resume_id = resume_conversation_id
            if fork_session_id is not None:
                try:
                    fork_result = await client.sessions.fork(fork_session_id)
                except Exception as exc:
                    raise click.ClickException(f"Fork failed: {exc}") from exc
                effective_resume_id = fork_result["id"]
                click.echo(
                    f"Conversation forked. To return to the previous "
                    f"conversation, run --resume {fork_session_id}",
                    err=True,
                )
            conversation_id = await run_repl(
                client,
                agent_name,
                tool_handler,
                initial_message=initial_message,
                resume_conversation_id=effective_resume_id,
                log_dir=log_dir,
                debug_events=debug_events,
                server_log_path=server_log_path,
                runner_log_path=runner_log_path,
                session_bundle=session_bundle,
                session_bundle_filename=session_bundle_filename,
                runner_id=runner_id,
                runner_recover=runner_recover,
                resume_parts=resume_parts,
                ephemeral=ephemeral,
                skills=skills,
                server_url=base_url,
                harness=launch_harness,
                agent_description=agent_description,
                used_families=used_families,
                attach_only=attach_only,
                on_session_start=(
                    lambda session_id: open_conversation_link_if_enabled(
                        base_url=base_url,
                        conversation_id=session_id,
                        enabled=auto_open_conversation,
                        warn=lambda message: click.echo(message, err=True),
                    )
                ),
            )

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main())

def _run_one_shot(
    *,
    base_url: str,
    agent_name: str,
    tool_handler: ToolHandler | None,
    prompt: str,
    runner_id: str | None = None,
    session_bundle: bytes | None = None,
    session_bundle_filename: str = "agent.tar.gz",
    resume_conversation_id: str | None = None,
    auto_open_conversation: bool = False,
) -> None:
    """
    Send a single prompt to a remote server and print the final text.

    :param base_url: Remote server base URL.
    :param agent_name: Registered agent name to invoke.
    :param tool_handler: Optional client-side tool handler for local
        tool execution.
    :param prompt: User prompt to send as the single turn.
    :param runner_id: Optional preferred runner id to send on
        requests, e.g. ``"runner_0123456789abcdef"``.
    :param session_bundle: Optional gzipped agent bundle bytes used
        to create a fresh sessions-API session.
    :param session_bundle_filename: Multipart filename, e.g.
        ``"agent.tar.gz"``.
    :param resume_conversation_id: When set, resumes an existing
        session instead of creating a new one, e.g.
        ``"conv_abc123"``. ``None`` creates a fresh session.
    :param auto_open_conversation: When ``True``, open the
        browser conversation URL after the session is created or resumed.
    :returns: None.
    """

    async def _main() -> None:
        """Run the one-shot SDK query inside an async client context."""
        async with OmnigentClient(
            base_url=base_url,
            headers=_server_headers(runner_id=runner_id),
            auth=_server_auth(server_url=base_url),
        ) as client:
            if session_bundle is not None:
                text = await _query_sessions_once(
                    client=client,
                    agent_name=agent_name,
                    tool_handler=tool_handler,
                    prompt=prompt,
                    session_bundle=session_bundle,
                    session_bundle_filename=session_bundle_filename,
                    runner_id=runner_id,
                    resume_conversation_id=resume_conversation_id,
                    on_session_ready=(
                        lambda session_id: open_conversation_link_if_enabled(
                            base_url=base_url,
                            conversation_id=session_id,
                            enabled=auto_open_conversation,
                            warn=lambda message: click.echo(message, err=True),
                        )
                    ),
                )
                if text:
                    click.echo(text)
                return
            result = await client.query(
                model=agent_name,
                input=prompt,
                tool_handler=tool_handler,
            )
            if result.text:
                click.echo(result.text)

    try:
        asyncio.run(_main())
    except ClientOmnigentError as exc:
        # A turn that fails before the LLM stream starts (SETUP-phase
        # failure: spec resolution, spawn-env build) ends with only a
        # ``session.status: failed`` event, which SessionsChat.send
        # raises as an OmnigentError. Surface its message as a clean
        # CLI error instead of an opaque traceback so ``-p`` users see
        # why the turn produced no output.
        raise click.ClickException(str(exc)) from exc

def _load_tool_handler(name: str) -> ToolHandler:
    """
    Load a client-side tool set by name and wrap it as a ToolHandler.

    Prefers the modern ``@tool``-decorated functions (exposed
    by the tool set as ``_TOOL_FNS``) so the SDK's D6 lifecycle
    can detect ``synchronous: false`` properties on the wire
    schema. Falls back to the legacy ``TOOLS`` + ``execute_tool``
    surface for tool sets that haven't migrated yet — same
    behavior as before, just constructed manually rather than
    via ``build_tool_handler``.

    :param name: Tool set name, e.g. ``"coding"``.
    :returns: A ToolHandler with schemas and execute function.
    :raises click.ClickException: If the tool set is not found.
    """
    try:
        from omnigent.client_tools import get_tool_set

        tool_set = get_tool_set(name)
    except (ImportError, SystemExit) as exc:
        raise click.ClickException(
            f"Tool set {name!r} not found. Available: coding, async_demo"
        ) from exc

    # `_TOOL_FNS` is the "modern path" marker on a tool-set module —
    # @tool-decorated functions exported as a module-level list.
    # Legacy tool-sets instead expose a `build()` callable. Use
    # hasattr so mypy can narrow the attribute access below.
    if hasattr(tool_set, "_TOOL_FNS"):
        fns = tool_set._TOOL_FNS
        # Modern path: @tool-decorated functions. The SDK's
        # build_tool_handler derives schemas from type hints +
        # docstrings, strips ``synchronous`` routing hints
        # before invoking the user fn, and bridges sync vs
        # async ``execute`` correctly.
        from omnigent_client.tools import build_tool_handler

        return build_tool_handler(fns)

    # Legacy path: hand-written TOOLS dict + sync
    # execute_tool dispatcher.
    def execute(call: ToolCallInfo) -> str:
        """
        Execute a client-side tool call (legacy sync path).

        :param call: The tool call info with name and arguments.
        :returns: The tool result string.
        """
        return str(tool_set.execute_tool(call.name, call.arguments))

    return ToolHandler(schemas=tool_set.TOOLS, execute=execute)


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _daemon as _sib_daemon
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
    from . import _local as _sib_local
    from . import _native as _sib_native
    from . import _overrides as _sib_overrides
    from . import _remote as _sib_remote
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
