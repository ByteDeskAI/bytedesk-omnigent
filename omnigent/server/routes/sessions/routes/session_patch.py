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
from .._constants import *
from .._state import *
from .._access import *  # noqa: F403
from .._create import *  # noqa: F403
from .._elicitation import *  # noqa: F403
from .._external_events import *  # noqa: F403
from .._helpers import *  # noqa: F403
from .._list_updates import *  # noqa: F403
from .._managed_launch import *  # noqa: F403
from .._mcp import *  # noqa: F403
from .._native import *  # noqa: F403
from .._policy import *  # noqa: F403
from .._publish import *  # noqa: F403
from .._resources import *  # noqa: F403
from .._runner import *  # noqa: F403
from .._skills import *  # noqa: F403
from .._snapshot import *  # noqa: F403
from .._subagent import *  # noqa: F403
from .._usage import *  # noqa: F403

def register_session_patch(
    router,
    *,
    conversation_store,
    agent_store,
    file_store,
    artifact_store,
    runner_router,
    auth_provider,
    permission_store,
    agent_cache,
    liveness_lookup,
    comment_store,
    runner_tunnel_tokens,
    runner_exit_reports,

):
        # ── PATCH /sessions/{session_id} ────────────────────────────

        @router.patch(
            "/sessions/{session_id}",
            response_model=None,
        )
        async def update_session(
            request: Request,
            response: Response,
            session_id: str,
            body: UpdateSessionRequest,
        ) -> SessionResponse:
            """
            Update a session's mutable fields. When ``runner_id`` is
            provided, this is the mutable affinity primitive for the Alpha
            runner-state pivot: create-bind, resume-bind, and recover-bind
            all send the currently registered runner id, and the server
            atomically replaces ``conversations.runner_id`` with that
            value using last-write-wins semantics. Title, labels, and
            reasoning-effort updates remain supported for existing
            sessions clients.

            :param request: The incoming FastAPI request (for auth).
            :param session_id: Session/conversation identifier,
                e.g. ``"conv_abc123"``.
            :param body: The validated :class:`UpdateSessionRequest`.
            :returns: The updated :class:`SessionResponse` snapshot.
            :raises OmnigentError: 400 if the runner is not
                registered; 404 if no session exists.
            """
            user_id = _get_user_id(request, auth_provider)
            # Archiving/unarchiving is an owner-only lifecycle action: it pairs
            # with a client-driven, owner-gated stop, so an editor must not be
            # able to archive a session (hiding it, and via the client stopping
            # it) when they couldn't issue that stop. Every other field on this
            # endpoint needs only edit. Owner implies edit, so a single check at
            # the level the request actually requires gates both — no redundant
            # second permission-store read for archive/unarchive.
            required_level = LEVEL_OWNER if body.archived is not None else LEVEL_EDIT
            await _require_access(
                user_id, session_id, required_level, permission_store, conversation_store
            )
            if body.runner_id is not None and permission_store is not None:
                if not check_session_access(
                    user_id, session_id, LEVEL_OWNER, permission_store, conversation_store
                ):
                    raise OmnigentError(
                        f"Only the session owner can attach a runner to session {session_id!r}. "
                        f"To fork this session instead, run: omnigent run --fork {session_id}",
                        code=ErrorCode.FORBIDDEN,
                    )
            if body.labels:
                # Advisor-owned cost_control.* labels are written only by the
                # session's bound runner; gate them on runner proof BEFORE any
                # store mutation so a rejected request leaves the session untouched.
                _reserved_labels = reserved_cost_control_keys(body.labels)
                if _reserved_labels:
                    _conv_for_reserved = await asyncio.to_thread(
                        conversation_store.get_conversation, session_id
                    )
                    _require_cost_control_label_authority(
                        reserved_keys=_reserved_labels,
                        tunnel_token=request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER),
                        bound_runner_id=(
                            _conv_for_reserved.runner_id if _conv_for_reserved is not None else None
                        ),
                        allowed_tunnel_tokens=runner_tunnel_tokens,
                        multi_user=permission_store is not None,
                    )
            effort = body.reasoning_effort
            clear_effort = effort in EFFORT_CLEAR_VALUES
            if effort is not None and not clear_effort:
                try:
                    effort = validate_effort(
                        effort,
                        "session metadata",
                        EFFORT_VALUES,
                    )
                except ValueError as exc:
                    raise OmnigentError(
                        f"invalid reasoning_effort: {exc}",
                        code=ErrorCode.INVALID_INPUT,
                    ) from exc

            # Empty / whitespace strings are rejected loud — the only
            # clear path is the explicit ``default | off | reset`` alias.
            model_override = body.model_override
            clear_model = (
                isinstance(model_override, str)
                and model_override.strip().lower() in EFFORT_CLEAR_VALUES
            )
            if model_override is not None and not clear_model:
                # Mirror the create path: the persisted value reaches a native
                # CLI as a ``--model`` argv element and the Codex provider
                # ``config.toml`` as a ``model="..."`` field, so it must pass the
                # conservative model-id charset before it is stored. A bare
                # strip()/non-empty check here let shell-/TOML-shaped values
                # through, enabling host RCE via the Codex ``auth.command``.
                if not isinstance(model_override, str):
                    raise OmnigentError(
                        "invalid model_override: must be a non-empty string",
                        code=ErrorCode.INVALID_INPUT,
                    )
                try:
                    model_override = validate_model_override(model_override)
                except ValueError as exc:
                    raise OmnigentError(
                        f"invalid model_override: {exc}",
                        code=ErrorCode.INVALID_INPUT,
                    ) from exc

            # Cost-control switch: ``"off"`` is a real stored value here,
            # so the clear signal is an explicit JSON null (field present,
            # value None) rather than a clear alias; an omitted field
            # leaves the stored value unchanged.
            clear_cost_control = (
                "cost_control_mode_override" in body.model_fields_set
                and body.cost_control_mode_override is None
            )
            cost_control_mode_override = _validated_cost_control_mode_override(
                body.cost_control_mode_override
            )

            # Native-terminal pass-through args: ``None`` leaves them
            # unchanged; a provided list (including ``[]``) replaces the
            # stored value wholesale (resume is last-write-wins, never an
            # append). Bounds are validated here so a malformed list fails
            # loud at the route rather than at the DB.
            try:
                terminal_launch_args = _validate_terminal_launch_args(body.terminal_launch_args)
            except ValueError as exc:
                raise OmnigentError(
                    f"invalid terminal_launch_args: {exc}",
                    code=ErrorCode.INVALID_INPUT,
                ) from exc

            if body.runner_id is not None:
                # Empty string is the clear sentinel (None = leave unchanged);
                # used by /clear and /switch to move the runner between sessions.
                if body.runner_id == "":
                    try:
                        await asyncio.to_thread(conversation_store.clear_runner_id, session_id)
                    except ConversationNotFoundError as exc:
                        raise OmnigentError(
                            "Session not found",
                            code=ErrorCode.NOT_FOUND,
                        ) from exc
                else:
                    runner_id = _registered_runner_id(runner_router, body.runner_id, user_id=user_id)
                    try:
                        await asyncio.to_thread(
                            conversation_store.replace_runner_id, session_id, runner_id
                        )
                    except ConversationNotFoundError as exc:
                        raise OmnigentError(
                            "Session not found",
                            code=ErrorCode.NOT_FOUND,
                        ) from exc
                    _runner_client = await _get_runner_client(
                        session_id,
                        runner_router,
                    )
                    # Notify the runner about the session so it can
                    # resolve the spec and cache it before the first turn.
                    # This is the design doc's "Server POST /v1/sessions
                    # (to runner)" step from §7 Flow: session creation.
                    conv = conversation_store.get_conversation(
                        session_id,
                    )
                    if _runner_client is not None and conv is not None:
                        try:
                            runner_init_resp = await _runner_client.post(
                                "/v1/sessions",
                                json={
                                    "session_id": session_id,
                                    "agent_id": conv.agent_id,
                                    "sub_agent_name": conv.sub_agent_name,
                                },
                                timeout=10.0,
                            )
                            if runner_init_resp.status_code < 400:
                                _publish_runner_recovered_status(session_id)
                        except (httpx.HTTPError, ConnectionError):
                            # ConnectionError covers a tunnel close mid-POST
                            # (same source as the relay's except clause).
                            _logger.warning(
                                "Failed to notify runner about session %s",
                                session_id,
                                exc_info=True,
                            )
                    if _runner_client is None:
                        # Runner deregistered between validation and
                        # lookup; PATCH still returns 200 but no
                        # relay starts, so log the silent-skip case.
                        _logger.warning(
                            "PATCH rebind to %s on session %s: no runner "
                            "client resolved; relay not restarted.",
                            runner_id,
                            session_id,
                        )
                    # Restart the relay for the new runner; replaces
                    # any relay still pointing at the prior runner.
                    await _ensure_runner_relay_ready(
                        session_id,
                        runner_id,
                        _runner_client,
                        conversation_store,
                    )
            else:
                conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
                if conv is None:
                    raise OmnigentError(
                        "Session not found",
                        code=ErrorCode.NOT_FOUND,
                    )
                if conv.agent_id is None:
                    raise OmnigentError(
                        "Not a session (no agent binding)",
                        code=ErrorCode.NOT_FOUND,
                    )

            from omnigent.server.etag import parse_if_match

            expected_version = parse_if_match(request.headers.get("if-match"))
            updated = await asyncio.to_thread(
                conversation_store.update_conversation,
                session_id,
                title=body.title,
                reasoning_effort=None if clear_effort else effort,
                _unset_reasoning_effort=clear_effort,
                model_override=None if clear_model else model_override,
                _unset_model_override=clear_model,
                cost_control_mode_override=None if clear_cost_control else cost_control_mode_override,
                _unset_cost_control_mode_override=clear_cost_control,
                terminal_launch_args=terminal_launch_args,
                archived=body.archived,
                expected_version=expected_version,
            )
            if updated is None:
                raise OmnigentError(
                    "Session not found",
                    code=ErrorCode.NOT_FOUND,
                )
            response.headers["ETag"] = f'"{updated.version}"'
            # Notify the runner of effort / model changes so harnesses
            # that can't re-read these from store at turn boundaries
            # (today: claude-native, whose ``claude`` binary has
            # ``--effort`` / ``--model`` baked in at spawn) get a chance
            # to propagate them live. Best-effort — persisted values
            # remain the authoritative fallback. Skip both when
            # ``silent`` so bind-time auto-apply doesn't inject visible
            # ``/model X`` items into a fresh pane.
            # Effort and model both go through the unified ``/events``
            # dispatch — Omnigent server stays harness-agnostic; the runner
            # dispatches by harness (claude-native injects the slash
            # command into tmux, other harnesses 204 no-op). See
            # ``_forward_session_change_to_runner`` for the shared
            # runner-client fallback + non-2xx logging.
            live_forward = not body.silent
            if live_forward and (effort is not None or clear_effort):
                await _forward_session_change_to_runner(
                    session_id,
                    runner_router,
                    {"type": "effort_change", "effort": updated.reasoning_effort},
                )
            if live_forward and (model_override is not None or clear_model):
                await _forward_session_change_to_runner(
                    session_id,
                    runner_router,
                    {"type": "model_change", "model": updated.model_override},
                )
                # Append a durable [System: model changed to X] note for sessions
                # whose history Omnigent writes. Gate on the wrapper label (NOT
                # omnigent.ui, which chat-first SDK terminal-view sessions like
                # polly/debby also carry) — see _persist_model_change_note for the
                # full rationale. live_forward (== not silent) already excludes
                # bind-time auto-applies, so only an explicit /model lands a note.
                if not _is_native_terminal_session(updated):
                    await _persist_model_change_note(
                        session_id,
                        updated.model_override,
                        conversation_store,
                    )
            if body.labels is not None and body.labels:
                await asyncio.to_thread(conversation_store.set_labels, session_id, body.labels)
            if body.external_session_id is not None:
                try:
                    await asyncio.to_thread(
                        conversation_store.set_external_session_id,
                        session_id,
                        body.external_session_id,
                    )
                except ConversationNotFoundError as exc:
                    # Race: row vanished between the update above and this
                    # write. Reuse the NOT_FOUND code for consistency.
                    raise OmnigentError(
                        "Session not found",
                        code=ErrorCode.NOT_FOUND,
                    ) from exc
                except ValueError as exc:
                    # Store raises ValueError on attempted overwrite of an
                    # already-set external_session_id — surface as
                    # invalid_input so the caller (a wrapper bridge) sees a
                    # 400 with the conflict explained.
                    raise OmnigentError(
                        str(exc),
                        code=ErrorCode.INVALID_INPUT,
                    ) from exc
            level = await _get_permission_level(user_id, session_id, permission_store)
            return await _get_session_snapshot(
                conversation_store,
                session_id,
                level,
                agent_store,
                agent_cache,
                liveness_lookup=liveness_lookup,
                runner_exit_reports=runner_exit_reports,
            )

