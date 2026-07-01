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

from ._os_env import _effective_runner_os_env_spec

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

@dataclass(frozen=True)
class _SubagentInboxEvaluation:
    """
    Result of delayed sub-agent output policy evaluation.

    :param payload: Payload safe to format for ``sys_read_inbox``.
        On fail-closed paths this contains a policy-failure sentinel
        instead of the raw child output.
    :param retry_original: Whether policy evaluation failed before
        producing a terminal verdict, so the original payload should
        be requeued for a future drain attempt.
    """

    payload: dict[str, Any]
    retry_original: bool = False

@dataclass(frozen=True)
class _SubagentLabel:
    """
    Human-facing identity fields for a child session.

    :param agent: Sub-agent tool name, e.g. ``"claude"``. ``None`` means the
        server row did not include a valid tool name.
    :param title: Child session title, e.g. ``"issue-1756"``. ``None`` means
        the server row did not include a valid session title.
    """

    agent: str | None
    title: str | None

def _subagent_label(child: dict[str, Any]) -> _SubagentLabel:
    """
    Extract child identity fields from a child-session summary.

    :param child: One object from
        ``GET /v1/sessions/{parent}/child_sessions``, e.g.
        ``{"tool": "claude", "session_name": "issue-1"}``.
    :returns: Named child identity fields.
    """
    agent = child.get("tool")
    title = child.get("session_name")
    return _SubagentLabel(
        agent=agent if isinstance(agent, str) and agent else None,
        title=title if isinstance(title, str) and title else None,
    )

def _publish_child_launching_update(
    *,
    parent_session_id: str,
    child_session_id: str,
    title: str,
    tool: str,
    session_name: str,
    publish_event: Callable[[str, dict[str, Any]], None] | None,
) -> None:
    """
    Publish the honest pre-start child state to the parent stream.

    The child session exists at this point, but no child runtime has emitted
    a busy edge yet. Surfacing ``launching`` prevents the UI/orchestrator from
    mistaking session bookkeeping for a running worker.
    """
    event = {
        "type": "session.child_session.updated",
        "conversation_id": parent_session_id,
        "child_session_id": child_session_id,
        "child": {
            "id": child_session_id,
            "title": title,
            "tool": tool,
            "session_name": session_name,
            "busy": False,
            "current_task_status": "launching",
        },
    }
    if publish_event is not None:
        publish_event(parent_session_id, event)
        return
    from omnigent.runtime import session_stream

    session_stream.publish(parent_session_id, event)

async def _list_child_sessions(
    *,
    server_client: httpx.AsyncClient,
    conversation_id: str,
    limit: int = 100,
) -> list[dict[str, Any]] | str:
    """
    Fetch child-session summaries for a parent session.

    :param server_client: Omnigent server client.
    :param conversation_id: Parent session id, e.g. ``"conv_parent123"``.
    :param limit: Maximum child rows to request, e.g. ``100``.
    :returns: List of child summary dicts, or an error string.
    """
    resp = await server_client.get(
        f"/v1/sessions/{conversation_id}/child_sessions",
        params={"limit": limit, "order": "desc"},
        timeout=30.0,
    )
    if resp.status_code >= 400:
        return f"Error: failed to list child sessions: {resp.status_code} {resp.text[:200]}"
    payload = resp.json()
    data = payload.get("data")
    if not isinstance(data, list):
        return "Error: server child_sessions response missing data list"
    return [item for item in data if isinstance(item, dict)]

async def _find_existing_child_session(
    *,
    server_client: httpx.AsyncClient,
    conversation_id: str,
    agent: str,
    title: str,
) -> dict[str, Any] | str | None:
    """
    Find an existing child session by ``(agent, title)``.

    ``sys_session_send`` promises that repeated sends to the same
    pair continue the existing child. The runner must therefore look
    up the row before trying to create a new one; otherwise the
    server's unique child-title constraint turns a continuation into
    a duplicate-create failure. This currently fetches up to 1000
    children and scans locally because the child-session endpoint does
    not provide a ``(tool, session_name)`` filter yet.

    :param server_client: Omnigent server client.
    :param conversation_id: Parent session id, e.g. ``"conv_parent123"``.
    :param agent: Sub-agent name, e.g. ``"claude"``.
    :param title: Caller-chosen child title, e.g. ``"issue-1756"``.
    :returns: Matching child summary, ``None`` when absent, or an error
        string when the server lookup failed.
    """
    children = await _list_child_sessions(
        server_client=server_client,
        conversation_id=conversation_id,
        limit=1000,
    )
    if isinstance(children, str):
        return children
    for child in children:
        if is_session_closed(child.get("labels"), child.get("title")):
            continue
        label = _subagent_label(child)
        if label.agent == agent and label.title == title:
            return child
    return None

def _subagent_message_from_args(args: SubagentSendArgs) -> str | None:
    """
    Extract the user message from ``sys_session_send`` arguments.

    The public ``SysSessionSendTool`` contract accepts ``args`` as a plain
    string. polly also sends an object with ``input`` plus metadata such as
    ``purpose`` so its guardrail can classify headless helper usage.

    :param args: Parsed ``sys_session_send`` arguments, e.g.
        ``{"args": "review this"}`` or
        ``{"args": {"input": "review this", "purpose": "review"}}``.
    :returns: Message text, or ``None`` when the payload is malformed.
    """
    raw_message = args.get("args")
    if isinstance(raw_message, dict):
        raw_input = raw_message.get("input")
        return raw_input if isinstance(raw_input, str) else None
    if isinstance(raw_message, str):
        return raw_message
    return None

def _subagent_model_from_args(args: SubagentSendArgs) -> str | None:
    """
    Extract and validate the per-dispatch model from ``sys_session_send`` args.

    The optional ``model`` field lives inside the object form of
    ``args`` (``{"input": ..., "model": ...}``). Malformed values fail
    loud instead of being silently dropped — the value later crosses
    the harness spawn boundary as a ``--model`` argv element.

    :param args: Parsed ``sys_session_send`` arguments, e.g.
        ``{"args": {"input": "fix the bug", "model": "claude-sonnet-4-6"}}``.
    :returns: The validated model id, or ``None`` when absent.
    :raises ValueError: If ``model`` is present but not a string, or
        fails :func:`validate_model_override`.
    """
    raw_message = args.get("args")
    if not isinstance(raw_message, dict):
        return None
    raw_model = raw_message.get("model")
    if raw_model is None:
        return None
    if not isinstance(raw_model, str):
        raise ValueError("'model' must be a string when provided")
    return validate_model_override(raw_model)

def _find_subagent_spec(sub_agent_name: str, agent_spec: AgentSpecLike | None) -> Any | None:
    """
    Look up a named sub-agent's spec in the parent's ``sub_agents`` list.

    :param sub_agent_name: Name of the sub-agent, e.g. ``"claude_code"``.
    :param agent_spec: Parent agent's spec. ``None`` when no spec is
        available.
    :returns: The sub-agent's spec (an :class:`AgentSpec` or structural
        equivalent), or ``None`` when absent.
    """
    if agent_spec is None:
        return None
    for sa in getattr(agent_spec, "sub_agents", None) or []:
        if getattr(sa, "name", None) == sub_agent_name:
            return sa
    return None

def _subagent_harness(sub_agent_name: str, agent_spec: AgentSpecLike | None) -> str | None:
    """
    Resolve the declared harness for a named sub-agent.

    Mirrors the harness derivation in the runner's
    ``_resolve_harness_config`` (``executor.config["harness"]`` falling
    back to ``executor.type``) for the AP-style ``sub_agents`` spec
    shape. Returns ``None`` when the sub-spec or its executor cannot be
    resolved — callers treat that as "unknown harness" and fail loud.

    :param sub_agent_name: Name of the sub-agent, e.g. ``"claude_code"``.
    :param agent_spec: Parent agent's spec. ``None`` when no spec is
        available.
    :returns: Harness id, e.g. ``"codex-native"``, or ``None``.
    """
    from omnigent.model_catalog import spec_harness

    sub_spec = _find_subagent_spec(sub_agent_name, agent_spec)
    return spec_harness(sub_spec) if sub_spec is not None else None

def _normalize_subagent_model(
    model: str,
    *,
    sub_agent_name: str,
    agent_spec: AgentSpecLike | None,
    harness: str | None,
) -> str:
    """
    Localize a per-dispatch model id for the child's resolved provider.

    Runs after the family guard (see
    :func:`omnigent.model_override.normalize_model_for_provider` for
    the ordering rationale): a canonical vendor id is prefixed with
    ``databricks-`` when the child routes through the Databricks
    gateway, and the prefix is stripped for a vendor-direct child. When
    the child's provider cannot be determined, the id passes through
    unchanged — the existing fail-loud harness error stays the net.

    :param model: The validated requested model id, e.g.
        ``"claude-sonnet-4-6"``.
    :param sub_agent_name: Name of the sub-agent being dispatched.
    :param agent_spec: Parent agent's spec. ``None`` skips normalization.
    :param harness: The child's declared harness, e.g. ``"claude-native"``.
    :returns: The id to persist as ``model_override``.
    """
    from omnigent.model_catalog import resolve_model_provider

    sub_spec = _find_subagent_spec(sub_agent_name, agent_spec)
    if sub_spec is None or harness is None:
        return model
    # resolve_model_provider is total — undeterminable providers come
    # back as kind "none", which normalize passes through.
    provider = resolve_model_provider(sub_spec, harness)
    normalized = normalize_model_for_provider(model, provider.kind)
    if normalized != model:
        _logger.info(
            "sys_session_send: localized model %r -> %r for sub-agent %r "
            "(harness %s, provider kind %s)",
            model,
            normalized,
            sub_agent_name,
            harness,
            provider.kind,
        )
    return normalized

async def _execute_subagent_tool(
    args: SubagentSendArgs,
    *,
    server_client: httpx.AsyncClient | None = None,
    conversation_id: str | None = None,
    agent_spec: AgentSpecLike | None = None,
    publish_event: Callable[[str, dict[str, Any]], None] | None = None,
    session_inbox: asyncio.Queue[dict[str, Any]] | None = None,
) -> str:
    """
    Dispatch a sub-agent tool call (``sys_session_send``).

    Creates or reuses a child session on the server, registers a
    runner-local launch entry, posts the child message, and returns a
    launching handle immediately. The child work becomes ``running`` only
    after the child runtime emits a real busy status. When it completes,
    runner turn-end bookkeeping pushes a completion payload into the
    parent's ``sys_read_inbox`` queue.

    :param args: Parsed arguments from the LLM. Expected keys:
        ``agent`` (sub-agent name, e.g. ``"researcher"``),
        ``args`` (user message text, or an object with ``input`` plus
        optional ``purpose`` / ``model`` dispatch metadata),
        ``title`` (instance label).
    :param server_client: httpx client pointed at the Omnigent server.
    :param conversation_id: Parent session/conversation ID,
        e.g. ``"conv_abc123"``.
    :param agent_spec: Parent agent's :class:`AgentSpec`. Used
        to resolve sub-agent name to ``agent_id``.
    :param publish_event: Optional callback for publishing child-session
        discovery events to the parent stream.
    :param session_inbox: Parent session's inbox queue for async
        completion delivery.
    :returns: JSON child-session handle, or an error string.
    """
    # Lazy import to avoid circular dependency at module load.
    from omnigent.runner import app as _runner_app

    message = _subagent_message_from_args(args)
    if message is None or not message.strip():
        return "Error: sys_session_send requires non-empty args string or args.input string"
    if server_client is None:
        return "Error: sys_session_send requires server_client"
    if conversation_id is None:
        return "Error: sys_session_send requires conversation_id"
    if session_inbox is not None:
        _runner_app._session_inboxes_ref.setdefault(conversation_id, session_inbox)
    elif conversation_id not in _runner_app._session_inboxes_ref:
        return "Error: sys_session_send requires parent session inbox"

    try:
        model = _subagent_model_from_args(args)
    except ValueError as exc:
        return f"Error: sys_session_send invalid 'model': {exc}"

    # By-session-id mode: post to an existing direct child instead of
    # spawning/continuing a named (agent, title) sub-agent.
    target_session_id = args.get("session_id")
    if isinstance(target_session_id, str) and target_session_id:
        # Fail loud on a double-addressed send. The two modes can point at
        # different children, so silently letting session_id win would
        # misroute the message with no signal to the caller.
        if args.get("agent") or args.get("title"):
            return (
                "Error: sys_session_send received both 'session_id' and "
                "'agent'/'title' — supply exactly one addressing mode"
            )
        if model is not None:
            return (
                "Error: sys_session_send 'model' applies only when a "
                "sub-agent session is first created; it cannot change an "
                "existing session. Re-send without 'model' to continue "
                f"session {target_session_id!r}."
            )
        return await _send_to_existing_session(
            target_session_id,
            message,
            server_client=server_client,
            conversation_id=conversation_id,
            publish_event=publish_event,
        )

    # Named mode: (agent, title) spawn-or-continue.
    sub_agent_name = args.get("agent")
    session_name = args.get("title")
    if not sub_agent_name:
        return "Error: sys_session_send requires 'agent' (or 'session_id')"
    if not session_name or not isinstance(session_name, str):
        return "Error: sys_session_send requires non-empty 'title' string"

    # Verify the sub-agent exists in the parent spec.
    if not _has_subagent(sub_agent_name, agent_spec):
        return f"Error: sub-agent {sub_agent_name!r} not found in agent spec"

    # Use the PARENT's agent_id — inline sub-agents are part of
    # the same bundle, not separately registered. The runner
    # resolves the sub-agent spec from the parent's sub_agents
    # list when it starts the child turn.
    # Try runner-local cache first, then fall back to server query.
    parent_agent_id = _runner_app.get_session_agent_id(conversation_id)
    if parent_agent_id is None:
        try:
            sess_resp = await server_client.get(
                f"/v1/sessions/{conversation_id}",
                timeout=10.0,
            )
            if sess_resp.status_code == 200:
                parent_agent_id = sess_resp.json().get("agent_id")
        except (httpx.HTTPError, RuntimeError):
            pass
    if parent_agent_id is None:
        return "Error: cannot resolve parent agent_id for sub-agent dispatch"

    existing = await _find_existing_child_session(
        server_client=server_client,
        conversation_id=conversation_id,
        agent=str(sub_agent_name),
        title=session_name,
    )
    if isinstance(existing, str):
        return existing
    created_child = False
    child_wrapper_label: str | None = None
    if existing is not None:
        child_session_id = existing.get("id")
        if not isinstance(child_session_id, str) or not child_session_id:
            return "Error: existing child session is missing id"
        if model is not None:
            # A native child bakes --model in at terminal launch, so a
            # mid-conversation override would be silently ignored there.
            return (
                f"Error: sys_session_send 'model' applies only when a "
                f"sub-agent session is first created; {sub_agent_name!r} "
                f"title {session_name!r} already exists as "
                f"{child_session_id}. Re-send without 'model' to continue "
                "it, or sys_session_close it first to spawn a fresh "
                "session on the requested model."
            )
        child_wrapper_label = _session_wrapper_label(existing)
        existing_work = _runner_app.get_subagent_work(child_session_id)
        if existing_work is not None and existing_work.status in _SUBAGENT_ACTIVE_STATUSES:
            return (
                f"Error: sub-agent {sub_agent_name!r} title {session_name!r} "
                "already has a launching or running turn; wait for completion before sending again"
            )
        if existing.get("busy") is True:
            return (
                f"Error: sub-agent {sub_agent_name!r} title {session_name!r} "
                "is already running; wait for completion before sending again"
            )
    else:
        child_harness = _subagent_harness(str(sub_agent_name), agent_spec)
        # Fail loud at dispatch when the child's harness needs a CLI binary
        # that isn't on PATH. Otherwise a missing CLI surfaces only as a lazy
        # first-turn failure (e.g. the pi harness raises ImportError, which the
        # parent sees as a generic "turn failed" inbox item that hides the
        # cause), and the orchestrator may re-dispatch into the same wall. The
        # which-probe here reads the same PATH the harness boot uses, so the
        # verdict can't disagree with the real launch.
        from omnigent.onboarding.harness_install import missing_harness_cli

        if child_harness is not None:
            missing_cli = missing_harness_cli(child_harness)
            if missing_cli is not None:
                # Non-npm CLIs (e.g. cursor-agent) carry an ``install_hint``
                # instead of a ``package``; using the hint avoids an
                # ``npm install -g None`` instruction.
                install = (
                    f"npm install -g {missing_cli.package}"
                    if missing_cli.package
                    else (missing_cli.install_hint or "see the harness's install docs")
                )
                return (
                    f"Error: sub-agent {sub_agent_name!r} can't start on this "
                    f"machine: harness {child_harness!r} needs the "
                    f"{missing_cli.binary!r} CLI on PATH, which was not found. "
                    f"Install it with: {install} "
                    f"(or don't dispatch to {sub_agent_name!r} here)."
                )
        # Create child session on the server (no initial items —
        # those go via a separate POST so the server forwards them
        # to the runner and triggers a turn).
        create_body: dict[str, Any] = {
            "agent_id": parent_agent_id,
            "parent_session_id": conversation_id,
            "title": f"{sub_agent_name}:{session_name}",
            "sub_agent_name": sub_agent_name,
        }
        if model is not None:
            # Reject up front when the child harness would silently
            # ignore the persisted override — no silent drops.
            if not harness_supports_model_override(child_harness):
                return (
                    f"Error: sys_session_send 'model' is not supported for "
                    f"sub-agent {sub_agent_name!r}: harness "
                    f"{child_harness or 'unknown'!r} has no model-override "
                    "plumbing. Omit 'model' to use the harness default."
                )
            mismatch = model_family_mismatch(child_harness, model) if child_harness else None
            if mismatch is not None:
                return (
                    f"Error: sys_session_send 'model' rejected for sub-agent "
                    f"{sub_agent_name!r}: {mismatch}"
                )
            # Family guard first (on the requested id, so the error
            # quotes what the caller sent), then mechanical
            # canonical<->gateway-local normalization. The normalized
            # id is what the server persists as model_override.
            create_body["model_override"] = _normalize_subagent_model(
                model,
                sub_agent_name=str(sub_agent_name),
                agent_spec=agent_spec,
                harness=child_harness,
            )
        resp = await server_client.post("/v1/sessions", json=create_body, timeout=30.0)
        if resp.status_code >= 400:
            return f"Error: failed to create child session: {resp.status_code} {resp.text[:200]}"
        child_data = resp.json()
        child_session_id = child_data.get("session_id") or child_data.get("id")
        if not child_session_id:
            return "Error: server did not return child session_id"
        child_wrapper_label = _session_wrapper_label(child_data)
        created_child = True

    # Publish session.created on the parent's SSE stream so the
    # REPL debug panel and any client subscribers discover the
    # child session. SSE-only (transient); durability comes from
    # the conversation_store row written by the server above.
    if not parent_agent_id:
        return f"Error: missing parent agent_id for child session {child_session_id}"
    from omnigent.server.schemas import SessionCreatedEvent

    if created_child:
        _evt = SessionCreatedEvent(
            type="session.created",
            conversation_id=conversation_id,
            child_session_id=child_session_id,
            agent_id=parent_agent_id,
            parent_session_id=conversation_id,
        )
        # Route through the runner's per-session queue, NOT session_stream
        # directly: in the out-of-process (--server) runner, session_stream
        # has no subscribers (they live in the Omnigent server), so a direct
        # publish here is silently dropped. ``publish_event`` enqueues onto
        # the parent's queue, which the Omnigent server's relay republishes onto
        # session_stream — the same channel terminals use. Falls back
        # to a direct publish only for in-process callers without a queue.
        if publish_event is not None:
            publish_event(conversation_id, _evt.model_dump())
        else:
            from omnigent.runtime import session_stream

            session_stream.publish(conversation_id, _evt.model_dump())

    # Register the child→parent mapping so the runner can fan out the
    # child's status/preview deltas onto the PARENT's stream (the child's
    # own relay isn't running when only the parent is being viewed). The
    # title/tool/session_name are known here (we set the title above), so
    # even a cold status update carries a display name. Cleaned up when
    # the child session ends.
    _runner_app.register_child_session(
        child_session_id,
        parent_session_id=conversation_id,
        title=f"{sub_agent_name}:{session_name}",
        tool=sub_agent_name,
        session_name=session_name,
    )
    _runner_app.register_subagent_work(
        parent_session_id=conversation_id,
        child_session_id=child_session_id,
        agent=str(sub_agent_name),
        title=session_name,
        wrapper_label=child_wrapper_label,
    )
    _publish_child_launching_update(
        parent_session_id=conversation_id,
        child_session_id=child_session_id,
        title=f"{sub_agent_name}:{session_name}",
        tool=str(sub_agent_name),
        session_name=session_name,
        publish_event=publish_event,
    )

    # Send the user message as a separate event so the server's
    # post_event forwards it to the runner and starts the child
    # turn.
    try:
        msg_resp = await _post_child_message_event(
            server_client=server_client,
            child_session_id=child_session_id,
            message=str(message),
        )
    except httpx.HTTPError as exc:
        _runner_app.unregister_child_session(child_session_id)
        _runner_app.unregister_subagent_work(child_session_id)
        return f"Error: failed to send message to child: {type(exc).__name__}: {exc}"
    if msg_resp.status_code >= 400:
        _runner_app.unregister_child_session(child_session_id)
        _runner_app.unregister_subagent_work(child_session_id)
        return (
            f"Error: failed to send message to child: {msg_resp.status_code} {msg_resp.text[:200]}"
        )

    # Return the structured handle mirrored from ``spawn.py``. The debug panel
    # parses this to discover child sessions in the sidebar.
    return json.dumps(
        {
            "task_id": child_session_id,
            "handle_id": child_session_id,
            "conversation_id": child_session_id,
            "kind": "sub_agent",
            "agent": sub_agent_name,
            "title": session_name,
            "status": "launching",
            "message": (
                f"[System: sub-agent {sub_agent_name} title {session_name!r} "
                f"launching as task {child_session_id}. Result will appear in "
                "your inbox; call sys_read_inbox to check or sys_cancel_task "
                "to interrupt it.]"
            ),
        }
    )

def _is_retryable_child_message_response(response: httpx.Response) -> bool:
    """
    Return whether a child message POST failed on a transient relay race.

    The server returns ``503 runner_unavailable`` when the session row exists
    but the runner stream relay is still subscribing. Retrying here keeps the
    public delegation API stable while the relay catches up.
    """
    if response.status_code != 503:
        return False
    try:
        payload = response.json()
    except ValueError:
        return "runner_unavailable" in response.text or "message relay" in response.text
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        return code == "runner_unavailable" or (
            isinstance(message, str) and "message relay" in message
        )
    return False

async def _post_child_message_event(
    *,
    server_client: httpx.AsyncClient,
    child_session_id: str,
    message: str,
) -> httpx.Response:
    """
    Post a user message to a child session, retrying relay-readiness races.

    :param server_client: HTTP client pointed at the Omnigent server.
    :param child_session_id: Child conversation id to start/continue.
    :param message: User message text.
    :returns: The final server response.
    :raises httpx.HTTPError: If the HTTP client itself fails.
    """
    body = {
        "type": "message",
        "data": {
            "role": "user",
            "content": [{"type": "input_text", "text": message}],
        },
    }
    for attempt in range(len(_CHILD_MESSAGE_RETRY_DELAYS_S) + 1):
        response = await server_client.post(
            f"/v1/sessions/{child_session_id}/events",
            json=body,
            timeout=30.0,
        )
        if not _is_retryable_child_message_response(response):
            return response
        if attempt >= len(_CHILD_MESSAGE_RETRY_DELAYS_S):
            return response
        delay_s = _CHILD_MESSAGE_RETRY_DELAYS_S[attempt]
        _logger.info(
            "child message post for session %s hit runner relay race; retrying in %.1fs",
            child_session_id,
            delay_s,
        )
        await asyncio.sleep(delay_s)
    return response

async def _send_to_existing_session(
    target_session_id: str,
    message: str,
    *,
    server_client: httpx.AsyncClient,
    conversation_id: str,
    publish_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> str:
    """
    Post a message to an existing direct-child session, return a handle.

    The by-session-id mode of ``sys_session_send``. **Child-only**: the
    target must be a direct child of the caller (its
    ``parent_session_id`` equals ``conversation_id``), so a caller can
    only drive sessions inside its own subtree — never a sibling or an
    unrelated session it merely has access to. Looks the target up to
    verify parentage (404 → ``session_not_found``; wrong parent or
    denied read → ``session_out_of_tree``), registers the child→parent
    fan-out and work mappings, posts the message, and returns a
    ``running`` handle immediately — the completion lands in the parent's
    ``sys_read_inbox`` queue, matching named-mode send.

    :param target_session_id: The existing child session id, e.g.
        ``"conv_abc123"``.
    :param message: The user message text to post.
    :param server_client: HTTP client pointed at the Omnigent server.
    :param conversation_id: The caller's own session id — the required
        parent of the target.
    :returns: JSON handle on success; a JSON/text error otherwise.
    """
    from omnigent.runner import app as _runner_app

    try:
        snap = await server_client.get(f"/v1/sessions/{target_session_id}", timeout=30.0)
    except Exception as exc:  # noqa: BLE001
        return f"Error: sys_session_send failed to look up session: {exc}"
    if snap.status_code == 404:
        return json.dumps({"error": "session_not_found", "conversation_id": target_session_id})
    if snap.status_code in (401, 403):
        return json.dumps({"error": "session_out_of_tree", "conversation_id": target_session_id})
    if snap.status_code != 200:
        return f"Error: sys_session_send lookup returned {snap.status_code}"
    snap_data = snap.json()
    if snap_data.get("parent_session_id") != conversation_id:
        return json.dumps(
            {
                "error": "session_out_of_tree",
                "conversation_id": target_session_id,
                "message": (
                    "target is not a direct child of the calling session; "
                    "sys_session_send by session_id is child-only."
                ),
            }
        )
    if is_session_closed(snap_data.get("labels"), snap_data.get("title")):
        return json.dumps(
            {
                "error": "session_closed",
                "conversation_id": target_session_id,
                "message": "target sub-agent session is closed; create a new session to continue.",
            }
        )
    parsed = _parse_session_title(snap_data.get("title"))
    agent_label = parsed.agent or "agent"
    existing_work = _runner_app.get_subagent_work(target_session_id)
    if existing_work is not None and existing_work.status in _SUBAGENT_ACTIVE_STATUSES:
        return (
            f"Error: session {target_session_id!r} already has a launching or running turn; "
            "wait for completion before sending again"
        )
    if snap_data.get("busy") is True:
        return (
            f"Error: session {target_session_id!r} is already running; "
            "wait for completion before sending again"
        )
    _runner_app.register_child_session(
        target_session_id,
        parent_session_id=conversation_id,
        title=snap_data.get("title") or "",
        tool=agent_label,
        session_name=parsed.title or "",
    )
    _runner_app.register_subagent_work(
        parent_session_id=conversation_id,
        child_session_id=target_session_id,
        agent=agent_label,
        title=parsed.title or "",
        wrapper_label=_session_wrapper_label(snap_data),
    )
    _publish_child_launching_update(
        parent_session_id=conversation_id,
        child_session_id=target_session_id,
        title=snap_data.get("title") or "",
        tool=agent_label,
        session_name=parsed.title or "",
        publish_event=publish_event,
    )

    try:
        msg_resp = await _post_child_message_event(
            server_client=server_client,
            child_session_id=target_session_id,
            message=message,
        )
    except httpx.HTTPError as exc:
        _runner_app.unregister_child_session(target_session_id)
        _runner_app.unregister_subagent_work(target_session_id)
        return f"Error: failed to send message to child: {type(exc).__name__}: {exc}"
    if msg_resp.status_code >= 400:
        _runner_app.unregister_child_session(target_session_id)
        _runner_app.unregister_subagent_work(target_session_id)
        return (
            f"Error: failed to send message to child: {msg_resp.status_code} {msg_resp.text[:200]}"
        )

    return json.dumps(
        {
            "task_id": target_session_id,
            "handle_id": target_session_id,
            "conversation_id": target_session_id,
            "kind": "sub_agent",
            "agent": agent_label,
            "title": parsed.title,
            "status": "launching",
            "message": (
                f"[System: sub-agent {agent_label} title {parsed.title!r} "
                f"launching as task {target_session_id}. Result will appear in "
                "your inbox; call sys_read_inbox to check or sys_cancel_task "
                "to interrupt it.]"
            ),
        }
    )

def _build_session_create_body(
    agent_id: str,
    conversation_id: str,
    title: Any,
    message: Any,
) -> dict[str, Any]:
    """
    Build the JSON ``POST /v1/sessions`` body for ``sys_session_create``.

    ``parent_session_id`` is hard-forced to ``conversation_id`` — this is
    what makes the write child-only (an orchestrator cannot create a
    top-level or sibling session). A non-empty ``title`` and ``message``
    are included when provided; the message becomes the child's first
    queued user turn via ``initial_items``.

    :param agent_id: The existing agent id or template name to launch,
        e.g. ``"ag_abc123"`` or ``"chief-of-staff"``.
    :param conversation_id: The caller's session id — the forced parent.
    :param title: Optional session label; included only when a non-empty
        string.
    :param message: Optional first user message; included only when a
        non-empty string.
    :returns: The JSON request body.
    """
    body: dict[str, Any] = {
        "agent_id": agent_id,
        "parent_session_id": conversation_id,
    }
    if isinstance(title, str) and title:
        body["title"] = title
    if isinstance(message, str) and message:
        body["initial_items"] = [
            {
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": message}]},
            }
        ]
    return body

def _finalize_created_session(
    data: dict[str, Any],
    *,
    conversation_id: str,
    agent_id: str,
    title: Any,
    publish_event: Callable[[str, dict[str, Any]], None] | None,
) -> str:
    """
    Register fan-out, emit ``session.created``, and build the handle.

    Records the child→parent mapping so the child's status/preview
    deltas fan out onto the caller's stream, publishes a transient
    ``session.created`` event (durability comes from the server's
    conversation row), and returns the handle the orchestrator uses to
    drive / monitor the child.

    :param data: The :class:`SessionResponse` JSON from the create call.
    :param conversation_id: The caller (parent) session id.
    :param agent_id: The launched durable agent id, e.g. ``"ag_abc123"``.
    :param title: The caller-supplied title (or non-str when absent).
    :param publish_event: Callback that enqueues an SSE event on the
        caller's outbound queue; ``None`` for in-process callers.
    :returns: JSON handle ``{conversation_id, kind, agent_id,
        agent_name, title, status}``.
    """
    from omnigent.runner import app as _runner_app
    from omnigent.server.schemas import SessionCreatedEvent

    child_id = data["id"]
    label = title if isinstance(title, str) else ""
    _runner_app.register_child_session(
        child_id,
        parent_session_id=conversation_id,
        title=label,
        tool=data.get("agent_name") or "agent",
        session_name=label,
    )
    evt = SessionCreatedEvent(
        type="session.created",
        conversation_id=conversation_id,
        child_session_id=child_id,
        agent_id=agent_id,
        parent_session_id=conversation_id,
    )
    if publish_event is not None:
        publish_event(conversation_id, evt.model_dump())
    return json.dumps(
        {
            "conversation_id": child_id,
            "kind": "sub_agent",
            "agent_id": agent_id,
            "agent_name": data.get("agent_name"),
            "title": title if isinstance(title, str) else None,
            "status": data.get("status") or "created",
        }
    )

async def _execute_session_create(
    args: dict[str, Any],
    *,
    server_client: httpx.AsyncClient | None,
    conversation_id: str | None,
    publish_event: Callable[[str, dict[str, Any]], None] | None,
    agent_spec: AgentSpecLike | None = None,
    runner_workspace: Path | None = None,
) -> str:
    """
    Create a child session (``sys_session_create``).

    Two modes, split on the provided argument (exactly one required):

    - ``agent_id`` — spawn from an existing agent via the JSON
      ``POST /v1/sessions`` create.
    - ``config_path`` — upload a NEW agent from local disk (an agent
      config YAML, agent directory, or pre-built ``.tar.gz`` bundle
      inside the caller's working directory) via the multipart
      ``POST /v1/sessions`` create.

    Both modes force ``parent_session_id`` to the caller (child-only).
    The child inherits the caller's runner (server-side affinity), so a
    queued initial message starts a turn immediately. Returns a handle
    the orchestrator can monitor (``sys_session_get_history`` /
    ``sys_session_get_info``) or drive (``sys_session_send`` by
    ``conversation_id``) — unlike named-mode send, it does NOT block on
    the child turn.

    Maps a 404 to ``agent_not_found`` and 401/403 to ``access_denied``.

    :param args: Parsed arguments; exactly one of ``agent_id`` /
        ``config_path`` required, ``title`` / ``message`` optional.
    :param server_client: HTTP client pointed at the Omnigent server; ``None``
        returns an error string.
    :param conversation_id: The caller's session id — the forced parent;
        ``None`` returns an error string.
    :param publish_event: SSE publish callback for ``session.created``.
    :param agent_spec: The calling agent's spec, used (with
        ``conversation_id`` / ``runner_workspace``) to resolve the
        os_env cwd that ``config_path`` is read from.
    :param runner_workspace: The runner's workspace dir, authoritative
        for the os_env cwd when present.
    :returns: JSON handle on success; a JSON error object otherwise.
    """
    if server_client is None:
        return json.dumps({"error": "sys_session_create requires server access"})
    if conversation_id is None:
        return json.dumps({"error": "sys_session_create requires a session id"})
    agent_id = args.get("agent_id")
    config_path = args.get("config_path")
    has_agent_id = isinstance(agent_id, str) and bool(agent_id)
    has_config_path = isinstance(config_path, str) and bool(config_path)
    if has_agent_id == has_config_path:
        # Fail loud on both-or-neither: the two modes create different
        # agents, so silently preferring one would mislaunch.
        return json.dumps(
            {
                "error": (
                    "sys_session_create requires exactly one of 'agent_id' "
                    "(existing agent) or 'config_path' (new agent from a "
                    "local config)"
                )
            }
        )
    if has_config_path:
        return await _session_create_from_config_path(
            str(config_path),
            args,
            server_client=server_client,
            conversation_id=conversation_id,
            publish_event=publish_event,
            agent_spec=agent_spec,
            runner_workspace=runner_workspace,
        )
    body = _build_session_create_body(
        str(agent_id), conversation_id, args.get("title"), args.get("message")
    )
    try:
        resp = await server_client.post("/v1/sessions", json=body, timeout=30.0)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_session_create failed: {exc}"})
    if resp.status_code == 404:
        return json.dumps({"error": "agent_not_found", "agent_id": agent_id})
    if resp.status_code in (401, 403):
        return json.dumps({"error": "access_denied", "agent_id": agent_id})
    if resp.status_code >= 400:
        return json.dumps(
            {"error": f"sys_session_create returned {resp.status_code}", "detail": resp.text[:200]}
        )
    data = resp.json()
    if not isinstance(data.get("id"), str) or not data["id"]:
        return json.dumps({"error": "server did not return a child session id"})
    launched_agent_id = (
        data.get("agent_id") if isinstance(data.get("agent_id"), str) else str(agent_id)
    )
    return _finalize_created_session(
        data,
        conversation_id=conversation_id,
        agent_id=launched_agent_id,
        title=args.get("title"),
        publish_event=publish_event,
    )

def _bundle_local_agent_source(source: Path) -> bytes:
    """
    Build gzipped agent-bundle bytes from a local source path.

    Handles the same source shapes as the CLI bundler: a standalone
    agent YAML file or an agent directory is materialized into a
    uniform bundle directory and tarred; any other file (e.g. a
    pre-built ``.tar.gz``) passes through as raw bytes for the
    server's bundle validation to accept or reject.

    Unlike the CLI bundler, no ``${VAR}`` env expansion is performed:
    expanding from the runner process environment would leak runner
    secrets into the uploaded bundle. Configs with unresolved env
    references fail loud in the server's spec validation instead.

    :param source: Local agent config YAML, agent directory, or
        bundle file, e.g.
        ``Path("/work/.omnigent/agent-configs/helper.yaml")``.
    :returns: Gzipped tarball bytes for the multipart ``bundle`` part.
    :raises FileNotFoundError: If ``source`` does not exist.
    """
    import io
    import tarfile

    from omnigent.spec import materialize_bundle

    if source.is_file() and source.suffix.lower() not in {".yaml", ".yml"}:
        return source.read_bytes()
    with tempfile.TemporaryDirectory() as tmpdir:
        bundle_dir = materialize_bundle(source, Path(tmpdir) / "bundle")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for file_path in sorted(bundle_dir.rglob("*")):
                if file_path.is_file():
                    tf.add(
                        str(file_path),
                        arcname=str(file_path.relative_to(bundle_dir)),
                    )
        return buf.getvalue()

async def _post_child_first_message(
    child_session_id: str,
    message: str,
    server_client: httpx.AsyncClient,
) -> str | None:
    """
    Queue a bundle-created child's first user message.

    Posted as a separate event so the server's post_event forwards it
    to the runner and starts the child turn (same pattern as
    named-mode ``sys_session_send``).

    :param child_session_id: The new child session id,
        e.g. ``"conv_abc123"``.
    :param message: The first user message text.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: ``None`` on success; a JSON error string (carrying the
        created ``conversation_id`` so the orchestrator can retry via
        ``sys_session_send``) on failure.
    """
    try:
        msg_resp = await _post_child_message_event(
            server_client=server_client,
            child_session_id=child_session_id,
            message=message,
        )
    except httpx.HTTPError as exc:
        return json.dumps(
            {
                "error": f"child session created but message failed: {exc}",
                "conversation_id": child_session_id,
            }
        )
    if msg_resp.status_code >= 400:
        return json.dumps(
            {
                "error": (
                    "child session created but message failed: "
                    f"{msg_resp.status_code} {msg_resp.text[:200]}"
                ),
                "conversation_id": child_session_id,
            }
        )
    return None

async def _upload_config_bundle(
    config_path: str,
    args: dict[str, Any],
    *,
    server_client: httpx.AsyncClient,
    conversation_id: str,
    agent_spec: AgentSpecLike | None,
    runner_workspace: Path | None,
) -> dict[str, Any] | str:
    """
    Resolve, bundle, and upload a local agent config as a child session.

    Reads ``config_path`` from the caller's os_env working directory
    (containment-checked, mirroring the ``sys_agent_download`` write
    guard), bundles it, and proxies the multipart
    ``POST /v1/sessions`` create with ``parent_session_id`` forced to
    the caller.

    :param config_path: Caller-supplied path to the agent config YAML,
        agent directory, or ``.tar.gz`` bundle, relative to the os_env
        cwd, e.g. ``".omnigent/agent-configs/helper.yaml"``.
    :param args: Parsed tool arguments; optional ``title``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :param conversation_id: The caller's session id — the forced parent.
    :param agent_spec: The calling agent's spec, for os_env resolution.
    :param runner_workspace: The runner workspace, authoritative cwd.
    :returns: The parsed ``CreatedSessionResponse`` dict on success; a
        JSON error string otherwise.
    """
    os_spec = _effective_runner_os_env_spec(agent_spec, conversation_id, runner_workspace)
    resolved_cwd = Path(os_spec.cwd).resolve()
    source = (resolved_cwd / config_path).resolve()
    if not source.is_relative_to(resolved_cwd):
        return json.dumps(
            {"error": "sys_session_create config_path escapes the working directory"}
        )
    if not source.exists():
        return json.dumps({"error": "config_not_found", "config_path": config_path})
    try:
        bundle_bytes = await asyncio.to_thread(_bundle_local_agent_source, source)
    except Exception as exc:  # noqa: BLE001 — disk/tar errors become a typed tool error.
        return json.dumps({"error": f"sys_session_create failed to bundle config: {exc}"})

    metadata: dict[str, Any] = {"parent_session_id": conversation_id}
    title = args.get("title")
    if isinstance(title, str) and title:
        metadata["title"] = title
    try:
        resp = await server_client.post(
            "/v1/sessions",
            data={"metadata": json.dumps(metadata)},
            files={"bundle": (f"{source.name}.tar.gz", bundle_bytes, "application/gzip")},
            timeout=60.0,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_session_create failed: {exc}"})
    if resp.status_code in (401, 403):
        return json.dumps({"error": "access_denied", "config_path": config_path})
    if resp.status_code >= 400:
        return json.dumps(
            {"error": f"sys_session_create returned {resp.status_code}", "detail": resp.text[:200]}
        )
    data: dict[str, Any] = resp.json()
    return data

async def _session_create_from_config_path(
    config_path: str,
    args: dict[str, Any],
    *,
    server_client: httpx.AsyncClient,
    conversation_id: str,
    publish_event: Callable[[str, dict[str, Any]], None] | None,
    agent_spec: AgentSpecLike | None,
    runner_workspace: Path | None,
) -> str:
    """
    Bundle-mode ``sys_session_create``: upload a new agent and launch it.

    Delegates the resolve/bundle/upload pipeline to
    :func:`_upload_config_bundle`, validates the server's
    ``CreatedSessionResponse``, queues the optional first ``message``
    via :func:`_post_child_first_message`, and returns the
    orchestrator handle.

    :param config_path: Caller-supplied path to the agent config YAML,
        agent directory, or ``.tar.gz`` bundle, relative to the os_env
        cwd, e.g. ``".omnigent/agent-configs/helper.yaml"``.
    :param args: Parsed tool arguments; optional ``title`` /
        ``message``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :param conversation_id: The caller's session id — the forced parent.
    :param publish_event: SSE publish callback for ``session.created``.
    :param agent_spec: The calling agent's spec, for os_env resolution.
    :param runner_workspace: The runner workspace, authoritative cwd.
    :returns: JSON handle on success; a JSON error object otherwise.
    """
    data = await _upload_config_bundle(
        config_path,
        args,
        server_client=server_client,
        conversation_id=conversation_id,
        agent_spec=agent_spec,
        runner_workspace=runner_workspace,
    )
    if isinstance(data, str):
        return data
    child_session_id = data.get("session_id")
    if not isinstance(child_session_id, str) or not child_session_id:
        return json.dumps({"error": "server did not return a child session id"})
    created_agent_id = data.get("agent_id")
    if not isinstance(created_agent_id, str) or not created_agent_id:
        # CreatedSessionResponse.agent_id is a required field — a
        # missing value is a server contract violation, not a
        # recoverable state.
        return json.dumps(
            {
                "error": "server did not return the created agent id",
                "conversation_id": child_session_id,
            }
        )

    message = args.get("message")
    if isinstance(message, str) and message:
        message_error = await _post_child_first_message(child_session_id, message, server_client)
        if message_error is not None:
            return message_error

    return _finalize_created_session(
        # Adapt the multipart CreatedSessionResponse shape to the
        # session-snapshot keys _finalize_created_session reads.
        {
            "id": child_session_id,
            "agent_name": data.get("agent_name"),
            "status": "created",
        },
        conversation_id=conversation_id,
        agent_id=created_agent_id,
        title=args.get("title"),
        publish_event=publish_event,
    )

def _has_subagent(
    sub_agent_name: str,
    agent_spec: AgentSpecLike | None,
) -> bool:
    """
    Check whether a sub-agent name exists in the parent spec.

    Searches both ``sub_agents`` (AP-style spec) and ``tools``
    dict (omnigent inner loader) for a matching name.

    :param sub_agent_name: Name of the sub-agent, e.g.
        ``"researcher"``.
    :param agent_spec: Parent agent's spec. ``None`` when no
        spec is available.
    :returns: ``True`` if the sub-agent is declared.
    """
    if agent_spec is None:
        return False
    # AP-style spec: sub_agents list
    sub_agents = getattr(agent_spec, "sub_agents", None) or []
    for sa in sub_agents:
        if getattr(sa, "name", None) == sub_agent_name:
            return True
    # Omnigent inner loader: tools dict with AgentTool entries
    tools = getattr(agent_spec, "tools", None)
    if isinstance(tools, dict) and sub_agent_name in tools:
        return True
    return False

def _child_rows_to_entries(
    rows: list[dict[str, Any]],
) -> list[dict[str, str | None]]:
    """
    Map ``child_sessions`` rows to ``sys_session_list`` entries.

    Skips closed and titleless/colonless rows. The server already
    parses ``tool``/``session_name`` from the title (including the
    ``"ui:<agent>:<label>"`` form), so those are reused.

    :param rows: ``data`` rows from ``GET .../child_sessions``.
    :returns: ``[{"agent", "title", "conversation_id"}, ...]``.
    """
    entries: list[dict[str, str | None]] = []
    for row in rows:
        title = row.get("title")
        if not title or ":" not in title or is_session_closed(row.get("labels"), title):
            continue
        entries.append(
            {
                "agent": row.get("tool"),
                "title": row.get("session_name"),
                "conversation_id": row.get("id"),
            }
        )
    return entries

def _subagent_child_id(payload: dict[str, Any]) -> str | None:
    """
    Extract the child session id from a sub-agent inbox payload.

    :param payload: Inbox payload, e.g. a ``type="sub_agent"`` item.
    :returns: Child session id, or ``None`` when absent.
    """
    for key in ("conversation_id", "task_id", "handle_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None

def _subagent_policy_failure_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Return a fail-closed copy of a sub-agent inbox payload.

    :param payload: Original inbox payload.
    :returns: Payload with output replaced by a policy-failure
        sentinel.
    """
    return {**payload, "output": _SUBAGENT_POLICY_FAILURE_OUTPUT}

def _subagent_tool_result_policy_request(
    payload: dict[str, Any],
    output: str,
) -> dict[str, Any]:
    """
    Build the Omnigent policy-evaluation request for delayed child output.

    :param payload: Completed sub-agent inbox payload.
    :param output: Raw child output text.
    :returns: JSON body for ``POST /policies/evaluate``.
    """
    return {
        "event": {
            "type": "PHASE_TOOL_RESULT",
            "data": {"result": output},
            "request_data": {
                "name": "sys_session_send",
                "tool": "sys_session_send",
                "args": {
                    "agent": payload.get("agent") or payload.get("tool_name"),
                    "title": payload.get("title"),
                    "conversation_id": _subagent_child_id(payload),
                },
            },
        }
    }

async def _post_subagent_policy_verdict(
    *,
    server_client: httpx.AsyncClient,
    conversation_id: str,
    payload: dict[str, Any],
    output: str,
) -> dict[str, Any] | None:
    """
    POST delayed sub-agent output to Omnigent policy evaluation.

    :param server_client: HTTP client pointed at Omnigent server.
    :param conversation_id: Parent session id, e.g.
        ``"conv_parent123"``.
    :param payload: Completed sub-agent inbox payload.
    :param output: Raw child output text.
    :returns: Parsed policy verdict, or ``None`` on failure.
    """
    try:
        resp = await server_client.post(
            f"/v1/sessions/{conversation_id}/policies/evaluate",
            json=_subagent_tool_result_policy_request(payload, output),
            timeout=30.0,
        )
    except httpx.HTTPError:
        _logger.exception(
            "Sub-agent inbox TOOL_RESULT policy evaluation failed for parent=%s child=%s",
            conversation_id,
            _subagent_child_id(payload),
        )
        return None
    if resp.status_code >= 400:
        _logger.warning(
            "Sub-agent inbox TOOL_RESULT policy evaluation rejected for "
            "parent=%s status=%s body=%s",
            conversation_id,
            resp.status_code,
            resp.text,
        )
        return None
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError):
        _logger.warning(
            "Sub-agent inbox TOOL_RESULT policy evaluation returned non-JSON for parent=%s",
            conversation_id,
        )
        return None

def _apply_subagent_policy_verdict(
    payload: dict[str, Any],
    verdict: dict[str, Any],
) -> _SubagentInboxEvaluation:
    """
    Apply an Omnigent policy verdict to a sub-agent inbox payload.

    :param payload: Original completed sub-agent payload.
    :param verdict: Parsed Omnigent policy response, e.g.
        ``{"result": "POLICY_ACTION_ALLOW"}``.
    :returns: Evaluation result for ``sys_read_inbox`` formatting.
    """
    result = verdict.get("result")
    if result in {"POLICY_ACTION_DENY", "POLICY_ACTION_ASK"}:
        reason = verdict.get("reason") or "no reason given"
        return _SubagentInboxEvaluation(
            {**payload, "output": f"[Result suppressed by policy: {reason}]"}
        )
    if result in {"POLICY_ACTION_ALLOW", "POLICY_ACTION_UNSPECIFIED"}:
        transformed = verdict.get("data")
        if transformed is None:
            return _SubagentInboxEvaluation(payload)
        if not isinstance(transformed, str):
            _logger.warning(
                "Sub-agent inbox TOOL_RESULT policy data must be str; got %s",
                type(transformed).__name__,
            )
        return _SubagentInboxEvaluation(
            {
                **payload,
                "output": transformed if isinstance(transformed, str) else str(transformed),
            }
        )
    _logger.warning(
        "Sub-agent inbox TOOL_RESULT policy evaluation returned unknown result=%r",
        result,
    )
    return _SubagentInboxEvaluation(
        _subagent_policy_failure_payload(payload),
        retry_original=True,
    )

async def _evaluate_subagent_inbox_output(
    payload: dict[str, Any],
    *,
    server_client: httpx.AsyncClient | None,
    conversation_id: str | None,
) -> _SubagentInboxEvaluation:
    """
    Apply parent TOOL_RESULT policy to a delayed sub-agent payload.

    :param payload: Inbox payload for a completed sub-agent task.
    :param server_client: HTTP client pointed at Omnigent server.
    :param conversation_id: Parent session id, e.g.
        ``"conv_parent123"``.
    :returns: Evaluation result carrying the safe payload plus retry
        metadata for transient evaluation failures.
    """
    if (
        payload.get("type") != "sub_agent"
        or payload.get("status") not in _SUBAGENT_POLICY_STATUSES
    ):
        return _SubagentInboxEvaluation(payload)
    output = payload.get("output")
    if not isinstance(output, str) or server_client is None or conversation_id is None:
        return _SubagentInboxEvaluation(
            _subagent_policy_failure_payload(payload),
            retry_original=True,
        )
    verdict = await _post_subagent_policy_verdict(
        server_client=server_client,
        conversation_id=conversation_id,
        payload=payload,
        output=output,
    )
    if verdict is None:
        return _SubagentInboxEvaluation(
            _subagent_policy_failure_payload(payload),
            retry_original=True,
        )
    return _apply_subagent_policy_verdict(payload, verdict)

def _cleanup_drained_subagent_work(payload: dict[str, Any]) -> None:
    """
    Remove terminal sub-agent work after its inbox item is drained.

    :param payload: Drained inbox payload.
    :returns: None.
    """
    if payload.get("type") != "sub_agent":
        return
    if payload.get("status") not in _SUBAGENT_INBOX_TERMINAL_STATUSES:
        return
    child_id = _subagent_child_id(payload)
    if child_id is None:
        return
    work_id = payload.get("work_id")
    if not isinstance(work_id, str) or not work_id:
        return
    from omnigent.runner import app as _runner_app

    _runner_app.unregister_subagent_work(
        child_id,
        work_id=work_id,
        remember_drained_delivery=True,
    )

async def _cancel_subagent_task(
    args: dict[str, Any],
    *,
    conversation_id: str | None,
    server_client: httpx.AsyncClient | None,
) -> str:
    """
    Cancel a running sub-agent worker, routing by the child's harness.

    Only ``claude-native`` has a runner-side hard-stop, so the cancel
    event is chosen per harness — the child runner's ``stop_session``
    handler 204 no-ops for every other harness, so posting it there
    would silently do nothing:

    * ``claude-native`` — POST ``stop_session``. The child runner
      hard-kills the worker's tmux pane via ``_handle_claude_native_stop``
      and marks the work entry cancelled, delivering a terminal payload to
      the parent inbox and auto-waking it. A bare interrupt (Escape) only
      cancelled the current turn and left the worker process alive; a stop
      frees it.
    * everything else (in-process harnesses, ``codex-native``) — POST
      ``interrupt``, the path those harnesses actually honor. For an
      in-process child the runner marks the turn cancelled (via
      ``_interrupted_sessions`` → ``_on_proxy_stream_end``) and wakes the
      parent. ``codex-native`` has no runner-side stop yet, so its cancel
      stays best-effort (see message).

    :param args: Tool arguments containing ``task_id`` or
        ``handle_id``, e.g. ``{"task_id": "conv_child456"}``.
    :param conversation_id: Parent session id, e.g.
        ``"conv_parent123"``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: JSON cancellation result.
    """
    from omnigent.runner import app as _runner_app

    task_id = args.get("task_id") or args.get("handle_id")
    if not task_id:
        return 'Error: sys_cancel_task requires "task_id"'
    if conversation_id is None:
        return "Error: sys_cancel_task requires conversation_id"
    entry = _runner_app.get_subagent_work(str(task_id))
    if entry is None or entry.parent_session_id != conversation_id:
        return f"Error: no in-flight task with task_id {task_id}"
    # A dispatched child sits in ``launching`` until its runtime emits a real
    # busy edge (see ``mark_subagent_work_started``). Cancellation must still
    # route to the child during that window — otherwise cancelling a slow-to-
    # start sub-agent would silently no-op and leave it running. Only terminal
    # states (``completed`` / ``failed`` / ``cancelled``) short-circuit here.
    if entry.status not in _SUBAGENT_ACTIVE_STATUSES:
        return json.dumps(
            {
                "cancelled": entry.status == SubagentWorkStatus.CANCELLED,
                "task_id": task_id,
                "status": entry.status,
            }
        )
    if server_client is None:
        return "Error: sys_cancel_task requires server access for sub-agent tasks"

    # claude-native is the only harness with a runner-side hard-stop; every
    # other harness 204 no-ops on stop_session, so route them to interrupt.
    event_type = (
        "stop_session" if entry.wrapper_label == CLAUDE_NATIVE_WRAPPER_VALUE else "interrupt"
    )

    try:
        resp = await server_client.post(
            f"/v1/sessions/{task_id}/events",
            # Bare control events 422 on servers that require body.data.
            json={"type": event_type, "data": {}},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        return f"Error: sys_cancel_task {event_type} failed: {type(exc).__name__}: {exc}"
    if resp.status_code >= 400:
        return (
            f"Error: sys_cancel_task {event_type} returned {resp.status_code}: {resp.text[:200]}"
        )

    updated = _runner_app.get_subagent_work(str(task_id)) or entry
    if updated.status == "cancelled":
        return json.dumps({"cancelled": True, "task_id": task_id, "status": "cancelled"})
    if updated.wrapper_label == CODEX_NATIVE_WRAPPER_VALUE:
        return json.dumps(
            {
                "cancel_requested": True,
                "cancel_confirmed": False,
                "best_effort": True,
                "task_id": task_id,
                "status": updated.status,
                "message": (
                    "Interrupt forwarded, but a runner-side hard-stop is not wired "
                    "for codex-native workers yet; the child may keep running and no "
                    "terminal inbox status is guaranteed."
                ),
            }
        )
    return json.dumps(
        {
            "cancel_requested": True,
            "cancel_confirmed": False,
            "task_id": task_id,
            "status": updated.status,
            "message": (
                "Cancel requested; cancellation has not been confirmed yet. "
                "Use sys_read_inbox to observe terminal status."
            ),
        }
    )
