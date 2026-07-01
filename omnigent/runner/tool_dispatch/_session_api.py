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

def _session_wrapper_label(session_payload: dict[str, Any]) -> str | None:
    """
    Extract the native terminal wrapper label from a session payload.

    :param session_payload: Session or child-session payload, e.g.
        ``{"labels": {"omnigent.wrapper": "codex-native-ui"}}``.
    :returns: Wrapper label value, or ``None`` when absent.
    """
    labels = session_payload.get("labels")
    if not isinstance(labels, dict):
        return None
    wrapper = labels.get(_SESSION_WRAPPER_LABEL_KEY)
    return wrapper if isinstance(wrapper, str) and wrapper else None

@dataclass
class _ParsedTitle:
    """
    A child-session title split into its agent + instance components.

    :param agent: The agent/tool segment, e.g. ``"researcher"`` or
        ``"claude-native-ui"``; ``None`` when the title has no colon
        (a top-level/legacy row that isn't a sub-agent).
    :param title: The instance label segment, e.g. ``"auth"`` or
        ``"1"``; ``None`` in the same no-colon case.
    """

    agent: str | None
    title: str | None

def _parse_session_title(raw_title: str | None) -> _ParsedTitle:
    """
    Split a child-session title into agent + instance label.

    Mirrors the server's ``_child_session_summary_from_conversation``
    parse: the canonical ``"<agent>:<title>"`` form written by
    ``sys_session_send``, plus the 3-segment ``"ui:<agent>:<label>"``
    form written by the Web UI "Add agent" flow. Legacy closed suffixes
    are stripped before parsing so display/tool output stays human
    readable. Returns both fields ``None`` when the title has no colon
    (a top-level conversation that is not a sub-agent).

    :param raw_title: The conversation ``title``, e.g.
        ``"researcher:auth"`` or ``"ui:claude-native-ui:1"``; may be
        ``None``.
    :returns: The parsed agent/title pair.
    """
    display_title = title_without_closed_marker(raw_title)
    if not display_title or ":" not in display_title:
        return _ParsedTitle(agent=None, title=None)
    head, _, tail = display_title.partition(":")
    if head == "ui" and ":" in tail:
        agent, _, label = tail.partition(":")
        return _ParsedTitle(agent=agent, title=label)
    return _ParsedTitle(agent=head, title=tail)

def _truncate_activity(text: str | None) -> str | None:
    """
    Truncate text to ``_ACTIVITY_MAX_CHARS`` to bound peek prompt size.

    :param text: The text to truncate, or ``None``.
    :returns: The (possibly truncated) text, or ``None`` when the input
        is ``None``.
    """
    if text is None:
        return None
    if len(text) <= _ACTIVITY_MAX_CHARS:
        return text
    return text[:_ACTIVITY_MAX_CHARS] + " [truncated]"

def _text_from_api_content(content: Any) -> str:
    """
    Join the text blocks of an API message ``content`` array.

    :param content: The ``content`` field of an API message item — a
        list of blocks like ``{"type": "output_text", "text": "..."}``.
    :returns: The concatenated text, or ``""`` when there is none.
    """
    if not isinstance(content, list):
        return ""
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    return " ".join(parts)

def _project_api_item(item: dict[str, Any]) -> dict[str, str | None]:
    """
    Project a REST API conversation item into the compact peek shape.

    Mirrors :func:`omnigent.tools.builtins.spawn._project_activity_item`
    but reads the API item JSON returned by
    ``GET /v1/sessions/{id}/items`` (``ConversationItem.to_api_dict()``)
    rather than the in-process entity, so the harness peek result reads
    the same as the in-process tool's.

    :param item: One API item dict from the items endpoint.
    :returns: A compact dict — ``{type, tool, args}`` for tool calls,
        ``{type, output}`` for tool results, ``{type, role, text}`` for
        messages.
    """
    itype = item.get("type")
    if itype == "function_call":
        return {
            "type": "function_call",
            "tool": item.get("name"),
            "args": _truncate_activity(item.get("arguments")),
        }
    if itype == "function_call_output":
        output = item.get("output")
        rendered = output if isinstance(output, str) else json.dumps(output)
        return {"type": "function_call_output", "output": _truncate_activity(rendered)}
    if itype == "message":
        return {
            "type": "message",
            "role": item.get("role"),
            "text": _truncate_activity(_text_from_api_content(item.get("content"))),
        }
    return {"type": itype}

async def _execute_session_query_tool(
    tool_name: str,
    arguments: str,
    *,
    conversation_id: str | None,
    server_client: httpx.AsyncClient | None,
) -> str:
    """
    Runner-local handler for ``sys_session_get_history`` / ``sys_session_list`` /
    ``sys_session_close``.

    The runner is a separate subprocess from the Omnigent server and has no
    in-process ``ConversationStore`` (same constraint as
    :func:`_execute_comment_tool`). These tools therefore dispatch to the
    Omnigent server's existing REST endpoints over ``server_client``:

    - ``sys_session_list`` → ``GET /v1/sessions/{caller}/child_sessions``
    - ``sys_session_get_history`` → ``GET /v1/sessions/{target}/items``
    - ``sys_session_get_info`` → ``GET /v1/sessions/{target}`` (plus a
      best-effort ``GET /v1/runners/{id}/status`` for connectivity)
    - ``sys_session_close`` → ``GET`` the target snapshot then ``PATCH
      /v1/sessions/{target}`` with a tombstoned title

    Output shapes mirror the in-process tools in
    :mod:`omnigent.tools.builtins.spawn` so the LLM sees identical
    results regardless of executor. No new identity handling is
    introduced: access control is whatever the server already enforces
    on those endpoints for ``server_client`` — the same posture as
    :func:`_execute_subagent_tool`.

    :param tool_name: ``"sys_session_get_history"``, ``"sys_session_list"``,
        ``"sys_session_close"``, or ``"sys_session_get_info"``.
    :param arguments: JSON-encoded arguments string from the LLM, e.g.
        ``'{"conversation_id": "conv_abc123", "tail_items": 5}'``.
    :param conversation_id: The calling session id, e.g. ``"conv_root1"``;
        used as the parent for ``sys_session_list``.
    :param server_client: HTTP client pointed at the Omnigent server; ``None``
        if unavailable (returns an error string).
    :returns: Tool output JSON string matching the in-process tool shape.
    """
    if server_client is None:
        return json.dumps({"error": f"{tool_name} requires server access"})
    if conversation_id is None:
        return json.dumps({"error": f"{tool_name} requires a session id"})
    try:
        args: dict[str, Any] = json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError:
        return json.dumps({"error": f"{tool_name}: malformed JSON arguments"})

    if tool_name == "sys_session_list":
        return await _session_list_via_rest(conversation_id, server_client, args.get("agent_name"))
    if tool_name == "sys_session_get_history":
        return await _session_get_history_via_rest(args, server_client)
    if tool_name == "sys_session_get_info":
        return await _session_get_info_via_rest(args, conversation_id, server_client)
    return await _session_close_via_rest(args, conversation_id, server_client)

async def _runner_online_or_none(
    runner_id: str | None,
    server_client: httpx.AsyncClient,
) -> bool | None:
    """
    Resolve a runner's live connectivity via ``GET /v1/runners/{id}/status``.

    Best-effort: returns ``None`` when no runner is bound or the status
    lookup fails, so ``sys_session_get_info`` degrades to "connectivity
    unknown" rather than erroring on a transient runner-status hiccup.

    :param runner_id: The session's bound runner id, or ``None``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: ``True``/``False`` from the status endpoint, or ``None``
        when unbound or the lookup is inconclusive.
    """
    if not runner_id:
        return None
    try:
        resp = await server_client.get(f"/v1/runners/{runner_id}/status", timeout=30.0)
    except Exception:  # noqa: BLE001
        return None
    if resp.status_code != 200:
        return None
    online = resp.json().get("online")
    return online if isinstance(online, bool) else None

async def _session_get_info_via_rest(
    args: dict[str, Any],
    conversation_id: str,
    server_client: httpx.AsyncClient,
) -> str:
    """
    Return a session's metadata snapshot via ``GET /v1/sessions/{id}``.

    Resolves the target from ``args["session_id"]`` (falling back to the
    caller's own ``conversation_id`` when omitted), fetches the session
    snapshot, and projects the metadata fields — status, title, agent
    binding, runner binding, host, reasoning effort, effective model,
    parent linkage, workspace / git branch, and the outstanding approval
    prompts (the prompts themselves plus a count). Runner connectivity
    is resolved best-effort via
    ``GET /v1/runners/{id}/status`` (``runner_online`` is ``None`` when
    the lookup fails or no runner is bound). The full transcript is
    intentionally omitted — that is what ``sys_session_get_history`` returns.

    Maps a 404 to ``session_not_found`` and 401/403 to ``access_denied``
    (the server denied the read, so from the caller's vantage the target
    is one it may not see).

    :param args: Parsed tool arguments; optional ``session_id``.
    :param conversation_id: The caller's own session id, used as the
        default target when ``session_id`` is omitted.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: JSON metadata object, or a JSON error object.
    """
    raw_target = args.get("session_id") or conversation_id
    if not isinstance(raw_target, str) or not raw_target:
        return json.dumps(
            {"error": "sys_session_get_info requires a non-empty 'session_id' string"}
        )
    try:
        resp = await server_client.get(f"/v1/sessions/{raw_target}", timeout=30.0)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_session_get_info failed: {exc}"})
    if resp.status_code == 404:
        return json.dumps({"error": "session_not_found", "session_id": raw_target})
    if resp.status_code in (401, 403):
        return json.dumps({"error": "access_denied", "session_id": raw_target})
    if resp.status_code != 200:
        return json.dumps({"error": f"sys_session_get_info returned {resp.status_code}"})
    snap: dict[str, Any] = resp.json()
    pending = snap.get("pending_elicitations") or []
    return json.dumps(
        {
            "session_id": snap.get("id"),
            "status": snap.get("status"),
            "title": snap.get("title"),
            "agent_id": snap.get("agent_id"),
            "agent_name": snap.get("agent_name"),
            "runner_id": snap.get("runner_id"),
            "runner_online": await _runner_online_or_none(snap.get("runner_id"), server_client),
            "host_id": snap.get("host_id"),
            "parent_session_id": snap.get("parent_session_id"),
            "sub_agent_name": snap.get("sub_agent_name"),
            "reasoning_effort": snap.get("reasoning_effort"),
            # Effective model: a per-session override wins over the
            # agent spec's default; both may be None when unset.
            "model": snap.get("model_override") or snap.get("llm_model"),
            "workspace": snap.get("workspace"),
            "git_branch": snap.get("git_branch"),
            # The outstanding approval prompts themselves (original
            # elicitation-request event dicts), plus a count for quick
            # status checks. Surfacing the prompts — not just a tally —
            # lets the orchestrator see what each blocked session is
            # waiting on.
            "pending_elicitations": pending,
            "pending_elicitation_count": len(pending),
        }
    )

async def _session_list_via_rest(
    conversation_id: str,
    server_client: httpx.AsyncClient,
    agent_name: Any = None,
) -> str:
    """
    Return the two-view session list: ``sub_agents`` + global ``sessions``.

    ``sub_agents`` is the caller's named-sub-agent view (children, plus
    parent/siblings when the caller is itself a child) — see
    :func:`_collect_sub_agents`. ``sessions`` is the **global**,
    permission-bounded list of every session the caller can access, each
    annotated with status + runner connectivity, optionally filtered by
    ``agent_name`` — see :func:`_collect_global_sessions`. Both are
    best-effort: a failure in either view yields an empty list for it
    rather than failing the whole call.

    :param conversation_id: The caller session id, e.g. ``"conv_root1"``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :param agent_name: Optional agent-name filter for the global
        ``sessions`` view; ignored for ``sub_agents``.
    :returns: JSON ``{"sub_agents": [...], "sessions": [...]}``.
    """
    sub_agents = await _collect_sub_agents(conversation_id, server_client)
    sessions = await _collect_global_sessions(server_client, agent_name)
    return json.dumps({"sub_agents": sub_agents, "sessions": sessions})

async def _resolve_runner_online_map(
    rows: list[dict[str, Any]],
    server_client: httpx.AsyncClient,
) -> dict[str, bool | None]:
    """
    Resolve live connectivity for the unique runners bound across rows.

    Checks each distinct ``runner_id`` once (sessions frequently share a
    runner) so the status round-trips scale with the number of runners,
    not the number of sessions. Best-effort per runner via
    :func:`_runner_online_or_none`.

    :param rows: Session rows from ``GET /v1/sessions``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: Map of ``runner_id`` → online bool (or ``None`` if the
        lookup was inconclusive).
    """
    unique_ids: list[str] = []
    seen: set[str] = set()
    for r in rows:
        rid = r.get("runner_id")
        if isinstance(rid, str) and rid and rid not in seen:
            seen.add(rid)
            unique_ids.append(rid)
    results = await asyncio.gather(
        *(_runner_online_or_none(rid, server_client) for rid in unique_ids)
    )
    # strict=True: results is gathered in unique_ids order, so lengths
    # match by construction — assert it rather than silently truncating.
    return dict(zip(unique_ids, results, strict=True))

async def _session_parent_id(
    conversation_id: str,
    server_client: httpx.AsyncClient,
) -> str | None:
    """
    Return a session's ``parent_session_id`` (None if top-level/unknown).

    Used to decide whether the caller is itself a child — i.e. a
    user-added agent that should also see ``main`` + siblings. Best-
    effort: returns ``None`` on any read failure rather than raising.

    :param conversation_id: The session to inspect.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: The parent session id, or ``None``.
    """
    try:
        snap = await server_client.get(f"/v1/sessions/{conversation_id}", timeout=30.0)
    except Exception:  # noqa: BLE001
        return None
    if snap.status_code != 200:
        return None
    parent = snap.json().get("parent_session_id")
    return parent if isinstance(parent, str) and parent else None

async def _session_get_history_via_rest(
    args: dict[str, Any],
    server_client: httpx.AsyncClient,
) -> str:
    """
    Read a target session's recent items via ``GET .../items``.

    Mirrors :class:`SysSessionGetHistoryTool`: returns
    ``{"conversation_id", "agent", "title", "items"}`` with items in
    chronological order. The target's ``agent``/``title`` come from its
    session snapshot. Maps a 404 to ``session_not_found`` and a
    403/401 to ``session_out_of_tree`` (the server denied read access,
    so from the caller's vantage the target is outside the sessions it
    may read).

    :param args: Parsed tool arguments; requires ``conversation_id``,
        optional ``tail_items``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: JSON peek result, or a JSON error object.
    """
    target_id = args.get("conversation_id")
    if not isinstance(target_id, str) or not target_id:
        return json.dumps(
            {"error": "sys_session_get_history requires a non-empty 'conversation_id' string"}
        )
    tail_items = _clamp_tail_items(args.get("tail_items", _HISTORY_DEFAULT_TAIL))
    if isinstance(tail_items, str):
        return tail_items
    try:
        resp = await server_client.get(
            f"/v1/sessions/{target_id}/items",
            params={"limit": tail_items, "order": "desc"},
            timeout=30.0,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_session_get_history failed: {exc}"})
    if resp.status_code == 404:
        return json.dumps({"error": "session_not_found", "conversation_id": target_id})
    if resp.status_code in (401, 403):
        return json.dumps({"error": "session_out_of_tree", "conversation_id": target_id})
    if resp.status_code != 200:
        return json.dumps({"error": f"sys_session_get_history returned {resp.status_code}"})
    data: list[dict[str, Any]] = resp.json().get("data", [])
    # ``order="desc"`` returns newest-first; reverse to chronological so
    # the LLM reads top-to-bottom (matches the in-process peek).
    items: list[dict[str, Any]] = [_project_api_item(it) for it in reversed(data)]
    meta = await _fetch_peek_meta(target_id, server_client)
    # A parked elicitation never lands in the conversation store (it
    # lives only in the Omnigent server's pending-elicitations index, replayed
    # on the snapshot), so append the snapshot's outstanding prompts
    # after the stored tail — they are the sub-agent's most recent act.
    items.extend(
        pending_elicitations.project_for_peek(event) for event in meta.pending_elicitations
    )
    return json.dumps(
        {
            "conversation_id": target_id,
            "agent": meta.agent,
            "title": meta.title,
            "items": items,
        }
    )

async def _fetch_close_target(
    target_id: str,
    server_client: httpx.AsyncClient,
) -> dict[str, Any] | str:
    """
    Fetch + status-classify the close target's session snapshot.

    :param target_id: The conversation id to close, e.g. ``"conv_abc123"``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: The parsed snapshot dict on HTTP 200; otherwise a JSON
        error string (``session_not_found`` for 404,
        ``session_out_of_tree`` for 401/403, a generic status error
        otherwise) suitable for returning verbatim to the LLM.
    """
    try:
        snap = await server_client.get(f"/v1/sessions/{target_id}", timeout=30.0)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_session_close failed: {exc}"})
    if snap.status_code == 404:
        return json.dumps({"error": "session_not_found", "conversation_id": target_id})
    if snap.status_code in (401, 403):
        return json.dumps({"error": "session_out_of_tree", "conversation_id": target_id})
    if snap.status_code != 200:
        return json.dumps({"error": f"sys_session_close returned {snap.status_code}"})
    return snap.json()

async def _close_tree_scope_error(
    target_snap: dict[str, Any],
    caller_conversation_id: str,
    target_id: str,
    server_client: httpx.AsyncClient,
) -> str | None:
    """
    Enforce the close tool's spawn-tree gate over REST.

    Mirrors the in-process :func:`_resolve_session_call` check: the
    target must share the caller's ``root_conversation_id`` and must be
    a sub-agent (have a parent). The caller's own root is resolved via
    its session snapshot — a session can always read itself, so this is
    a 200 on the happy path; a non-200 is surfaced as an error rather
    than failing open. A ``None`` root on either side is treated as
    out-of-tree (never a match).

    :param target_snap: The close target's session snapshot dict (from
        :func:`_fetch_close_target`), carrying ``root_conversation_id``
        and ``parent_session_id``.
    :param caller_conversation_id: The calling session's own id, e.g.
        ``"conv_caller"``.
    :param target_id: The target conversation id, echoed into errors,
        e.g. ``"conv_abc123"``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: ``None`` when the target is in-tree and a sub-agent;
        otherwise a JSON error string (``session_out_of_tree`` or
        ``session_not_a_sub_agent``).
    """
    try:
        caller_snap = await server_client.get(
            f"/v1/sessions/{caller_conversation_id}", timeout=30.0
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_session_close failed: {exc}"})
    if caller_snap.status_code != 200:
        return json.dumps(
            {
                "error": "sys_session_close could not resolve caller session "
                f"{caller_conversation_id!r}"
            }
        )
    caller_root = caller_snap.json().get("root_conversation_id")
    target_root = target_snap.get("root_conversation_id")
    if caller_root is None or target_root != caller_root:
        return json.dumps({"error": "session_out_of_tree", "conversation_id": target_id})
    if target_snap.get("parent_session_id") is None:
        return json.dumps({"error": "session_not_a_sub_agent", "conversation_id": target_id})
    return None

async def _session_close_via_rest(
    args: dict[str, Any],
    conversation_id: str,
    server_client: httpx.AsyncClient,
) -> str:
    """
    Close a target sub-agent via ``GET`` snapshot + ``PATCH`` metadata.

    Mirrors :class:`SysSessionCloseTool` — including its tree-scoping:
    close is a write, so the target MUST share the caller's spawn tree
    (same ``root_conversation_id``) and MUST itself be a sub-agent (have
    a parent). Without this the REST path would let an agent tombstone
    any session it merely has edit access to — e.g. a sub-agent in one
    of the caller's *other*, unrelated spawn trees — which the in-process
    path forbids. The gate lives in the close tool (via
    :func:`_close_tree_scope_error`), not the PATCH route, because the
    route is a general title/metadata mutator; only the close tool
    carries the spawn-tree contract.

    On success marks the child with ``omnigent.closed=true`` and
    rewrites its internal title to ``"<agent>:<title>:closed:<id>"`` so
    future ``sys_session_send`` calls with the same ``(agent, title)``
    create a fresh child.

    :param args: Parsed tool arguments; requires ``conversation_id``.
    :param conversation_id: The calling session's own id, e.g.
        ``"conv_caller"``. Used to resolve the caller's spawn-tree root
        for the tree-scope check.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: JSON ``{"closed": true, ...}`` on success; a JSON error
        object otherwise: ``session_not_found`` (404),
        ``session_out_of_tree`` (403/401, or the target's root differs
        from the caller's), or ``session_not_a_sub_agent`` (the target
        is a top-level session, not a sub-agent).
    """
    target_id = args.get("conversation_id")
    if not isinstance(target_id, str) or not target_id:
        return json.dumps(
            {"error": "sys_session_close requires a non-empty 'conversation_id' string"}
        )
    target_snap = await _fetch_close_target(target_id, server_client)
    if isinstance(target_snap, str):
        return target_snap
    scope_error = await _close_tree_scope_error(
        target_snap, conversation_id, target_id, server_client
    )
    if scope_error is not None:
        return scope_error
    parsed = _parse_session_title(target_snap.get("title"))
    if parsed.agent is None or parsed.title is None:
        return json.dumps({"error": "session_not_a_sub_agent", "conversation_id": target_id})
    new_title = f"{parsed.agent}:{parsed.title}{_CLOSED_TITLE_INFIX}{target_id}"
    try:
        patch = await server_client.patch(
            f"/v1/sessions/{target_id}",
            json={
                "title": new_title,
                "labels": {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE},
            },
            timeout=30.0,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_session_close failed: {exc}"})
    if patch.status_code != 200:
        return json.dumps({"error": f"sys_session_close returned {patch.status_code}"})
    return json.dumps(
        {
            "closed": True,
            "conversation_id": target_id,
            "agent": parsed.agent,
            "title": parsed.title,
        }
    )

@dataclass
class _PeekMeta:
    """
    Session metadata peek reads off the target's ``GET /v1/sessions/{id}``.

    :param agent: Parsed agent/tool segment of the title, e.g.
        ``"researcher"``; ``None`` when the title isn't sub-agent-shaped.
    :param title: Parsed instance label segment, e.g. ``"auth"``;
        ``None`` in the same case.
    :param pending_elicitations: Outstanding
        ``response.elicitation_request`` event payloads the target is
        parked on, replayed on the snapshot from the Omnigent server's
        :mod:`omnigent.runtime.pending_elicitations` index. Empty list
        when the target has none (or the snapshot couldn't be read).
    """

    agent: str | None
    title: str | None
    pending_elicitations: list[dict[str, Any]]

async def _fetch_peek_meta(
    target_id: str,
    server_client: httpx.AsyncClient,
) -> _PeekMeta:
    """
    Fetch a session's title + pending elicitations for peek output.

    One snapshot read serves both peek's ``agent``/``title`` labels and
    the parked-elicitation items it appends. Best-effort: returns empty
    fields when the snapshot can't be read, so a miss degrades
    gracefully (peek still returns the stored item tail) rather than
    failing the whole call.

    :param target_id: The session whose snapshot to read.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: The parsed title plus any outstanding elicitation
        payloads (all empty/``None`` on any miss).
    """
    try:
        snap = await server_client.get(f"/v1/sessions/{target_id}", timeout=30.0)
    except Exception:  # noqa: BLE001
        return _PeekMeta(agent=None, title=None, pending_elicitations=[])
    if snap.status_code != 200:
        return _PeekMeta(agent=None, title=None, pending_elicitations=[])
    body = snap.json()
    parsed = _parse_session_title(body.get("title"))
    raw_pending = body.get("pending_elicitations")
    pending = (
        [e for e in raw_pending if isinstance(e, dict)] if isinstance(raw_pending, list) else []
    )
    return _PeekMeta(agent=parsed.agent, title=parsed.title, pending_elicitations=pending)

