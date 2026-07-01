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

def _build_claude_native_base_args(
    *,
    reasoning_effort: str | None,
    model_override: str | None,
    terminal_launch_args: list[str] | None,
    resume_external_session_id: str | None = None,
) -> tuple[str, ...]:
    """
    Assemble the base ``claude`` CLI args for a native-terminal launch.

    These are the args before :func:`augment_claude_args` layers on the
    bridge / MCP / hook / Omnigent wiring. The order is: ``--resume`` for a
    cold resume, then persisted reasoning effort, then the user's
    pass-through ``terminal_launch_args``, then a ``--model`` derived
    from ``model_override`` — appended only when the user did not
    already pass an explicit ``--model``. That precedence (explicit
    ``--model`` in pass-through args wins over ``model_override``)
    mirrors the CLI's ``_merge_default_model_arg``, moved runner-side.
    The ``--resume``-first ordering mirrors the CLI's
    ``(*cold_resume_args, *claude_args)``. See
    designs/NATIVE_RUNNER_SERVER_LAUNCH.md.

    :param reasoning_effort: Persisted per-session effort, e.g.
        ``"high"``. Added as ``--effort <value>`` only when it is one
        of Claude's supported efforts; otherwise ignored. ``None``
        adds nothing (Claude uses its own ``~/.claude/settings.json``
        default).
    :param model_override: Per-session model override, e.g.
        ``"claude-opus-4-7"``. Appended as ``--model <value>`` unless
        the pass-through args already contain a ``--model`` flag.
        ``None`` adds nothing.
    :param terminal_launch_args: The user's pass-through CLI args,
        e.g. ``["--dangerously-skip-permissions"]``. ``None`` or an
        empty list contributes nothing.
    :param resume_external_session_id: Claude-native session id to
        resume, e.g. ``"02857840-6362-408f-b41f-309e396ed7c6"``.
        Prepended as ``--resume <value>`` so Claude reopens the prior
        transcript. A forked clone passes the uuid it assigned to its
        OWN cloned transcript here (see
        :func:`omnigent.claude_native._clone_claude_transcript`), so
        the same plain ``--resume`` path serves both cold resume and
        fork resume. ``None`` (a fresh launch, or no local transcript
        could be synthesized) adds nothing.
    :returns: The assembled base args, e.g.
        ``("--resume", "<sid>", "--effort", "high")``.
    """
    from omnigent.reasoning_effort import CLAUDE_EFFORTS

    args: list[str] = []
    if resume_external_session_id:
        args.extend(("--resume", resume_external_session_id))
    if reasoning_effort is not None and reasoning_effort in CLAUDE_EFFORTS:
        args.extend(("--effort", reasoning_effort))
    if terminal_launch_args:
        args.extend(terminal_launch_args)
    # model_override is a default: it applies only when the user did
    # not pass their own ``--model`` (in either the long ``--model X``
    # or the joined ``--model=X`` form).
    if model_override and not any(arg == "--model" or arg.startswith("--model=") for arg in args):
        args.extend(("--model", model_override))
    return tuple(args)

async def _auto_create_claude_terminal(
    session_id: str,
    resource_registry: SessionResourceRegistry,
    publish_event: Callable[[str, dict[str, Any]], None],
    *,
    server_client: httpx.AsyncClient,
    bundle_dir: Path | None = None,
    agent_name: str | None = None,
    skills_filter: str | list[str] = "all",
) -> SessionResourceView:
    """
    Auto-create a Claude Code terminal for a claude-native session.

    Called when the runner receives a claude-native session via
    ``POST /v1/sessions`` and no terminal exists yet. This handles
    the host-spawned runner case where no CLI client is present to
    create the terminal.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param resource_registry: Session resource registry for
        launching the terminal.
    :param publish_event: The runner's per-session SSE emitter, used to
        surface the new terminal on the live stream (the Omnigent relay
        republishes it to the web UI) so the Terminal toggle enables
        without a refresh.
    :param server_client: Omnigent server client used to fetch the session
        snapshot so the terminal inherits the persisted
        ``reasoning_effort``.
    :param bundle_dir: Materialized agent-bundle root when the session's
        agent ships a ``skills/`` directory, resolved by the caller
        (which has the runner's spec resolver). Threaded to
        :func:`augment_claude_args` so Claude Code discovers bundled
        skills via ``--plugin-dir``. ``None`` adds no plugin args.
    :param agent_name: Agent display name for the bundle's plugin
        manifest, e.g. ``"researcher"``. ``None`` falls back to the
        bundle directory's basename.
    :param skills_filter: The agent spec's ``skills_filter`` (``"all"``
        / ``"none"`` / list of skill names), threaded to
        :func:`augment_claude_args`. Defaults to ``"all"``.
    :returns: The launched terminal's :class:`SessionResourceView`, so
        callers that create it on demand (the resume "ensure" path in
        :func:`create_session_terminal`) can return the resource.
    """
    from pathlib import Path

    from omnigent.claude_native_bridge import (
        BRIDGE_ID_LABEL_KEY,
        ensure_claude_workspace_trusted,
        prepare_bridge_dir,
    )
    from omnigent.claude_native_forwarder import reset_transcript_forward_state
    from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec

    workspace = os.environ.get("OMNIGENT_RUNNER_WORKSPACE", str(Path.cwd()))
    started_at = time.monotonic()
    _logger.info(
        "Claude terminal auto-create starting: session=%s workspace=%s bundle_dir=%s "
        "agent_name=%s skills_filter=%s",
        session_id,
        workspace,
        bundle_dir,
        agent_name,
        skills_filter,
    )
    # prepare_bridge_dir uses session_id as the bridge_id (no explicit
    # bridge_id passed), so the bridge dir is keyed by session_id.  If the
    # Omnigent session carries a stale bridge_id label from a prior rotation that
    # timed out before the terminal transfer completed, _ensure_comment_relay_started
    # would read the label and write tool_relay.json to the wrong directory —
    # the bridge subprocess would never see it and the relay tools would be absent.
    # Correcting the label here ensures all subsequent label lookups return
    # session_id, which matches the actual bridge dir.
    try:
        await server_client.patch(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
            json={"labels": {BRIDGE_ID_LABEL_KEY: session_id}},
        )
    except httpx.HTTPError:
        _logger.debug(
            "Could not reset bridge_id label for %s; relay may target wrong dir",
            session_id,
        )
    bridge_dir = prepare_bridge_dir(session_id, workspace=Path(workspace))
    # Cancel any surviving forwarder BEFORE wiping its cursor/seen state, else it
    # re-posts with fresh dedup state alongside the forwarder spawned below.
    await _cancel_auto_forwarder_task(session_id)
    reset_transcript_forward_state(bridge_dir)
    _logger.info(
        "Claude terminal bridge prepared: session=%s bridge_dir=%s",
        session_id,
        bridge_dir,
    )
    # Pre-accept Claude's first-run trust + onboarding TUI prompts for this
    # workspace. They have no PermissionRequest hook, so on a host-spawned
    # (web-UI-driven) session they would hang Claude in its terminal with
    # nothing shown in the UI. Acute with per-session worktrees,
    # which launch Claude in a brand-new, untrusted directory.
    ensure_claude_workspace_trusted(Path(workspace))

    from omnigent.runner._entry import _make_auth_token_factory, _RunnerDatabricksAuth

    # The Omnigent server URL + auth are needed in two places below: the
    # PermissionRequest hook (so Claude's approval prompts route to the
    # web UI instead of its TUI) and the transcript forwarder. The CLI
    # client supplies these on the wrapper path; on this host-spawned
    # path the runner reconstructs them from its own environment/auth.
    server_url = os.environ.get("RUNNER_SERVER_URL", "http://localhost:6767")
    # Authenticate the runner's outbound POSTs the same way its other
    # HTTP calls are authenticated.
    _auth_factory = _make_auth_token_factory()
    # The PermissionRequest hook runs in a separate subprocess that reads
    # static headers from permission_hook.json, so it gets a one-shot
    # token snapshot. The long-running transcript forwarder instead gets
    # a refresh-capable ``httpx.Auth`` (below) so it survives the ~1h
    # Databricks OAuth token expiry; a one-shot header would silently
    # stop forwarding after the token lapses. ``_RunnerDatabricksAuth``
    # with a ``None`` factory is a safe no-op (local unauthenticated).
    _auth_token = _auth_factory() if _auth_factory is not None else None
    _runner_headers = {"Authorization": f"Bearer {_auth_token}"} if _auth_token else {}
    _runner_auth = _RunnerDatabricksAuth(_auth_factory)

    from omnigent.claude_native import (
        ClaudeNativeUcodeConfig,
        augment_claude_args,
        build_native_claude_terminal_env,
        resolve_native_claude_config,
    )

    # Fetch the session's persisted launch config (reasoning_effort,
    # model_override, terminal_launch_args) so a web-UI / daemon-spawned
    # launch honours the same flags the CLI would have passed. Best-effort
    # — a failed lookup means Claude starts at its settings.json defaults
    # with no extra args. See designs/NATIVE_RUNNER_SERVER_LAUNCH.md.
    from omnigent.stores.conversation_store import (
        FORK_CARRY_HISTORY_LABEL_KEY,
        FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
    )

    session_effort: str | None = None
    session_model_override: str | None = None
    session_launch_args: list[str] | None = None
    session_external_id: str | None = None
    # Source native session id stamped on a forked clone (one-shot): when
    # the clone has no native session of its own yet, resume + branch the
    # source's local transcript so it opens with prior history.
    fork_source_external_id: str | None = None
    # Set on a forked clone bound to a native target: when no source
    # native transcript exists to clone (an SDK or cross-family source),
    # build the clone's native transcript from the copied Omnigent items
    # instead (see FORK_CARRY_HISTORY_LABEL_KEY / native_replay design notes).
    fork_carry_history: bool = False
    if server_client is not None:
        try:
            _resp = await server_client.get(
                f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
                timeout=10.0,
            )
            if _resp.status_code == 200:
                _snap = _resp.json()
                _re = _snap.get("reasoning_effort")
                if isinstance(_re, str) and _re:
                    session_effort = _re
                _mo = _snap.get("model_override")
                if isinstance(_mo, str) and _mo:
                    session_model_override = _mo
                _tla = _snap.get("terminal_launch_args")
                if isinstance(_tla, list) and all(isinstance(a, str) for a in _tla):
                    session_launch_args = _tla
                _ext = _snap.get("external_session_id")
                if isinstance(_ext, str) and _ext:
                    session_external_id = _ext
                _labels = _snap.get("labels")
                if isinstance(_labels, dict):
                    _fse = _labels.get(FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY)
                    if isinstance(_fse, str) and _fse:
                        fork_source_external_id = _fse
                    fork_carry_history = _labels.get(FORK_CARRY_HISTORY_LABEL_KEY) == "1"
            _logger.info(
                "Claude terminal launch config fetched: session=%s status=%s "
                "effort_set=%s model_override_set=%s launch_args_count=%d "
                "external_session_id_set=%s",
                session_id,
                _resp.status_code,
                session_effort is not None,
                session_model_override is not None,
                len(session_launch_args or []),
                session_external_id is not None,
            )
        except httpx.HTTPError:
            _logger.debug(
                "Could not fetch session launch config for %s; terminal will "
                "use Claude's defaults",
                session_id,
            )

    # Cold resume: when this session wraps a prior Claude session,
    # synthesize the local ``~/.claude/projects/<workspace>/<sid>.jsonl``
    # transcript that Claude's ``--resume`` reads, then pass ``--resume``.
    # The CLI does this client-side via ``_resolve_cold_resume_args``;
    # doing it here lets a daemon / web-UI launch resume too. Best-effort:
    # on any failure we launch fresh rather than point ``--resume`` at a
    # transcript that doesn't exist. See
    # designs/NATIVE_RUNNER_SERVER_LAUNCH.md.
    resume_external_session_id: str | None = None
    if server_client is not None and session_external_id is not None:
        from omnigent.claude_native import _ensure_local_claude_resume_transcript

        try:
            _transcript = await _ensure_local_claude_resume_transcript(
                server_client,
                session_id=session_id,
                external_session_id=session_external_id,
                workspace=Path(workspace).resolve(),
            )
            if _transcript is not None:
                resume_external_session_id = session_external_id
        except Exception:  # noqa: BLE001 — best-effort; launch fresh on failure
            _logger.warning(
                "Could not synthesize Claude resume transcript for %s; launching without --resume",
                session_id,
                exc_info=True,
            )
    elif session_external_id is None and fork_source_external_id is not None:
        # Forked clone with no native session yet: clone the SOURCE's
        # local Claude transcript into the clone's OWN project dir under a
        # uuid we assign — rewriting per-record sessionId/cwd — then launch
        # plain ``--resume <our_uuid>``. Writing the file ourselves before
        # launch means the forwarder's ``start_at_end`` seeks past the
        # copied prefix (no double-render), and placing it in the clone's
        # own project dir means cwd-scoped ``--resume`` finds it in any
        # dir/worktree. Only viable when the source transcript exists on
        # THIS host (same-host fork — CUJ 1 same-user); else launch fresh.
        # See designs/FORK_SESSION_UX.md.
        from omnigent.claude_native import _clone_claude_transcript

        our_uuid = str(uuid.uuid4())
        _clone_workspace = Path(workspace).resolve()
        try:
            _cloned = _clone_claude_transcript(
                source_external_session_id=fork_source_external_id,
                target_external_session_id=our_uuid,
                clone_workspace=_clone_workspace,
            )
        except Exception:  # noqa: BLE001 — best-effort; launch fresh on failure
            _cloned = None
            _logger.warning(
                "Could not clone source transcript for forked clone %s; launching fresh",
                session_id,
                exc_info=True,
            )
        _logger.info(
            "Claude terminal fork-resume decision: session=%s source_ext=%s "
            "our_uuid=%s clone_workspace=%s cloned_transcript=%s",
            session_id,
            fork_source_external_id,
            our_uuid,
            _clone_workspace,
            str(_cloned) if _cloned is not None else None,
        )
        if _cloned is not None:
            # Resume our OWN clone (plain --resume, no --fork-session).
            resume_external_session_id = our_uuid
            # Record the assigned id now so Omnigent reflects the clone's own
            # Claude session immediately, and a later relaunch resumes it
            # via the normal cold-resume path (this branch is gated on
            # external_session_id being unset). Best-effort.
            if server_client is not None:
                try:
                    await server_client.patch(
                        f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
                        json={"external_session_id": our_uuid},
                        timeout=10.0,
                    )
                except httpx.HTTPError:
                    _logger.warning(
                        "Could not pre-set external_session_id for forked clone %s; "
                        "relying on hook capture",
                        session_id,
                        exc_info=True,
                    )
    elif (
        server_client is not None
        and fork_carry_history
        and session_external_id is None
        and fork_source_external_id is None
    ):
        # Forked clone bound to a native target with NO source native
        # transcript to clone (an SDK or cross-family source): build the clone's
        # native transcript from its OWN copied Omnigent items under a uuid we
        # assign, then launch plain ``--resume <our_uuid>``. This reuses the
        # same server-items→transcript converter the cross-machine cold
        # resume path uses (``_ensure_local_claude_resume_transcript``), so
        # the clone opens with the prior conversation (messages + tool
        # history) as real Claude context. Best-effort: launch fresh on
        # failure. See designs/FORK_SESSION_UX.md.
        from omnigent.claude_native import _ensure_local_claude_resume_transcript

        our_uuid = str(uuid.uuid4())
        _clone_workspace = Path(workspace).resolve()
        try:
            _built = await _ensure_local_claude_resume_transcript(
                server_client,
                session_id=session_id,
                external_session_id=our_uuid,
                workspace=_clone_workspace,
            )
        except Exception:  # noqa: BLE001 — best-effort; launch fresh on failure
            _built = None
            _logger.warning(
                "Could not build native transcript from items for forked clone %s; "
                "launching fresh",
                session_id,
                exc_info=True,
            )
        _logger.info(
            "Claude terminal fork-rebuild decision: session=%s our_uuid=%s "
            "clone_workspace=%s built_transcript=%s",
            session_id,
            our_uuid,
            _clone_workspace,
            str(_built) if _built is not None else None,
        )
        if _built is not None:
            resume_external_session_id = our_uuid
            # Record the assigned id so Omnigent reflects the clone's own Claude
            # session and a later relaunch resumes it via the cold-resume
            # path above. Best-effort, mirroring the clone branch.
            try:
                await server_client.patch(
                    f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
                    json={"external_session_id": our_uuid},
                    timeout=10.0,
                )
            except httpx.HTTPError:
                _logger.warning(
                    "Could not pre-set external_session_id for forked clone %s; "
                    "relying on hook capture",
                    session_id,
                    exc_info=True,
                )
    _logger.info(
        "Claude terminal cold-resume decision: session=%s external_session_id_set=%s "
        "fork_source_set=%s resume_enabled=%s",
        session_id,
        session_external_id is not None,
        fork_source_external_id is not None,
        resume_external_session_id is not None,
    )

    # Derive the ucode (Databricks gateway) launch config from the
    # runner's own profile so a daemon / web-UI-launched Claude
    # authenticates to the gateway exactly like a CLI-launched one —
    # the CLI injects this in ``_claude_terminal_request``; on this path
    # the runner must, since it (not the CLI) launches the terminal.
    # Best-effort: no profile / no ucode state / malformed state falls
    # back to Claude's own native config (empty env). The runner env is
    # an allowlist that excludes ``ANTHROPIC_API_KEY`` /
    # ``CLAUDE_CODE_*``, so — unlike the CLI — there are no stray
    # provider/session vars to unset before the gateway env applies.
    # See designs/NATIVE_RUNNER_SERVER_LAUNCH.md.
    # Resolve the launch config across all offerings — a configured provider
    # (omnigent setup), a Databricks ucode profile from provider config, or
    # Claude's own login — so a host-spawned native-claude session honors the
    # provider selection just like the in-process claude-sdk harness and the
    # CLI path.
    claude_config: ClaudeNativeUcodeConfig | None = None
    try:
        claude_config = resolve_native_claude_config(spec=None)
    except Exception:  # noqa: BLE001 — best-effort; fall back to native auth
        _logger.warning(
            "native-claude: could not derive a provider/ucode launch config "
            "— FALLING BACK to Claude Code's own login; "
            "your configured provider will NOT be used. Check "
            "`omnigent setup --no-internal-beta` "
            "and that the secret resolves in this process.",
            exc_info=True,
        )
    _logger.info(
        "Claude terminal provider config resolved: session=%s configured=%s "
        "env_keys=%s api_key_helper_set=%s model_set=%s",
        session_id,
        claude_config is not None,
        sorted(claude_config.env) if claude_config is not None else [],
        bool(claude_config.api_key_helper) if claude_config is not None else False,
        bool(claude_config.model) if claude_config is not None else False,
    )

    base_claude_args = _build_claude_native_base_args(
        reasoning_effort=session_effort,
        # Session override wins; the ucode gateway model is the default
        # when no per-session override is set. Both yield to an explicit
        # ``--model`` in the user's pass-through args (handled in the
        # helper).
        model_override=session_model_override
        or (claude_config.model if claude_config is not None else None),
        terminal_launch_args=session_launch_args,
        resume_external_session_id=resume_external_session_id,
    )

    # Pass ``ap_server_url`` so ``build_hook_settings`` registers the
    # claude-native ``PermissionRequest`` command hook and writes
    # permission_hook.json. Without it, the hook is silently omitted and
    # approval prompts never reach the web UI on this host-spawned path.
    # ``bundle_dir`` / ``skills_filter`` (resolved by the caller, which
    # has the spec resolver) expose a bundle's ``skills/`` to Claude Code
    # via ``--plugin-dir`` — the CLI mirror of the SDK plugin wiring.
    # ``api_key_helper`` (ucode) registers Claude's gateway token command.
    claude_args = augment_claude_args(
        base_claude_args,
        bridge_dir=bridge_dir,
        ap_server_url=server_url,
        ap_auth_headers=_runner_headers,
        bundle_dir=bundle_dir,
        agent_name=agent_name,
        skills_filter=skills_filter,
        api_key_helper=claude_config.api_key_helper if claude_config is not None else None,
    )

    env_spec = TerminalEnvSpec(
        os_env=OSEnvSpec(type="caller_process", cwd=workspace),
        command="claude",
        args=list(claude_args),
        # Tool Search env plus ucode gateway env (ANTHROPIC_BASE_URL
        # etc.) when derived. Empty provider config still forces
        # ENABLE_TOOL_SEARCH=true so MCP schemas are loaded on demand.
        env=build_native_claude_terminal_env(claude_config),
        # Strip the ambient Databricks-SDK profile selection from
        # the Claude tmux env. Claude's MCP servers inherit this env,
        # and several construct ``WorkspaceClient`` without pinning
        # ``auth_type``; when ``DATABRICKS_CONFIG_PROFILE`` is set,
        # the SDK's auth resolver picks up that profile's cached
        # OAuth token and ignores the explicit token the MCP was
        # configured with — sending a bearer minted for the wrong
        # workspace and getting back a 400 ``Invalid Token`` from
        # the right one. Claude itself doesn't read this env var
        # (provider routing is via ``ANTHROPIC_BASE_URL`` /
        # ``apiKeyHelper``), so dropping it from the terminal env
        # affects only the leak path. MCPs that genuinely need a
        # specific profile must declare it in their own per-MCP env
        # configuration rather than inheriting it from the runner.
        env_unset=["DATABRICKS_CONFIG_PROFILE"],
        scrollback=50000,
    )
    _logger.info(
        "Claude terminal tmux launch requested: session=%s command=%s args_count=%d "
        "env_keys=%s cwd=%s scrollback=%d",
        session_id,
        env_spec.command,
        len(env_spec.args),
        sorted(env_spec.env),
        workspace,
        env_spec.scrollback,
    )
    try:
        terminal_view = await resource_registry.launch_required_terminal(
            session_id=session_id,
            terminal_name="claude",
            session_key="main",
            spec=env_spec,
            # Mark this as the claude-native agent terminal so its pane
            # activity drives the session's PTY-derived working status.
            resource_role=CLAUDE_NATIVE_TERMINAL_ROLE,
        )
    except Exception:
        _logger.exception(
            "Claude terminal tmux launch failed: session=%s elapsed_ms=%.0f",
            session_id,
            (time.monotonic() - started_at) * 1000,
        )
        raise
    # Surface the terminal on the live SSE stream so an already-connected
    # web UI enables the Terminal toggle immediately. The required-terminal
    # launch helper registers the resource and starts the activity watcher but
    # does not publish; the tool / REST launch paths emit this same event via
    # _emit_terminal_resource_event. Without it, this auto-created terminal
    # is only discovered on reconnect (snapshot-on-connect), so the toggle
    # stays gray until the user refreshes.
    from omnigent.entities.session_resources import session_resource_view_to_dict

    terminal_payload = session_resource_view_to_dict(terminal_view)
    terminal_metadata = terminal_payload.get("metadata")
    if not isinstance(terminal_metadata, dict):
        terminal_metadata = {}
    _logger.info(
        "Claude terminal tmux launch returned: session=%s terminal_id=%s running=%s "
        "tmux_socket=%s tmux_target=%s elapsed_ms=%.0f",
        session_id,
        terminal_payload.get("id"),
        terminal_metadata.get("running"),
        terminal_metadata.get("tmux_socket"),
        terminal_metadata.get("tmux_target"),
        (time.monotonic() - started_at) * 1000,
    )

    publish_event(
        session_id,
        {
            "type": "session.resource.created",
            "resource": terminal_payload,
        },
    )
    _publish_tmux_target_for_bridge(
        resource_registry=resource_registry,
        session_id=session_id,
        # The bridge dir was created via ``prepare_bridge_dir(session_id)``
        # above (no explicit bridge_id), so it is keyed by session_id.
        # Pass the same id so the tmux target lands in that dir and the
        # claude-native harness can find it.
        bridge_id=session_id,
        terminal_name="claude",
        session_key="main",
    )
    _logger.info(
        "Claude terminal tmux target published: session=%s bridge_id=%s",
        session_id,
        session_id,
    )

    # Start the transcript forwarder so Claude's responses flow
    # back to the Omnigent server. Normally the CLI client runs this,
    # but for host-spawned sessions there is no CLI. Reuses the
    # ``server_url`` + auth computed above; ``auth`` refreshes the
    # bearer token per request so forwarding outlives token expiry.
    #
    # ``start_at_end`` must be ``True`` on resume: when
    # ``resume_external_session_id`` is set we launched Claude with
    # ``--resume`` over a transcript synthesized from AP's committed
    # history (see ``_ensure_local_claude_resume_transcript`` above), so
    # offset 0 already holds every item Omnigent has. Starting the forwarder at
    # offset 0 would re-post the whole transcript as new external
    # conversation items — there is no server-side dedup — duplicating the
    # visible history on every resume. A genuinely fresh
    # session (no ``--resume``) starts with an empty transcript, so
    # ``False`` correctly forwards everything from the beginning. This
    # mirrors the CLI client's ``prepared.cold_resumed`` handling in
    # ``claude_native.py``.
    from omnigent.claude_native_forwarder import supervise_forwarder

    _forwarder_task = asyncio.create_task(
        supervise_forwarder(
            base_url=server_url,
            headers=_runner_headers,
            session_id=session_id,
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=resume_external_session_id is not None,
            auth=_runner_auth,
        ),
        name=f"claude-forwarder-{session_id}",
    )
    _register_auto_forwarder_task(session_id, _forwarder_task)
    _logger.info(
        "Auto-created claude terminal + forwarder for session %s; "
        "forwarder_task=%s elapsed_ms=%.0f",
        session_id,
        _forwarder_task.get_name(),
        (time.monotonic() - started_at) * 1000,
    )
    return terminal_view

async def _claude_native_bridge_id_for_session(
    *,
    server_client: httpx.AsyncClient,
    session_id: str,
) -> str:
    """Resolve the bridge id label for a Claude-native session.

    :param server_client: Omnigent server client used to fetch the session
        snapshot.
    :param session_id: Omnigent session/conversation id, e.g.
        ``"conv_abc123"``.
    :returns: Opaque bridge id from
        ``omnigent.claude_native.bridge_id`` when present, otherwise
        *session_id* for legacy single-session bridges.
    """
    from omnigent.claude_native_bridge import BRIDGE_ID_LABEL_KEY

    labels = await _session_labels_for_runner_spawn(
        server_client=server_client,
        session_id=session_id,
    )
    bridge_id = labels.get(BRIDGE_ID_LABEL_KEY)
    if isinstance(bridge_id, str) and bridge_id:
        return bridge_id
    return session_id

async def _claude_native_session_wants_rebuild(
    server_client: httpx.AsyncClient | None,
    session_id: str,
) -> bool:
    """
    Return whether a claude-native session is pending a post-switch rebuild.

    An in-place agent switch into claude-native clears the session's
    ``external_session_id`` and stamps the carry-history label, so the next
    launch must re-synthesize the Claude transcript from the CURRENT AP items.
    But when the session was ALREADY claude-native before the switch, its
    original terminal can still be registered (an open terminal tab keeps it
    alive). The auto-create that performs the re-synthesis is skipped while a
    terminal exists, so the switched-back agent keeps its original on-disk
    transcript — missing the turns added on the other agent. Detecting this
    lets the caller tear the stale terminal down first. A normal resume
    (``external_session_id`` already set) returns ``False`` so its terminal is
    left untouched.

    :param server_client: AP client; ``None`` can't confirm, returns ``False``.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :returns: ``True`` when ``external_session_id`` is unset AND the
        carry-history label is set (a pending rebuild), else ``False``.
    """
    if server_client is None:
        return False
    from omnigent.stores.conversation_store import FORK_CARRY_HISTORY_LABEL_KEY

    try:
        resp = await server_client.get(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
            timeout=10.0,
        )
    except httpx.HTTPError:
        return False
    if resp.status_code != 200:
        return False
    snap = resp.json()
    # A captured native session means this is a normal resume, not a switch.
    if snap.get("external_session_id"):
        return False
    labels = snap.get("labels")
    return isinstance(labels, dict) and labels.get(FORK_CARRY_HISTORY_LABEL_KEY) == "1"

async def _claude_native_terminal_arrives_via_transfer(
    *,
    server_client: httpx.AsyncClient | None,
    session_id: str,
    resource_registry: SessionResourceRegistry,
) -> bool:
    """
    Return whether a live Claude terminal will be transferred into a session.

    A ``/clear`` / ``/fork`` rotation binds the runner to a fresh session
    before transferring the existing terminal onto it; auto-creating a
    second Claude here would 409 the transfer and loop the rotation
    (rotation loop). The shared-bridge ``active_session_id`` still names the
    live terminal-owning session at bind time, detected here so the
    caller skips auto-create and lets the transfer deliver the terminal.

    :param server_client: Omnigent client to resolve the bridge id label;
        ``None`` can't confirm a rotation, so returns ``False``.
    :param session_id: Newly-bound session id, e.g. ``"conv_new"``.
    :param resource_registry: Registry probed for the original session's
        live ``claude:main`` terminal.
    :returns: ``True`` when a different session on the same bridge owns a
        live ``claude:main`` terminal (transfer inbound), else ``False``.
    """
    terminal_registry = resource_registry.terminal_registry
    if terminal_registry is None:
        return False
    # Lazy import keeps claude-native out of the generic runner import graph.
    from omnigent.claude_native_bridge import (
        bridge_dir_for_bridge_id,
        read_active_session_id,
    )

    bridge_id = await _claude_native_bridge_id_for_session(
        server_client=server_client,
        session_id=session_id,
    )
    active_session_id = read_active_session_id(bridge_dir_for_bridge_id(bridge_id))
    # Fresh bridge, or the new session is already active — nothing transfers in.
    if active_session_id is None or active_session_id == session_id:
        return False
    return terminal_registry.get(active_session_id, "claude", "main") is not None

