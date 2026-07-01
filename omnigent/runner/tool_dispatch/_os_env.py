"""Runner-local tool dispatch for intercepted action_required events.

Per designs/RUNNER_TOOL_DISPATCH.md, the runner dispatches most tools
locally and relays action_required events upstream UNCHANGED for
visibility (the executor emits ToolCallInProgress/ToolCallObserved for
the REPL but doesn't dispatch itself — it checks should_dispatch_locally
and skips).

Tool categories:
- _OS_ENV_TOOLS: execute through a runner-local OSEnvironment (sys_os_*)
- _REST_TOOLS: call server REST APIs (sys_call_async, sys_cancel_async)
- _FILE_TOOLS: call server file APIs (sys_upload/download/list_files)
- _TERMINAL_TOOLS: runner-local TerminalRegistry
- MCP tools: spec-defined; dispatched via RunnerMcpManager passed
  in by proxy_stream (designs/RUNNER_MCP.md). Not in the static
  allow-list because names vary per spec.
- Client-side tools: tunneled via REPL (deferred)
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, cast

if TYPE_CHECKING:
    from omnigent.identity.identity import ActingIdentity
    from omnigent.runner.mcp_manager import McpManager
    from omnigent.runner.resource_registry import SessionResourceRegistry
    from omnigent.runtime.filesystem_registry import FilesystemRegistry
    from omnigent.spec.types import AgentSpec
    from omnigent.terminals.registry import TerminalRegistry

import httpx

from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE,
    CODEX_NATIVE_WRAPPER_VALUE,
)
from omnigent.model_override import (
    harness_supports_model_override,
    model_family_mismatch,
    normalize_model_for_provider,
    validate_model_override,
)
from omnigent.runner.subagent_status import (
    _ACTIVE as _SUBAGENT_ACTIVE_STATUSES,
)
from omnigent.runner.subagent_status import (
    SubagentWorkStatus,
)
from omnigent.runner.tool_execution_context import ToolExecutionContext
from omnigent.runtime import pending_elicitations
from omnigent.session_lifecycle import (
    CLOSED_LABEL_KEY,
    CLOSED_LABEL_VALUE,
    is_session_closed,
    title_without_closed_marker,
)
from omnigent.tools import ToolManager
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.async_inbox import (
    SysCallAsyncTool,
    SysCancelAsyncTool,
    SysCancelTaskTool,
    SysReadInboxTool,
)
from omnigent.tools.builtins.download_file import DownloadFileTool
from omnigent.tools.builtins.list_comments import ListCommentsTool
from omnigent.tools.builtins.os_env import (
    SysOsEditTool,
    SysOsReadTool,
    SysOsShellTool,
    SysOsWriteTool,
)
from omnigent.tools.builtins.spawn import (
    # Shared contract values with the in-process sys_session_* tools. Imported
    # (not duplicated) so the runner's REST-backed peek clamps to the same
    # bounds the LLM-facing tool schema advertises and tombstones with the
    # same marker the in-process close writes.
    _ACTIVITY_MAX_CHARS,
    _CLOSED_TITLE_INFIX,
    _HISTORY_DEFAULT_TAIL,
    _clamp_tail_items,
)
from omnigent.tools.builtins.sys_terminal import (
    SysTerminalCloseTool,
    SysTerminalLaunchTool,
    SysTerminalListTool,
    SysTerminalReadTool,
    SysTerminalSendTool,
)
from omnigent.tools.builtins.update_comment import UpdateCommentTool
from omnigent.tools.builtins.upload_file import UploadFileTool, safe_resolve

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

def _clone_os_env_spec(spec: Any) -> Any:
    """Return a defensive copy of an OSEnvSpec-like object.

    Uses :func:`dataclasses.replace` so any field added to
    :class:`OSEnvSandboxSpec` or :class:`OSEnvSpec` in the future is
    carried over automatically. Mutable list fields are copied
    explicitly so the clone and the original don't alias the same
    list (which would let one caller's later mutation leak into the
    other's view — a real hazard when the same parent spec is reused
    across many runner-local sys_os_* dispatches).

    Symmetric with :func:`omnigent.inner.terminal._clone_sandbox_spec`;
    both fixes close the same class of bug where hand-enumerated
    field copies silently drop newly-added security-critical fields
    such as ``egress_rules`` and ``egress_allow_private_destinations``.
    """
    sandbox = getattr(spec, "sandbox", None)
    sandbox_copy = None
    if sandbox is not None:
        sandbox_copy = dataclasses.replace(
            sandbox,
            read_paths=list(sandbox.read_paths) if sandbox.read_paths is not None else None,
            write_paths=list(sandbox.write_paths) if sandbox.write_paths is not None else None,
            write_files=list(sandbox.write_files) if sandbox.write_files is not None else None,
            cwd_allow_hidden=(
                list(sandbox.cwd_allow_hidden) if sandbox.cwd_allow_hidden is not None else None
            ),
            env_passthrough=(
                list(sandbox.env_passthrough) if sandbox.env_passthrough is not None else None
            ),
            egress_rules=list(sandbox.egress_rules) if sandbox.egress_rules is not None else None,
        )
    return dataclasses.replace(spec, sandbox=sandbox_copy)

def _runner_default_os_env_cwd(conversation_id: str | None) -> str:
    """Return the cwd for a default runner-owned primary OSEnv."""
    safe_conv = "default"
    if conversation_id:
        safe_conv = "".join(
            ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in conversation_id
        )
    root = Path(
        os.environ.get(
            "OMNIGENT_RUNNER_OS_ENV_ROOT",
            str(Path(tempfile.gettempdir()) / "omnigent-runner-os-envs"),
        )
    )
    cwd = root / safe_conv / "workspace"
    cwd.mkdir(parents=True, exist_ok=True)
    return str(cwd)

def _effective_runner_os_env_spec(
    agent_spec: AgentSpecLike | None,
    conversation_id: str | None,
    runner_workspace: Path | None = None,
) -> Any:
    """
    Build the OSEnvSpec used by runner-local sys_os_* dispatch.

    Precedence (per
    designs/SESSION_WORKSPACE_SELECTION.md "How this maps onto runtime"):

    - When ``runner_workspace`` is set, it ALWAYS wins — whether
      the spec's cwd is relative, absolute, or unset. The runner
      workspace is the authoritative starting cwd for both
      CLI-launched sessions (CLI captures ``os.getcwd()`` and
      passes it via ``OMNIGENT_RUNNER_WORKSPACE``) and
      host-launched sessions (host applies the validated picked
      directory). The agent's spec ``cwd`` is treated as a
      boundary at session-create time, not a runtime override.
    - When ``runner_workspace`` is unset (pure local runs without
      the env var), the spec's cwd applies, with placeholder
      values (``.``, ``./``, ``""``, ``None``) substituted by
      a per-conversation tmpdir as before.

    :param agent_spec: Agent spec resolved for the current turn, or
        ``None`` when dispatch only has request-body hints.
    :param conversation_id: Conversation id used to derive the
        per-conversation fallback workspace, e.g. ``"conv_123"``.
    :param runner_workspace: Authoritative runtime cwd for the
        runner, sourced from ``OMNIGENT_RUNNER_WORKSPACE``.
        Overrides the spec's cwd when set.
    :returns: An ``OSEnvSpec`` with a concrete cwd.
    """
    from omnigent.inner.datamodel import OSEnvSpec

    configured = getattr(agent_spec, "os_env", None) if agent_spec is not None else None
    if configured is not None:
        spec = _clone_os_env_spec(configured)
        if runner_workspace is not None:
            # Runner workspace is authoritative — overrides whatever
            # the spec declared (relative or absolute).
            spec.cwd = str(runner_workspace)
        elif spec.cwd in _PLACEHOLDER_CWDS:
            # No runner workspace; spec is relative — fall back to
            # the per-conversation tmpdir so multiple sessions
            # don't collide on a shared default cwd.
            spec.cwd = _runner_default_os_env_cwd(conversation_id)
        return spec
    cwd = (
        str(runner_workspace)
        if runner_workspace is not None
        else _runner_default_os_env_cwd(conversation_id)
    )
    return OSEnvSpec(type="caller_process", cwd=cwd)

async def _seed_os_env_snapshot(
    os_env: Any,
    path: str,
    filesystem_registry: FilesystemRegistry,
    conversation_id: str,
) -> None:
    """Seed the diff snapshot with *path*'s current content before a write or edit.

    Reads the file via *os_env* and passes the content to
    :meth:`~omnigent.runtime.filesystem_registry.FilesystemRegistry.seed_snapshot`
    so the before/after diff endpoint can show the original content.
    Silently skips when the file does not yet exist (new-file creates have no
    baseline) or when any other read error occurs.

    :param os_env: The :class:`~omnigent.inner.os_env.OSEnvironment` used for
        the current tool dispatch — reused to avoid opening a second connection.
    :param path: Path argument forwarded from the tool call, e.g. ``"src/foo.py"``.
    :param filesystem_registry: Registry that stores the snapshot.
    :param conversation_id: Session scope for the snapshot, e.g. ``"conv_abc123"``.
    """
    try:
        existing = await os_env.read(path=path, offset=1, limit=None)
        if isinstance(existing, dict) and "content" in existing:
            filesystem_registry.seed_snapshot(
                path, existing["content"], session_id=conversation_id
            )
    except Exception:  # noqa: BLE001
        pass  # file does not exist yet or unreadable — no baseline to capture

async def _execute_os_env_tool(
    tool_name: str,
    args: dict[str, Any],
    *,
    agent_spec: AgentSpecLike | None = None,
    conversation_id: str | None = None,
    runner_workspace: Path | None = None,
    filesystem_registry: FilesystemRegistry | None = None,
) -> str:
    """
    Execute sys_os_* through a runner-local OSEnvironment.

    :param tool_name: Built-in OS tool name, e.g. ``"sys_os_read"``.
    :param args: Parsed tool-call arguments.
    :param agent_spec: Agent spec resolved for the current turn, or
        ``None`` when unavailable.
    :param conversation_id: Conversation id used for the fallback
        workspace, e.g. ``"conv_123"``.
    :param runner_workspace: Optional CLI launch workspace used for
        placeholder cwd values in remote app sessions.
    :param filesystem_registry: Optional registry for tracking agent
        file modifications. When provided, ``sys_os_write`` and
        ``sys_os_edit`` calls record changed paths so the
        ``GET …/changes`` endpoint can surface them. ``sys_os_shell``
        is not tracked — shell side-effects cannot be attributed to a
        session.
    :returns: Serialized tool result string.
    """
    from omnigent.inner.os_env import _DEFAULT_READ_LIMIT, create_os_environment

    os_env = None
    try:
        os_env = create_os_environment(
            _effective_runner_os_env_spec(agent_spec, conversation_id, runner_workspace)
        )
        if os_env is None:
            return "Error: unable to create OSEnvironment"

        if tool_name == SysOsReadTool.name():
            result = await os_env.read(
                path=args.get("path", ""),
                offset=args.get("offset", 1),
                # Unspecified limit → agent-tool default (2 000 lines).
                # None is now "unlimited" in _read_impl, so we must be explicit.
                # Use is-None check (not `or`) so that invalid values like 0 are
                # forwarded to os_env.read for validation rather than silently
                # replaced with the default.
                limit=(lv if (lv := args.get("limit")) is not None else _DEFAULT_READ_LIMIT),
            )
        elif tool_name == SysOsWriteTool.name():
            _path = args.get("path", "")
            if filesystem_registry is not None and conversation_id is not None:
                await _seed_os_env_snapshot(os_env, _path, filesystem_registry, conversation_id)
            result = await os_env.write(path=_path, content=args.get("content", ""))
            if filesystem_registry is not None and conversation_id is not None:
                # _write_impl returns {"created": True} when the file did not
                # previously exist, {"created": False} for an overwrite.
                was_created = isinstance(result, dict) and result.get("created") is True
                status = "created" if was_created else "modified"
                filesystem_registry.record_change(_path, status, conversation_id)
        elif tool_name == SysOsEditTool.name():
            _path = args.get("path", "")
            if filesystem_registry is not None and conversation_id is not None:
                await _seed_os_env_snapshot(os_env, _path, filesystem_registry, conversation_id)
            result = await os_env.edit(
                path=_path,
                old_text=args.get("oldText") or args.get("old_string"),
                new_text=args.get("newText") or args.get("new_string"),
                edits=args.get("edits"),
            )
            if filesystem_registry is not None and conversation_id is not None:
                filesystem_registry.record_change(_path, "modified", conversation_id)
        elif tool_name == SysOsShellTool.name():
            result = await os_env.shell(
                command=args.get("command", ""),
                timeout=args.get("timeout"),
            )
        else:
            return f"Error: {tool_name} not implemented"
    except Exception as exc:
        _logger.exception("runner OSEnvironment dispatch failed for %s", tool_name)
        return json.dumps({"error": str(exc)})
    finally:
        if os_env is not None:
            os_env.close()

    return json.dumps(result)

