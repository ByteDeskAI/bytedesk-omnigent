"""Routes for the Sessions API (``/v1/sessions``).

These endpoints expose a thin, harness-agnostic surface over an
agent's conversation: create a session bound to an agent, post events
(messages, tool outputs, interrupts), read a snapshot, and live-tail
the SSE stream. The session is implemented on top of the existing
conversation-item + task + live-stream machinery — this module is a
boundary translation layer, not a new runtime.

Input dispatch (POST /events) persists the item to
``conversation_items`` and forwards to the bound runner over the WS
tunnel. The persist-before-forward order is invariant I1 in
``designs/SESSION_REARCHITECTURE.md`` — a snapshot read immediately
after POST observes the input in ``items``.

The reconnect contract is **snapshot + live tail**, not replay: a
client opens the live stream and ``GET``s the snapshot, then
deduplicates by item id any events that fire between the two reads.
See ``server/API.md`` for the full contract.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import re
import secrets
import time
import urllib.parse
import weakref
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast

import cachetools
import httpx
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
    status,
)
from fastapi.responses import Response, StreamingResponse
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.datastructures import UploadFile as StarletteUploadFile

from omnigent.blueprints import (
    BlueprintRunner,
    ChildDispatchResult,
    blueprint_events_to_run,
    render_blueprint_value,
)
from omnigent.codex_native_elicitation import codex_elicitation_id
from omnigent.cost_plan import (
    COST_CONTROL_LABEL_NAMESPACE,
    reserved_cost_control_keys,
)
from omnigent.db.utils import generate_agent_id, generate_task_id
from omnigent.entities import (
    Agent,
    BlueprintEventData,
    CommentsFingerprint,
    Conversation,
    ConversationItem,
    ErrorData,
    MessageData,
    NewConversationItem,
    SlashCommandData,
    StoredFile,
    synthesize_conversation_title,
)
from omnigent.entities.conversation import (
    ITEM_TYPE_TO_DATA_CLS,
    NON_CONTENT_ITEM_TYPES,
    FunctionCallData,
    FunctionCallOutputData,
    parse_item_data,
)
from omnigent.entities.permission import SessionPermission
from omnigent.entities.session_resources import session_resource_view_to_dict
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.fabric.runner_fabric import (
    FabricRunnerConflict,
    HostRunnerAcquisition,
    HostWorkerRunnerFabric,
)
from omnigent.host.frames import (
    HARNESS_NOT_CONFIGURED_ERROR_CODE as _HARNESS_NOT_CONFIGURED_ERROR_CODE,
)
from omnigent.model_override import validate_model_override
from omnigent.native_coding_agents import (
    CLAUDE_NATIVE_CODING_AGENT,
    CODEX_NATIVE_CODING_AGENT,
    NativeCodingAgent,
    native_coding_agent_for_agent_name,
    native_coding_agent_for_harness,
    native_coding_agent_for_terminal_name,
    native_coding_agent_for_wrapper_label,
)
from omnigent.policies.types import (
    ElicitationRequest,
    EvaluationContext,
    PolicyAction,
    PolicyResult,
)
from omnigent.reasoning_effort import (
    EFFORT_CLEAR_VALUES,
    EFFORT_VALUES,
    validate_effort,
)
from omnigent.runner.identity import (
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.runner.routing import RunnerRouter
from omnigent.runtime import (
    event_hub,
    get_agent_cache,
    get_caps,
    get_policy_store,
    inflight_text,
    pending_elicitations,
    pending_inputs,
    session_stream,
    user_session_stream,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.policies.approval import (
    _ELICITATION_MODE,
    build_elicitation_request_event,
    resolve_ask_timeout,
)
from omnigent.runtime.policies.builder import build_policy_engine, load_session_usage
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.runtime.tool_output import cap_tool_output
from omnigent.server import presence
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_elicitation_registry,
    _harness_parked_elicitations,
    _harness_pre_resolved_elicitations,
    _ParkedHarnessElicitation,
    _PreResolvedHarnessElicitation,
)
from omnigent.server.agent_refs import require_agent_ref, resolve_agent_ref
from omnigent.server.agent_write import apply_bundle_update
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_MANAGE,
    LEVEL_OWNER,
    LEVEL_READ,
    RESERVED_USER_PUBLIC,
    AuthProvider,
    local_single_user_enabled,
)
from omnigent.server.bundles import bundle_location, validate_agent_bundle
from omnigent.server.conversation_event_sequencer import (
    SequencedPersistResult,
    flush_orphaned_outputs,
    persist_sequenced_item,
)
from omnigent.server.host_registry import HostConnection, HostRegistry, RunnerExitReports
from omnigent.server.managed_hosts import (
    ManagedHostLaunch,
    ManagedLaunch,
    ManagedLaunchTracker,
    ManagedSandboxConfig,
    RepoWorkspace,
)
from omnigent.server.mcp_pool import ServerMcpPool
from omnigent.server.permissions import check_session_access
from omnigent.server.routes._auth_helpers import (
    attribution_user as _attribution_user,
)
from omnigent.server.routes._auth_helpers import (
    get_permission_level as _get_permission_level,
)
from omnigent.server.routes._auth_helpers import (
    get_session_owner_id as _get_session_owner_id,
)
from omnigent.server.routes._auth_helpers import (
    get_user_id as _get_user_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access as _require_access,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._auth_helpers import (
    require_user as _require_user,
)
from omnigent.server.routes._codex_elicitation import parse_codex_elicitation_request
from omnigent.server.routes._content_type import (
    require_json_content_type,
    require_json_or_multipart_content_type,
)
from omnigent.server.routes._host_worktree import CreatedWorktree
from omnigent.server.runner_heal_config import RunnerHealConfig, load_runner_heal_config
from omnigent.server.schemas import (
    AgentObject,
    BlueprintRunResponse,
    ChildSessionSummary,
    ConversationDeleted,
    CreatedSessionResponse,
    ElicitationRequestEvent,
    ElicitationRequestParams,
    ElicitationResult,
    ErrorDetail,
    ErrorEvent,
    GrantPermissionRequest,
    MCPServerSummary,
    ModelUsage,
    OutputItemDoneEvent,
    OutputTextDeltaEvent,
    PaginatedList,
    PermissionObject,
    PolicySummary,
    SandboxStatus,
    ServerStreamEvent,
    SessionAgentChangedEvent,
    SessionCreatedEvent,
    SessionCreateMetadata,
    SessionCreateRequest,
    SessionEventInput,
    SessionForkRequest,
    SessionGitOptions,
    SessionInputConsumedEvent,
    SessionInputConsumedPayload,
    SessionInterruptedEvent,
    SessionInterruptedPayload,
    SessionLabelsResponse,
    SessionListItem,
    SessionModelEvent,
    SessionResourceListPage,
    SessionResourceObject,
    SessionResourcePaginatedList,
    SessionResponse,
    SessionSandboxStatusEvent,
    SessionSkillsEvent,
    SessionStatusEvent,
    SessionSwitchAgentRequest,
    SessionTerminalPendingEvent,
    SessionTodosEvent,
    SessionUsageEvent,
    SkillSummary,
    UpdateSessionRequest,
)
from omnigent.server.subject_token_stash import (
    evict_subject_token,
    stash_subject_token_from_headers,
)
from omnigent.session_lifecycle import (
    is_session_closed,
    labels_with_closed_status,
    title_without_closed_marker,
)
from omnigent.spec.types import (
    AgentSpec,
    BlueprintNode,
    FunctionPolicySpec,
    Phase,
    PolicySpec,
    StateUpdate,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.comment_store import CommentStore
from omnigent.stores.conversation_store import (
    ConversationNotFoundError,
    NameAlreadyExistsError,
)
from omnigent.stores.file_store import FileStore
from omnigent.stores.host_store import Host, HostStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.tools.client_specified import parse_client_side_tool_specs

_logger = logging.getLogger(__name__)
def _import_parent_bindings() -> None:
    from .. import _constants as _parent_constants
    from .. import _state as _parent_state
    g = globals()
    for _mod in (_parent_constants, _parent_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_parent_bindings()

def _publish_runner_skills(session_id: str) -> None:
    """
    Publish a typed :class:`SessionSkillsEvent` to the live stream.

    Fired the moment the background runner-skills fetch
    (:func:`_load_runner_skills`) populates the per-session cache, so a
    connected client can re-read the session snapshot and fill its
    slash-command menu instead of waiting for the next bind. Carries no
    payload beyond the conversation id — it is a "skills resolved,
    re-read the snapshot" nudge; the snapshot's cache-backed ``skills``
    field stays the source of truth.

    No-op when no client is subscribed (``session_stream`` has no
    buffer): a client binding later reads the now-warm snapshot directly.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    """
    event = SessionSkillsEvent(
        type="session.skills",
        conversation_id=session_id,
    )
    session_stream.publish(session_id, event.model_dump())

async def _resolve_skill_meta_text_via_runner(
    session_id: str,
    skill_name: str,
    arguments: str,
    runner_client: httpx.AsyncClient,
) -> str:
    """
    Resolve a skill's hidden ``<skill>`` meta text on the bound runner.

    Skill content is runner-owned: the runner reads the ``SKILL.md``
    body and resource files from the skill's directory on its own
    filesystem, so the embedded ``<path>`` and resource listing are
    valid where the harness executes. Wraps
    ``POST /v1/sessions/{id}/skills/resolve``.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param skill_name: Exact skill name to resolve, e.g.
        ``"code-review"``.
    :param arguments: Raw argument string typed after the slash
        command, e.g. ``"review this plan"``. Empty when none.
    :param runner_client: HTTP client pointed at the bound runner.
    :returns: The hidden ``<skill>`` meta text for a single
        ``input_text`` block.
    :raises OmnigentError: If the skill is not exposed for the session
        (the runner 404s with the available list), or the runner is
        unreachable / errors while resolving.
    """
    try:
        resp = await runner_client.post(
            f"/v1/sessions/{session_id}/skills/resolve",
            json={"name": skill_name, "arguments": arguments},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise OmnigentError(
            f"Runner unreachable while resolving skill {skill_name!r}: {exc}",
            code=ErrorCode.INTERNAL_ERROR,
        ) from exc
    if resp.status_code not in (200, 404):
        raise OmnigentError(
            f"Runner failed to resolve skill {skill_name!r}: HTTP {resp.status_code}",
            code=ErrorCode.INTERNAL_ERROR,
        )
    # Parse the body once, guarded: a transport proxy / HTML error page /
    # non-object body must surface as a controlled runner failure, not an
    # uncaught 500.
    try:
        payload = resp.json()
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object")
    except ValueError as exc:
        raise OmnigentError(
            f"Runner returned a malformed skill resolution for {skill_name!r}: {exc}",
            code=ErrorCode.INTERNAL_ERROR,
        ) from exc
    if resp.status_code == 404:
        available = payload.get("available", [])
        raise OmnigentError(
            f"Skill {skill_name!r} not found. Available skills: {available}",
            code=ErrorCode.INVALID_INPUT,
        )
    meta_text = payload.get("meta_text")
    if not isinstance(meta_text, str):
        raise OmnigentError(
            f"Runner returned malformed skill resolution for {skill_name!r}: missing 'meta_text'",
            code=ErrorCode.INTERNAL_ERROR,
        )
    return meta_text

async def _dispatch_skill_slash_command_to_runner(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    runner_client: httpx.AsyncClient,
    *,
    agent: Agent,
    has_mcp_servers: bool,
    created_by: str | None,
) -> str:
    """
    Persist a skill slash command and forward hidden skill context.

    Skill content is runner-owned: this asks the bound runner to
    resolve the skill (``POST /v1/sessions/{id}/skills/resolve``) into
    its ``<skill>`` meta text, reading the ``SKILL.md`` body and
    resource files from the skill's directory *on the runner* — so the
    embedded ``<path>`` and resource listing are valid where the harness
    executes. The server then persists the result (runner-resolves,
    server-persists). Appends two conversation items with the same
    response id:

    * a visible ``slash_command`` item for the UI transcript;
    * a hidden ``message`` item with ``is_meta=True`` containing the
      full skill instructions for runner history replay.

    Only the hidden message is sent to the runner as input. The visible
    command is published as ``response.output_item.done`` after the
    runner accepts the event.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: Conversation row for ``session_id``.
    :param body: Structured ``slash_command`` event body.
    :param conversation_store: Store used to append both durable
        items.
    :param runner_client: HTTP client pointed at the bound runner.
    :param agent: Agent bound to the conversation.
    :param has_mcp_servers: ``True`` when the agent spec declares MCP
        servers; forwarded unchanged to the runner event.
    :param created_by: Authenticated actor id, e.g.
        ``"alice@example.com"``, or ``None`` in single-user mode.
    :returns: The persisted visible ``slash_command`` item id.
    :raises OmnigentError: If the skill is not exposed for the
        session, or the runner is unreachable while resolving it.
    """
    import uuid

    skill_name, arguments = _parse_skill_slash_command(body)
    meta_text = await _resolve_skill_meta_text_via_runner(
        session_id,
        skill_name,
        arguments,
        runner_client,
    )

    response_id = f"turn_{uuid.uuid4().hex}"
    meta_content = [{"type": "input_text", "text": meta_text}]
    visible_item = NewConversationItem(
        type=_SLASH_COMMAND_TYPE,
        response_id=response_id,
        data=SlashCommandData(
            agent=agent.name,
            kind="skill",
            name=skill_name,
            arguments=arguments,
        ),
        created_by=created_by,
    )
    meta_item = NewConversationItem(
        type="message",
        response_id=response_id,
        data=MessageData(
            role="user",
            content=meta_content,
            is_meta=True,
        ),
        created_by=created_by,
    )
    persisted_items = await asyncio.to_thread(
        conversation_store.append,
        session_id,
        [visible_item, meta_item],
    )
    visible = persisted_items[0]

    # Mirror the plain-message path's title seeding: a session whose FIRST
    # message is a skill invocation (web landing composer, REPL) would
    # otherwise keep a NULL title and the sidebar falls back to the
    # conversation id. Titled from the typed command ("/debate kafka…"),
    # NOT the hidden meta item — that's the full SKILL.md instruction blob.
    command_text = f"/{skill_name} {arguments}" if arguments else f"/{skill_name}"
    await _seed_missing_title(
        conv,
        [{"type": "input_text", "text": command_text}],
        conversation_store,
    )

    runner_body: dict[str, Any] = {
        "type": "message",
        "role": "user",
        "content": meta_content,
        "agent_id": conv.agent_id,
        "model": agent.name,
        "has_mcp_servers": has_mcp_servers,
        # The forwarded message carries ``meta_content`` — i.e. the
        # META item (persisted_items[1]), not the user-visible item.
        # Hand the runner that id so a cold-cache reload drops the
        # right persisted copy (see _forward_event_to_runner).
        "persisted_item_id": persisted_items[1].id,
    }
    effective_runner_override = (
        body.model_override if body.model_override is not None else conv.model_override
    )
    if effective_runner_override is not None:
        runner_body["model_override"] = effective_runner_override
    # Per-session brain-harness override — create-time only, so no
    # per-event value exists; the persisted column is the source.
    if conv.harness_override is not None:
        runner_body["harness_override"] = conv.harness_override

    try:
        await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json=runner_body,
            timeout=10.0,
        )
        event = OutputItemDoneEvent(type="response.output_item.done", item=visible.to_api_dict())
        session_stream.publish(session_id, event.model_dump())
    except httpx.HTTPError:
        _logger.exception(
            "Forward of skill slash command failed for session=%s; "
            "items persisted, runner picks up on reconnect.",
            session_id,
        )
        _publish_status(session_id, "idle")
    return visible.id

async def _fetch_runner_skills(
    runner_client: httpx.AsyncClient | None,
    session_id: str,
) -> list[SkillSummary]:
    """
    Fetch a session's merged skills from its bound runner.

    Skills are runner-owned: the runner discovers them against its own
    filesystem (the spec's bundled skills plus host skills under the
    session's workspace and the runner's ``~/.claude/skills/``). The
    server only overlays the result onto the session snapshot (the web
    composer's slash-command menu).
    Best-effort: a missing/unreachable runner, a non-200, or any
    transport error yields an empty list rather than failing the
    snapshot.

    :param runner_client: HTTP client pointed at the bound runner, or
        ``None`` when no runner is bound.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :returns: Skill summaries (name + one-line description) for the
        session, or ``[]`` when unavailable.
    """
    if runner_client is None:
        return []
    cached = _runner_skills_cache.get(session_id)
    if cached is not None:
        return cached
    # Don't await the runner here: this snapshot is polled continuously
    # (incl. mid-turn), and a per-poll runner round-trip pins the runner's
    # event loop and wedges the turn. Kick one background fetch (single-
    # flight) and return ``[]``; a later poll serves the cached result.
    if session_id not in _runner_skills_inflight:
        task = asyncio.create_task(_load_runner_skills(runner_client, session_id))
        _runner_skills_inflight[session_id] = task
        task.add_done_callback(lambda _t, sid=session_id: _runner_skills_inflight.pop(sid, None))
    return []

async def _load_runner_skills(
    runner_client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """Background single-flight fetch of a session's runner-owned skills.

    Populates :data:`_runner_skills_cache` on success so subsequent
    snapshot polls serve skills without a per-poll runner round-trip. Runs
    off the snapshot's critical path (see :func:`_fetch_runner_skills`).
    Best-effort: transport errors / non-200 / malformed payloads leave the
    cache unset so a later poll retries.

    :param runner_client: HTTP client pointed at the bound runner.
    :param session_id: Session/conversation identifier, e.g. ``"conv_abc"``.
    """
    try:
        resp = await runner_client.get(
            f"/v1/sessions/{session_id}/skills",
            timeout=5.0,
        )
    except httpx.HTTPError:
        _logger.debug("Runner skills query failed for %s", session_id)
        return
    if resp.status_code != 200:
        return
    try:
        raw = resp.json().get("skills", [])
        skills = [SkillSummary(name=s["name"], description=s["description"]) for s in raw]
    except (ValueError, AttributeError, KeyError, TypeError):
        _logger.debug("Runner skills payload malformed for %s", session_id)
        return
    _runner_skills_cache[session_id] = skills
    # Nudge any subscribed client to re-read the (now-warm) snapshot so
    # its slash-command menu fills without waiting for the next bind.
    _publish_runner_skills(session_id)

