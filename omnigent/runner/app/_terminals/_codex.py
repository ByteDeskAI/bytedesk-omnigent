"""Runner FastAPI app — spawns harness subprocesses and dispatches to them.

Per ``designs/RUNNER.md`` §1, the runner owns harness subprocesses.
It resolves the harness type + spawn-env from the agent spec (either
via a spec_resolver callback for in-process use, or via
GET /v1/agents/{id}/contents for out-of-process use).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import mimetypes
import os
import sys
import tempfile
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Type-only import: the runner keeps codex deps out of its runtime import
    # graph (they are imported lazily inside the codex-native helpers).
    from omnigent.codex_native_app_server import CodexAppServerClient
    from omnigent.runner.cost_advisor import AdvisorTurnResult

    # Boundary payload TypedDicts (sweep-2 BDP-2366). Imported type-only so
    # the runtime ``app`` <-> ``tool_dispatch`` import stays lazy (the cycle
    # both modules already break with function-level imports).
    from omnigent.runner.tool_dispatch import (
        SessionSnapshotPayload,
        SubagentInboxPayload,
    )
    from omnigent.terminals.registry import TerminalListEntry

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse

from omnigent.entities.session_resources import (
    DEFAULT_ENVIRONMENT_ID,
    SessionResourceView,
    resolve_terminal_entry_by_resource_id,
    session_resource_view_to_dict,
    terminal_resource_id,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness, is_native_harness
from omnigent.llms.summarize import (
    build_summarization_input,
    build_summarization_prompt,
    extract_summary_text,
)
from omnigent.model_override import validate_model_override
from omnigent.runner import pending_approvals
from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
from omnigent.runner.resource_registry import (
    CLAUDE_NATIVE_TERMINAL_ROLE,
    CODEX_NATIVE_TERMINAL_ROLE,
    OMNIGENT_REPL_TERMINAL_ROLE,
    PI_NATIVE_TERMINAL_ROLE,
    SessionResourceRegistry,
    TerminalExitEvent,
    TerminalLifecycle,
)
from omnigent.runner.subagent_status import (
    _TERMINAL as _SUBAGENT_TERMINAL_STATUSES,
)
from omnigent.runner.subagent_status import (
    SubagentWorkStatus,
    TerminalStatus,
)
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
from omnigent.spec.parser import discover_host_skills
from omnigent.spec.types import AgentSpec, LocalToolInfo, SkillSpec
from omnigent.terminals.ws_bridge import (
    WS_CLOSE_TERMINAL_NOT_FOUND,
    bridge_tmux_pty_to_websocket,
)
from omnigent.tools.builtins.load_skill import (
    find_skill_by_name,
    format_skill_meta_text,
)

_logger = logging.getLogger(__name__)
def _import_parent_bindings() -> None:
    from .. import (
        _constants as _parent_constants,
        _dispatch as _parent_dispatch,
        _forwarders as _parent_forwarders,
        _harness as _parent_harness,
        _helpers as _parent_helpers,
        _policy as _parent_policy,
        _state as _parent_state,
        _streaming as _parent_streaming,
        _subagents as _parent_subagents,
        _timers as _parent_timers,
        _tools as _parent_tools,
    )

    g = globals()
    for _mod in (
        _parent_constants,
        _parent_state,
        _parent_dispatch,
        _parent_forwarders,
        _parent_harness,
        _parent_helpers,
        _parent_policy,
        _parent_streaming,
        _parent_subagents,
        _parent_timers,
        _parent_tools,
    ):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_parent_bindings()

@dataclasses.dataclass
class _CodexNativeLaunchConfig:
    """
    Persisted launch config needed for runner-owned Codex terminal setup.

    :param workspace: Workspace cwd for the Codex app-server and TUI,
        e.g. ``Path("/Users/me/repo")``.
    :param policy_server_url: Omnigent server URL for the Codex policy hook and
        forwarder, e.g. ``"http://127.0.0.1:8123"``.
    :param terminal_launch_args: User pass-through Codex CLI args, e.g.
        ``["--config", "approval_policy=on-request"]``.
    :param model_override: Persisted model override, e.g.
        ``"gpt-5.4-mini"``.
    :param external_session_id: Existing Codex thread id to resume, e.g.
        ``"thread_abc123"``.
    :param fork_source_id: SOURCE conversation id stamped on a forked
        clone (``omnigent.fork.source_id``), used to locate the
        source's ``CODEX_HOME`` when cloning its rollout, e.g.
        ``"conv_source"``. ``None`` when the session is not a fork.
    :param fork_source_external_id: SOURCE Codex thread id stamped on a
        forked clone (``omnigent.fork.source_external_session_id``),
        e.g. ``"019e96aa-..."``. ``None`` when the source had no captured
        thread id (the clone then resumes fresh).
    :param fork_carry_history: ``True`` on a forked clone bound to a
        native target (``omnigent.fork.carry_history``); when no source
        rollout exists to clone (an SDK or cross-family source) the runner
        builds the clone's rollout from the copied Omnigent items instead (see
        ``_ensure_local_codex_resume_rollout``).
    """

    workspace: Path
    policy_server_url: str
    terminal_launch_args: list[str] | None
    model_override: str | None
    external_session_id: str | None
    fork_source_id: str | None
    fork_source_external_id: str | None
    fork_carry_history: bool

def _codex_session_workspace(session_workspace: str | None) -> Path:
    """
    Resolve the cwd for a runner-owned Codex terminal.

    Mirrors :func:`_auto_create_claude_terminal`'s workspace
    resolution and the per-session filesystem registry
    (``_resolve_session_fs_registry``): the server-stored session
    ``workspace`` wins (it holds the git-worktree path for worktree
    sessions, or the repo root otherwise), falling back to the
    runner's ``OMNIGENT_RUNNER_WORKSPACE``.

    Deliberately does NOT consult ``ResolvedSpec.workdir`` — in the
    out-of-process runner that is the agent-bundle extraction dir
    (``runner-specs-<id>/ag_<id>-v<ver>``), not the repo, so using it
    stranded Codex in a temp dir with no ``.git`` (and ignored the
    worktree entirely).

    Normalizes the chosen value with ``strip().expanduser().resolve()``,
    matching the runner entrypoint's ``_runner_workspace_from_env`` and the
    per-session filesystem registry's ``Path(...).resolve()`` so a padded or
    ``~``-prefixed value can't yield a non-existent cwd or diverge from the
    path the Files panel watches.

    :param session_workspace: The session's ``workspace`` from
        ``GET /v1/sessions/{id}``, e.g.
        ``"/Users/me/repo-worktrees/feature-x"``. ``None`` when the
        snapshot omits it.
    :returns: Workspace path for the terminal cwd.
    :raises RuntimeError: If no workspace is available (neither the
        session snapshot nor ``OMNIGENT_RUNNER_WORKSPACE``).
    """
    raw = session_workspace or _required_runner_env("OMNIGENT_RUNNER_WORKSPACE")
    return Path(raw.strip()).expanduser().resolve()

async def _codex_native_launch_config(
    *,
    session_id: str,
    server_client: httpx.AsyncClient | None,
) -> _CodexNativeLaunchConfig:
    """
    Fetch and validate persisted Codex launch config for a session.

    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :param server_client: Runner Omnigent server client.
    :returns: Parsed launch config.
    :raises RuntimeError: If the session snapshot or required runner env is
        unavailable.
    """
    if server_client is None:
        raise RuntimeError("server_client is required for runner-owned Codex terminals.")
    try:
        resp = await server_client.get(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Could not fetch Codex launch config for {session_id!r}.") from exc
    if resp.status_code != 200:
        raise RuntimeError(
            f"Could not fetch Codex launch config for {session_id!r}: "
            f"GET /v1/sessions returned {resp.status_code}."
        )
    try:
        snapshot = resp.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Could not fetch Codex launch config for {session_id!r}: invalid JSON."
        ) from exc
    if not isinstance(snapshot, dict):
        raise RuntimeError(
            f"Could not fetch Codex launch config for {session_id!r}: "
            "snapshot was not a JSON object."
        )
    terminal_launch_args = snapshot.get("terminal_launch_args")
    if terminal_launch_args is not None and not (
        isinstance(terminal_launch_args, list)
        and all(isinstance(arg, str) for arg in terminal_launch_args)
    ):
        raise RuntimeError(f"Invalid terminal_launch_args for Codex session {session_id!r}.")
    model_override = snapshot.get("model_override")
    if model_override is not None:
        if not isinstance(model_override, str) or not model_override:
            raise RuntimeError(f"Invalid model_override for Codex session {session_id!r}.")
        # Defense-in-depth: re-validate the persisted override at the runner
        # boundary so a value that somehow bypassed server-side validation
        # can never reach the Codex ``config.toml`` / ``--model`` argv as
        # shell- or TOML-shaped input.
        try:
            validate_model_override(model_override)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid model_override for Codex session {session_id!r}: {exc}"
            ) from exc
    external_session_id = snapshot.get("external_session_id")
    if external_session_id is not None and (
        not isinstance(external_session_id, str) or not external_session_id
    ):
        raise RuntimeError(f"Invalid external_session_id for Codex session {session_id!r}.")
    # The session's stored workspace is the worktree path for worktree
    # sessions (set by _create_session_worktree), or the repo root
    # otherwise. Use it as the Codex terminal cwd so worktree sessions
    # land in the worktree, matching claude-native and the Files panel.
    session_workspace = snapshot.get("workspace")
    if session_workspace is not None and (
        not isinstance(session_workspace, str) or not session_workspace
    ):
        raise RuntimeError(f"Invalid workspace for Codex session {session_id!r}.")
    # Fork directives stamped on a clone at fork time. Only consulted when
    # the clone has no external_session_id of its own yet (see the
    # fork-source branch in _auto_create_codex_terminal); inert otherwise.
    from omnigent.stores.conversation_store import (
        FORK_CARRY_HISTORY_LABEL_KEY,
        FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
        FORK_SOURCE_LABEL_KEY,
    )

    fork_source_id: str | None = None
    fork_source_external_id: str | None = None
    fork_carry_history = False
    labels = snapshot.get("labels")
    if isinstance(labels, dict):
        _fsi = labels.get(FORK_SOURCE_LABEL_KEY)
        if isinstance(_fsi, str) and _fsi:
            fork_source_id = _fsi
        _fse = labels.get(FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY)
        if isinstance(_fse, str) and _fse:
            fork_source_external_id = _fse
        fork_carry_history = labels.get(FORK_CARRY_HISTORY_LABEL_KEY) == "1"
    return _CodexNativeLaunchConfig(
        workspace=_codex_session_workspace(session_workspace),
        policy_server_url=_required_runner_env("RUNNER_SERVER_URL"),
        terminal_launch_args=terminal_launch_args,
        model_override=model_override,
        external_session_id=external_session_id,
        fork_source_id=fork_source_id,
        fork_source_external_id=fork_source_external_id,
        fork_carry_history=fork_carry_history,
    )

async def _auto_create_codex_terminal(
    session_id: str,
    resource_registry: SessionResourceRegistry,
    publish_event: Callable[[str, dict[str, Any]], None],
    *,
    bundle_dir: Path | None = None,
    skills_filter: str | list[str] = "all",
    agent_spec: AgentSpec | ResolvedSpec | None = None,
    server_client: httpx.AsyncClient | None = None,
    ensure_comment_relay: Callable[..., Awaitable[None]] | None = None,
) -> SessionResourceView:
    """
    Auto-create a Codex terminal for a codex-native session.

    Called when the runner receives a codex-native session via
    ``POST /v1/sessions`` or an explicit terminal ensure request and no
    terminal exists yet. Mirrors :func:`_auto_create_claude_terminal`: it
    boots a Codex app-server, registers the Codex TUI as a streamable
    terminal resource attached to that app-server, then runs the transcript
    forwarder so the chat and terminal share one thread.

    Fresh sessions launch without a thread id so the TUI owns thread
    creation; resume sessions launch with the persisted Codex thread id.
    The runner does not pre-create a thread, because ``codex resume`` of a
    thread with no rollout yet exits the TUI (leaving a dead pane).

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param resource_registry: Session resource registry used to launch
        the Codex terminal resource.
    :param publish_event: The runner's per-session SSE emitter, used to
        surface the new terminal on the live stream (the Omnigent relay
        republishes it to the web UI) so the Terminal toggle enables
        without a refresh.
    :param bundle_dir: Materialized agent-bundle root when the session's
        agent ships a ``skills/`` directory, resolved by the caller
        (which has the runner's spec resolver). Its skills are linked
        into the per-bridge ``$CODEX_HOME/skills/`` before the
        app-server boots so the native Codex discovers them — matching
        the wrapped ``codex`` executor. ``None`` exposes no bundle skills.
    :param skills_filter: The agent spec's ``skills_filter`` (``"all"``
        / ``"none"`` / list of skill names), honoured when populating
        ``$CODEX_HOME/skills/``. Defaults to ``"all"``.
    :param agent_spec: Optional resolved agent spec for the session.
        When provided, its executor model is used as the Codex app-server
        default, e.g. ``"gpt-5.4-mini"``.
    :param server_client: Runner's Omnigent server HTTP client. Used to read
        persisted launch args and the native thread id.
    :returns: The created terminal resource view.
    """
    import socket as _socket
    from pathlib import Path

    from omnigent.codex_native_app_server import (
        CodexAppServerClient,
        build_codex_native_server,
        build_codex_remote_args,
        codex_session_meta_model_provider,
        codex_terminal_env,
        preload_codex_thread_for_resume,
        resolve_native_codex_launch,
    )
    from omnigent.codex_native_bridge import (
        clear_bridge_state,
        codex_home_for_bridge_dir,
        prepare_bridge_dir,
        socket_path_for_bridge_dir,
    )
    from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec

    launch_config = await _codex_native_launch_config(
        session_id=session_id,
        server_client=server_client,
    )
    original_external_session_id = launch_config.external_session_id
    workspace = str(launch_config.workspace)
    bridge_dir = prepare_bridge_dir(session_id)
    socket_path = socket_path_for_bridge_dir(bridge_dir)
    codex_home = codex_home_for_bridge_dir(bridge_dir)
    # Route across all offerings: a configured provider (omnigent setup),
    # a Databricks ucode profile from provider config, or Codex's own
    # login — parity with the in-process codex harness and the CLI path.
    # Resolved before the fork/cold-resume branches below so any rollout
    # synthesis can stamp session_meta.model_provider with the provider
    # this launch actually routes through.
    default_model = launch_config.model_override or _codex_native_model_from_spec(agent_spec)
    _codex_launch = resolve_native_codex_launch(model=default_model)
    _session_meta_provider = codex_session_meta_model_provider(_codex_launch)
    from omnigent.inner.codex_executor import _find_codex_cli

    _codex_cli_path = _find_codex_cli()
    # Cancel any surviving forwarder first so its teardown closes the OLD app-server,
    # not the one registered below — and so it can't mirror alongside the new one.
    await _cancel_auto_forwarder_task(session_id)
    clear_bridge_state(bridge_dir)

    # Forked clone with no native thread of its own yet: clone the SOURCE's
    # local Codex rollout into the clone's OWN CODEX_HOME under a thread id
    # we mint (rewriting session_meta.id + the structural cwd fields), then
    # flip launch_config so the normal resume path below launches
    # ``codex resume <our_thread_id>``. The app-server boots from this
    # CODEX_HOME just below, so the rollout must be written first. Only
    # viable when the source rollout exists on THIS host (same-host fork —
    # CUJ 1 same-user); else fall through and launch fresh. This mirrors the
    # claude-native fork-resume branch in _auto_create_claude_terminal. See
    # designs/FORK_SESSION_UX.md.
    if (
        launch_config.external_session_id is None
        and launch_config.fork_source_external_id is not None
        and launch_config.fork_source_id is not None
    ):
        from omnigent.codex_native import _clone_codex_rollout, _mint_codex_thread_id

        target_thread_id = _mint_codex_thread_id()
        clone_workspace = Path(workspace).resolve()
        try:
            cloned_rollout = _clone_codex_rollout(
                source_session_id=launch_config.fork_source_id,
                source_thread_id=launch_config.fork_source_external_id,
                target_thread_id=target_thread_id,
                clone_codex_home=codex_home,
                clone_workspace=clone_workspace,
            )
        except Exception:  # noqa: BLE001 — best-effort; launch fresh on failure
            cloned_rollout = None
            _logger.warning(
                "Could not clone source rollout for forked codex clone %s; launching fresh",
                session_id,
                exc_info=True,
            )
        _logger.info(
            "Codex terminal fork-resume decision: session=%s source_id=%s source_ext=%s "
            "our_thread=%s clone_workspace=%s cloned_rollout=%s",
            session_id,
            launch_config.fork_source_id,
            launch_config.fork_source_external_id,
            target_thread_id,
            clone_workspace,
            str(cloned_rollout) if cloned_rollout is not None else None,
        )
        if cloned_rollout is not None:
            # Resume our OWN clone via the existing resume path below.
            launch_config = dataclasses.replace(
                launch_config, external_session_id=target_thread_id
            )
            # Record the assigned thread id now so Omnigent reflects the clone's
            # own Codex thread immediately and a later relaunch resumes it.
            # Best-effort, like the claude-native fork branch.
            if server_client is not None:
                try:
                    await server_client.patch(
                        f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
                        json={"external_session_id": target_thread_id},
                        timeout=10.0,
                    )
                except httpx.HTTPError:
                    # The clone resumes via the known-thread forwarder (no
                    # discovery), so nothing re-captures the id later: it stays
                    # unset on the Omnigent session and a future relaunch of this
                    # clone will start fresh rather than resume the cloned
                    # rollout. The cloned rollout itself is already on disk, so
                    # the current launch still resumes with history.
                    _logger.warning(
                        "Could not pre-set external_session_id for forked codex clone %s; "
                        "it will remain unset and a future relaunch will start fresh",
                        session_id,
                        exc_info=True,
                    )
    elif (
        launch_config.external_session_id is None
        and launch_config.fork_carry_history
        and launch_config.fork_source_external_id is None
        and server_client is not None
    ):
        # Forked clone bound to a codex-native target with NO source
        # rollout to clone (an SDK or cross-family source): build the clone's
        # rollout from its OWN copied Omnigent items under a thread id we mint, then flip
        # launch_config so the resume path below launches ``codex resume
        # <our_thread_id>``. Reuses the same server-items→rollout converter
        # the cross-machine cold resume uses, so the clone opens with the
        # prior conversation (messages + tool history) as Codex context.
        # Best-effort: launch fresh on failure. See designs/FORK_SESSION_UX.md.
        from omnigent.codex_native import (
            _ensure_local_codex_resume_rollout,
            _mint_codex_thread_id,
        )

        target_thread_id = _mint_codex_thread_id()
        clone_workspace = Path(workspace).resolve()
        try:
            built_rollout = await _ensure_local_codex_resume_rollout(
                server_client,
                session_id=session_id,
                external_session_id=target_thread_id,
                codex_home=codex_home,
                workspace=clone_workspace,
                model_provider=_session_meta_provider,
                codex_path=_codex_cli_path,
            )
        except Exception:  # noqa: BLE001 — best-effort; launch fresh on failure
            built_rollout = None
            _logger.warning(
                "Could not build rollout from items for forked codex clone %s; launching fresh",
                session_id,
                exc_info=True,
            )
        _logger.info(
            "Codex terminal fork-rebuild decision: session=%s our_thread=%s "
            "clone_workspace=%s built_rollout=%s",
            session_id,
            target_thread_id,
            clone_workspace,
            str(built_rollout) if built_rollout is not None else None,
        )
        if built_rollout is not None:
            launch_config = dataclasses.replace(
                launch_config, external_session_id=target_thread_id
            )
            try:
                await server_client.patch(
                    f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
                    json={"external_session_id": target_thread_id},
                    timeout=10.0,
                )
            except httpx.HTTPError:
                _logger.warning(
                    "Could not pre-set external_session_id for forked codex clone %s; "
                    "it will remain unset and a future relaunch will start fresh",
                    session_id,
                    exc_info=True,
                )

    if launch_config.external_session_id is not None and original_external_session_id is not None:
        from omnigent.codex_native import _ensure_local_codex_resume_rollout

        if server_client is None:
            raise RuntimeError("server_client is required for Codex cold resume.")
        await _ensure_local_codex_resume_rollout(
            server_client,
            session_id=session_id,
            external_session_id=launch_config.external_session_id,
            codex_home=codex_home,
            workspace=Path(workspace).resolve(),
            model_provider=_session_meta_provider,
            codex_path=_codex_cli_path,
        )
    # Link the bundle's skills into the per-bridge CODEX_HOME before the
    # app-server boots — Codex discovers ``$CODEX_HOME/skills/<name>/``
    # at startup. This is the codex-native mirror of the wrapped codex
    # executor's skill population; the native CLI otherwise sees zero
    # bundled skills. Best-effort: a skill-link failure must not break
    # the terminal launch.
    from omnigent.inner.codex_executor import populate_codex_skills_from_bundle

    try:
        populate_codex_skills_from_bundle(codex_home, bundle_dir, skills_filter)
    except OSError:
        _logger.warning(
            "Could not populate codex skills for %s; native Codex will see no bundled skills",
            session_id,
            exc_info=True,
        )

    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        codex_ws_port = s.getsockname()[1]
    codex_ws_url = f"ws://127.0.0.1:{codex_ws_port}"

    # Write the minimal MCP bridge config so serve-mcp can boot, and
    # start the tool relay so tool_relay.json is on disk before codex
    # launches its MCP server. This mirrors the claude-native relay
    # start in ``create_session_terminal``. The relay is started here
    # (not in ``_ensure_comment_relay_started``) because that helper
    # is scoped inside ``create_routes`` and not reachable at module
    # level. The ``_run_turn_bg`` fallback path covers sessions whose
    # terminal was created outside this function.
    from omnigent.codex_native_bridge import (
        codex_mcp_config_overrides,
        write_mcp_bridge_config,
    )

    write_mcp_bridge_config(bridge_dir)
    mcp_overrides = codex_mcp_config_overrides(bridge_dir)

    # Omnigent coordinates for the codex-native policy hook. The hook runs as a
    # separate subprocess that POSTs tool calls to /policies/evaluate, so
    # it reads a one-shot token snapshot from policy_hook.json — same as
    # the claude-native PermissionRequest hook on this host-spawned path.
    from omnigent.runner._entry import _make_auth_token_factory

    _policy_auth_factory = _make_auth_token_factory()
    _policy_auth_token = _policy_auth_factory() if _policy_auth_factory is not None else None
    policy_headers = (
        {"Authorization": f"Bearer {_policy_auth_token}"} if _policy_auth_token else {}
    )

    app_server = build_codex_native_server(
        socket_path=socket_path,
        codex_home=codex_home,
        cwd=Path(workspace),
        model=_codex_launch.model,
        profile=_codex_launch.profile,
        extra_config_overrides=[*_codex_launch.config_overrides, *mcp_overrides],
        bridge_dir=bridge_dir,
        ap_server_url=launch_config.policy_server_url,
        ap_auth_headers=policy_headers,
    )
    app_server.listen_url = codex_ws_url
    await app_server.start()
    _AUTO_CODEX_APP_SERVERS[session_id] = app_server

    event_client = CodexAppServerClient(
        ws_url=codex_ws_url,
        client_name="omnigent-codex-native-auto",
    )
    if launch_config.external_session_id is None:
        try:
            # Connect the listener BEFORE launching the TUI so it observes the
            # ``thread/started`` the TUI emits on startup (the client buffers
            # notifications, so there is no created-before-listening race).
            await event_client.connect()
        except Exception:
            # connect() may have half-opened the ws before the initialize
            # handshake failed, so close the listener too — not just the
            # app-server.
            with contextlib.suppress(Exception):
                await event_client.close()
            await app_server.close()
            _AUTO_CODEX_APP_SERVERS.pop(session_id, None)
            raise
    else:
        from omnigent.codex_native_bridge import CodexNativeBridgeState, write_bridge_state

        await preload_codex_thread_for_resume(codex_ws_url, launch_config.external_session_id)
        write_bridge_state(
            bridge_dir,
            CodexNativeBridgeState(
                session_id=session_id,
                socket_path=codex_ws_url,
                thread_id=launch_config.external_session_id,
                codex_home=str(codex_home),
            ),
        )

    # Register the Codex TUI as a streamable terminal resource attached to
    # the app-server started above (``--remote`` over its loopback ws
    # endpoint). Without this the session can have a working chat path
    # (driven by the forwarder) but no terminal to attach to, unlike
    # claude-native, whose terminal IS the agent process. On failure, close
    # the listener and app-server here: the background forwarder task (which
    # otherwise owns their teardown) has not been created yet.
    try:
        terminal_view = await resource_registry.launch_auxiliary_terminal(
            session_id=session_id,
            terminal_name="codex",
            session_key="main",
            resource_role=CODEX_NATIVE_TERMINAL_ROLE,
            spec=TerminalEnvSpec(
                os_env=OSEnvSpec(type="caller_process", cwd=workspace),
                command=app_server.codex_path,
                # Fresh sessions pass no thread id so the TUI creates the
                # thread and the background task adopts it. Resume sessions
                # pass the persisted external_session_id so the runner-owned
                # TUI reopens the existing app-server thread.
                args=build_codex_remote_args(
                    codex_args=tuple(launch_config.terminal_launch_args or ()),
                    thread_id=launch_config.external_session_id,
                    remote_url=codex_ws_url,
                    # The --remote TUI loads its own config and does not
                    # inherit the app-server's -c flags; pass the same
                    # provider/model overrides so it resolves the
                    # Omnigent provider instead of falling back to the
                    # OpenAI built-in (which would force the first-run
                    # login screen and block thread creation).
                    config_overrides=tuple(app_server.config_overrides),
                ),
                env=codex_terminal_env(app_server),
                # Match the local ``omnigent codex`` terminal scrollback.
                scrollback=100_000,
                # Enable tmux passthrough so the Codex TUI's escape sequences
                # reach the web xterm.
                tmux_allow_passthrough=True,
                # Start the TUI at creation rather than on first attach,
                # mirroring claude-native. Deferring to attach (the local CLI
                # default) means the full-screen TUI cold-starts the instant
                # the web UI attaches over the runner tunnel; that initial
                # render burst starves the tunnel ping/pong and the host
                # recycles the unresponsive runner (the "runner
                # death on terminal attach" class). Starting now lets the TUI settle
                # in the detached tmux pane (no tunnel traffic) and create its
                # thread before anyone attaches.
                tmux_start_on_attach=False,
            ),
        )
        publish_event(
            session_id,
            {
                "type": "session.resource.created",
                "resource": session_resource_view_to_dict(terminal_view),
            },
        )
    except Exception:
        await event_client.close()
        await app_server.close()
        _AUTO_CODEX_APP_SERVERS.pop(session_id, None)
        raise

    # Adopt the thread the fresh TUI creates and run the forwarder in the
    # background, so session creation never blocks on TUI startup.
    _forwarder_task = asyncio.create_task(
        (
            _codex_discover_thread_and_forward(
                session_id=session_id,
                bridge_dir=bridge_dir,
                codex_ws_url=codex_ws_url,
                codex_home=codex_home,
                event_client=event_client,
            )
            if launch_config.external_session_id is None
            else _codex_forward_known_thread(
                session_id=session_id,
                bridge_dir=bridge_dir,
                codex_ws_url=codex_ws_url,
                thread_id=launch_config.external_session_id,
            )
        ),
        name=f"codex-forwarder-{session_id}",
    )
    _register_auto_forwarder_task(session_id, _forwarder_task)

    # Start the relay now (into codex's serve-mcp bridge dir) so tool_relay.json
    # is on disk and the relay recorded before codex connects on its first turn:
    # the first-turn `_ensure_comment_relay_started` then fast-paths, avoiding
    # the ~30s stall (see its docstring for the lazy-bridge / await_notify=False
    # rationale).
    if ensure_comment_relay is not None:
        await ensure_comment_relay(session_id, explicit_bridge_dir=bridge_dir, await_notify=False)

    _logger.info(
        "Auto-created codex terminal + forwarder for session %s",
        session_id,
    )
    return terminal_view

async def _codex_discover_thread_and_forward(
    *,
    session_id: str,
    bridge_dir: Path,
    codex_ws_url: str,
    codex_home: Path,
    event_client: CodexAppServerClient,
) -> None:
    """
    Adopt the fresh Codex TUI's thread, then mirror it into the Omnigent session.

    Runs as a background task spawned by :func:`_auto_create_codex_terminal`
    so session creation never blocks on TUI startup. Waits for the fresh TUI
    to create its app-server thread, persists the bridge state (so the Codex
    executor's bridge-state retry can inject web-UI turns into that same
    thread), then runs the transcript forwarder for the session's lifetime.

    :param session_id: Omnigent session/conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory for this session.
    :param codex_ws_url: App-server loopback ws URL the TUI and forwarder
        attach to, e.g. ``"ws://127.0.0.1:9876"``. Persisted as the bridge
        state's ``socket_path`` (the executor reads it to reach the
        app-server) and re-persisted by the forwarder's thread-rotation
        path so a native ``/clear`` keeps the ws:// transport.
    :param codex_home: Per-session private ``CODEX_HOME`` path.
    :param event_client: Connected app-server listener that will observe the
        TUI's ``thread/started``; reused to subscribe the forwarder.
    """
    from omnigent.codex_native_bridge import (
        CodexNativeBridgeState,
        write_bridge_state,
    )
    from omnigent.codex_native_forwarder import (
        supervise_forwarder,
        wait_for_thread_started,
    )
    from omnigent.runner._entry import (
        _make_auth_token_factory,
        _RunnerDatabricksAuth,
    )

    try:
        try:
            thread_id = await wait_for_thread_started(event_client)
        except (TimeoutError, RuntimeError):
            # Expected failure modes of wait_for_thread_started: the TUI exited
            # at startup, or the event stream ended before a thread was
            # created. Stop forwarding (cleanup runs in ``finally``); any other
            # error is a bug and propagates.
            _logger.exception(
                "Codex TUI never started a thread for %s; chat will not forward",
                session_id,
            )
            return

        write_bridge_state(
            bridge_dir,
            CodexNativeBridgeState(
                session_id=session_id,
                socket_path=codex_ws_url,
                thread_id=thread_id,
                codex_home=str(codex_home),
            ),
        )

        server_url = _required_runner_env("RUNNER_SERVER_URL")
        auth_factory = _make_auth_token_factory()
        auth_token = auth_factory() if auth_factory is not None else None
        headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}

        # Mirror the discovered Codex thread id onto the Omnigent session as its
        # external_session_id, the same way claude-native records its
        # captured session id. This is what makes the session forkable with
        # history: fork_conversation stamps
        # ``omnigent.fork.source_external_session_id`` from
        # external_session_id, and the forked clone's runner clones this
        # thread's rollout from it (see _clone_codex_rollout). Without it a
        # host-spawned codex session has no recorded thread id, so a fork
        # would resume fresh. Best-effort: a transient Omnigent failure here still
        # leaves chat streaming working — only fork-history carry-over
        # degrades.
        try:
            async with httpx.AsyncClient(
                base_url=server_url,
                headers=headers,
                auth=_RunnerDatabricksAuth(auth_factory),
                timeout=httpx.Timeout(10.0),
            ) as _ext_client:
                _ext_resp = await _ext_client.patch(
                    f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
                    json={"external_session_id": thread_id},
                )
            if _ext_resp.status_code >= 400:
                _logger.warning(
                    "AP rejected codex external_session_id PATCH (%s); session=%s thread=%s — "
                    "a fork of this session will resume fresh",
                    _ext_resp.status_code,
                    session_id,
                    thread_id,
                )
        except httpx.HTTPError:
            _logger.warning(
                "Could not record codex external_session_id for %s; a fork of this "
                "session will resume fresh",
                session_id,
                exc_info=True,
            )

        await supervise_forwarder(
            base_url=server_url,
            headers=headers,
            session_id=session_id,
            bridge_dir=bridge_dir,
            app_server_url=codex_ws_url,
            thread_id=thread_id,
            client=event_client,
            auth=_RunnerDatabricksAuth(auth_factory),
        )
    finally:
        # Tear down the listener and the per-session app-server whenever
        # forwarding ends — discovery failed, the app-server connection dropped
        # (``supervise_forwarder`` returned), or the task was cancelled on
        # session teardown. ``supervise_forwarder`` also closes ``event_client``
        # in its own ``finally``; ``close()`` is idempotent. The app-server
        # subprocess is ours to stop, else it orphans one process per session.
        # Pop first so the dict never holds a closed reference.
        leftover_app_server = _AUTO_CODEX_APP_SERVERS.pop(session_id, None)
        with contextlib.suppress(Exception):
            await event_client.close()
        if leftover_app_server is not None:
            with contextlib.suppress(Exception):
                await leftover_app_server.close()

async def _codex_forward_known_thread(
    *,
    session_id: str,
    bridge_dir: Path,
    codex_ws_url: str,
    thread_id: str,
) -> None:
    """
    Forward a runner-owned Codex terminal that resumes an existing thread.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory for this session.
    :param codex_ws_url: App-server loopback URL, e.g.
        ``"ws://127.0.0.1:9876"``.
    :param thread_id: Existing Codex app-server thread id, e.g.
        ``"thread_abc123"``.
    :returns: None. Runs until cancelled or the app-server connection
        closes.
    """
    from omnigent.codex_native_forwarder import supervise_forwarder
    from omnigent.runner._entry import (
        _make_auth_token_factory,
        _RunnerDatabricksAuth,
    )

    server_url = _required_runner_env("RUNNER_SERVER_URL")
    auth_factory = _make_auth_token_factory()
    auth_token = auth_factory() if auth_factory is not None else None
    headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
    try:
        await supervise_forwarder(
            base_url=server_url,
            headers=headers,
            session_id=session_id,
            bridge_dir=bridge_dir,
            app_server_url=codex_ws_url,
            thread_id=thread_id,
            auth=_RunnerDatabricksAuth(auth_factory),
        )
    finally:
        leftover_app_server = _AUTO_CODEX_APP_SERVERS.pop(session_id, None)
        if leftover_app_server is not None:
            with contextlib.suppress(Exception):
                await leftover_app_server.close()

async def _codex_session_needs_runner_terminal(
    server_client: httpx.AsyncClient | None,
    session_id: str,
) -> bool:
    """
    Whether the runner must auto-create the Codex terminal for a session.

    The runner owns the terminal for every codex-native session, including
    top-level CLI sessions. Older top-level CLI sessions used to run their
    own app-server/TUI/forwarder; that split ownership caused competing
    setup and teardown. Now all codex-native sessions need runner
    auto-create:

    - **Host-spawned (web-UI) top-level sessions** carry a ``host_id``.
    - **Sub-agent children** (dispatched server-side via
      ``sys_session_send``) carry a ``parent_session_id`` but no
      ``host_id`` of their own. No CLI ever manages a sub-agent terminal,
      so the runner must create it regardless of whether the *parent* was
      host- or CLI-spawned. (Gating on the parent's ``host_id`` was a
      regression: codex-native sub-agents under a CLI-driven parent —
      e.g. polly run via ``omnigent run --server`` — silently never got
      a terminal and the dispatch no-op'd.)

    - **CLI top-level sessions** have neither ``host_id`` nor
      ``parent_session_id`` but still need the runner to own the app-server
      and terminal.

    Returns ``False`` only when the lookup fails; without a session
    snapshot, the runner cannot confirm this is a codex-native session.

    :param server_client: The runner's Omnigent server HTTP client, or ``None`` in
        embedded/test setups.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :returns: ``True`` when the session snapshot exists; ``False`` on
        lookup failure.
    """
    payload = await _session_payload_for_host_spawn_check(server_client, session_id)
    if payload is None:
        return False
    return True

def _codex_native_model_from_spec(agent_spec: AgentSpec | ResolvedSpec | None) -> str | None:
    """
    Read the Codex model default from a resolved agent spec.

    :param agent_spec: Agent spec object, or a resolved wrapper carrying a
        ``spec`` attribute. ``None`` means no spec was available.
    :returns: Model id, e.g. ``"gpt-5.4-mini"``, or ``None``.
    """
    spec = agent_spec.spec if isinstance(agent_spec, ResolvedSpec) else agent_spec
    if spec is None:
        return None
    model = spec.executor.config.get("model")
    return model if isinstance(model, str) and model else None

def _is_runner_owned_codex_terminal(
    resource_registry: SessionResourceRegistry,
    resource: SessionResourceView,
) -> bool:
    """
    Return whether an existing ``codex/main`` terminal is the native TUI.

    A generic terminal launched with ``terminal=codex`` has the same public
    resource id but is not the runner-owned Codex TUI. The resource registry
    carries the private role marker that identifies terminals created by
    ``_auto_create_codex_terminal`` without leaking launch argv in public
    metadata.

    :param resource_registry: Runner resource registry that owns private
        terminal role markers.
    :param resource: Existing terminal resource view.
    :returns: ``True`` when the resource is marked as Codex native.
    """
    return (
        resource_registry.terminal_resource_role(resource.session_id, resource.id)
        == CODEX_NATIVE_TERMINAL_ROLE
    )

def _codex_ensure_response_with_policy_notice(
    session_id: str, terminal_view: SessionResourceView
) -> JSONResponse:
    """
    Build the codex terminal-ensure 200 response with a one-shot notice.

    When the codex app-server degraded to "no policy enforcement"
    (fail-open — codex too old or trust failed), attach the reason as
    ``policy_hook_disabled_reason`` exactly once so Omnigent can post a single
    durable web-UI banner. The app-server's one-shot flag is cleared
    after the first surface, so repeated ensures (each user message
    re-probes) do not re-post the notice.

    Must be called while holding the per-session codex ensure lock
    (``_codex_terminal_ensure_locks[session_id]``): the read-and-clear of
    ``policy_notice_pending`` is only one-shot because that lock
    serializes concurrent ensures for the same session.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param terminal_view: The runner-owned codex terminal resource view
        to return.
    :returns: A 200 JSON response, optionally carrying
        ``policy_hook_disabled_reason``.
    """
    body = session_resource_view_to_dict(terminal_view)
    app_server = _AUTO_CODEX_APP_SERVERS.get(session_id)
    if (
        app_server is not None
        and app_server.policy_notice_pending
        and app_server.policy_hook_disabled_reason
    ):
        body["policy_hook_disabled_reason"] = app_server.policy_hook_disabled_reason
        app_server.policy_notice_pending = False
    return JSONResponse(status_code=200, content=body)
