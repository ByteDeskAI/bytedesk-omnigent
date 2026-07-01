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
_INTERRUPT_TYPE: str = "interrupt"
_APPROVAL_TYPE: str = "approval"
_MCP_ELICITATION_TYPE: str = "mcp_elicitation"
_COMPACT_TYPE: str = "compact"
_SLASH_COMMAND_TYPE: str = "slash_command"
_STOP_SESSION_TYPE: str = "stop_session"
_EXTERNAL_ASSISTANT_MESSAGE_TYPE: str = "external_assistant_message"
_EXTERNAL_CONVERSATION_ITEM_TYPE: str = "external_conversation_item"
_EXTERNAL_OUTPUT_TEXT_DELTA_TYPE: str = "external_output_text_delta"
_EXTERNAL_SESSION_INTERRUPTED_TYPE: str = "external_session_interrupted"
_EXTERNAL_ELICITATION_RESOLVED_TYPE: str = "external_elicitation_resolved"
_EXTERNAL_SESSION_STATUS_TYPE: str = "external_session_status"
_EXTERNAL_SESSION_STATUS_VALUES: frozenset[str] = frozenset(
    {"idle", "running", "waiting", "failed"}
)
_EXTERNAL_STATUS_ASSISTANT_SCAN_LIMIT: int = 1000
_EXTERNAL_COMPACTION_STATUS_TYPE: str = "external_compaction_status"
_EXTERNAL_COMPACTION_STATUS_VALUES: frozenset[str] = frozenset(
    {"in_progress", "completed", "failed"}
)
_EXTERNAL_SESSION_USAGE_TYPE: str = "external_session_usage"
_EXTERNAL_MODEL_CHANGE_TYPE: str = "external_model_change"
_EXTERNAL_SUBAGENT_START_TYPE: str = "external_subagent_start"
_CLAUDE_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE = "claude-code-native-ui-subagent"
_CLAUDE_NATIVE_SUBAGENT_ID_LABEL_KEY = "omnigent.claude_native.subagent_id"
_CLAUDE_NATIVE_TOOL_USE_ID_LABEL_KEY = "omnigent.claude_native.tool_use_id"
_CLAUDE_NATIVE_DESCRIPTION_LABEL_KEY = "omnigent.claude_native.description"
_EXTERNAL_CODEX_SUBAGENT_START_TYPE: str = "external_codex_subagent_start"
_CODEX_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE = "codex-native-ui-subagent"
_CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY = "omnigent.codex_native.subagent_thread_id"
_CODEX_NATIVE_SUBAGENT_PARENT_THREAD_ID_LABEL_KEY = "omnigent.codex_native.parent_thread_id"
_CODEX_NATIVE_SUBAGENT_TOOL_CALL_ID_LABEL_KEY = "omnigent.codex_native.collab_tool_call_id"
_CODEX_NATIVE_SUBAGENT_PROMPT_LABEL_KEY = "omnigent.codex_native.prompt"
_CODEX_NATIVE_SUBAGENT_NICKNAME_LABEL_KEY = "omnigent.codex_native.agent_nickname"
_CODEX_NATIVE_SUBAGENT_ROLE_LABEL_KEY = "omnigent.codex_native.agent_role"
_CODEX_NATIVE_SUBAGENT_DISPLAY_FALLBACK = "Codex"
_LAST_CONTEXT_TOKENS_LABEL_KEY: str = "omnigent.last_context_tokens"
_LAST_CONTEXT_WINDOW_LABEL_KEY: str = "omnigent.last_context_window"
_LAST_TASK_ERROR_CODE_LABEL_KEY: str = "omnigent.last_task_error_code"
_LAST_TASK_ERROR_MESSAGE_LABEL_KEY: str = "omnigent.last_task_error_message"
_EXTERNAL_SESSION_TODOS_TYPE: str = "external_session_todos"
_CLAUDE_NATIVE_WRAPPER_LABEL_KEY = "omnigent.wrapper"
_CLAUDE_NATIVE_WRAPPER_LABEL_VALUE = CLAUDE_NATIVE_CODING_AGENT.wrapper_label
_CLAUDE_NATIVE_UI_LABEL_KEY = "omnigent.ui"
_CLAUDE_NATIVE_UI_LABEL_VALUE = "terminal"
_CLAUDE_NATIVE_HARNESS = CLAUDE_NATIVE_CODING_AGENT.harness
_CLAUDE_NATIVE_MODEL = CLAUDE_NATIVE_CODING_AGENT.agent_name
_CODEX_NATIVE_WRAPPER_LABEL_VALUE = CODEX_NATIVE_CODING_AGENT.wrapper_label
_CODEX_NATIVE_HARNESS = CODEX_NATIVE_CODING_AGENT.harness
_CODEX_NATIVE_MODEL = CODEX_NATIVE_CODING_AGENT.agent_name
_CLAUDE_NATIVE_MESSAGE_TIMEOUT_S = 30.0
_NATIVE_TERMINAL_START_FAILED_CODE = "native_terminal_start_failed"
_NATIVE_TERMINAL_ENSURE_FAILED_CODE = "native_terminal_ensure_failed"
_NATIVE_TERMINAL_ENSURE_TIMEOUT_S = 30.0
_NATIVE_TERMINAL_ENSURE_MAX_ATTEMPTS = 4
_NATIVE_TERMINAL_ENSURE_RETRY_DELAY_S = 2.0
_NATIVE_POLICY_NOT_ENFORCED_CODE = "native_policy_not_enforced"
_HOST_BOUND_RUNNER_CONNECT_GRACE_S = 3.0
_HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S = 30.0
_RUNNER_CONVICTION_POLL_S = 0.25
_HOST_LAUNCH_RESULT_TIMEOUT_S = 15.0
_CLAUDE_NATIVE_PERMISSION_HOOK_TIMEOUT_S = 86400.0
_CLAUDE_NATIVE_EDIT_TOOLS: frozenset[str] = frozenset(
    {"Edit", "Write", "MultiEdit", "NotebookEdit"}
)
_CODEX_NATIVE_ELICITATION_HOOK_TIMEOUT_S = 86400.0
_HARNESS_PRE_RESOLVED_ELICITATION_TTL_S = 300.0
_HARNESS_PRE_RESOLVED_ELICITATION_MAX_ENTRIES = 1024
_HARNESS_ELICITATION_REPARK_GRACE_S = 10.0
_CLAUDE_HOOK_ELICITATION_ID_RE = re.compile(r"^elicit_claude_[0-9a-f]{32}$")
_RACE_TASK_REAP_TIMEOUT_S = 5.0
_SESSION_STREAM_HEARTBEAT_INTERVAL_S = 15.0
_SNAPSHOT_RUNNER_TIMEOUT_S = 2.0
_RUNNER_RELAY_READY_TIMEOUT_S = 15.0
_ALLOWED_EVENT_TYPES: frozenset[str] = frozenset(ITEM_TYPE_TO_DATA_CLS.keys()) | {
    _INTERRUPT_TYPE,
    _APPROVAL_TYPE,
    _MCP_ELICITATION_TYPE,
    _COMPACT_TYPE,
    _STOP_SESSION_TYPE,
    _EXTERNAL_ASSISTANT_MESSAGE_TYPE,
    _EXTERNAL_CONVERSATION_ITEM_TYPE,
    _EXTERNAL_OUTPUT_TEXT_DELTA_TYPE,
    _EXTERNAL_SESSION_INTERRUPTED_TYPE,
    _EXTERNAL_ELICITATION_RESOLVED_TYPE,
    _EXTERNAL_SESSION_STATUS_TYPE,
    _EXTERNAL_SESSION_USAGE_TYPE,
    _EXTERNAL_COMPACTION_STATUS_TYPE,
    _EXTERNAL_MODEL_CHANGE_TYPE,
    _EXTERNAL_SESSION_TODOS_TYPE,
    _EXTERNAL_SUBAGENT_START_TYPE,
    _EXTERNAL_CODEX_SUBAGENT_START_TYPE,
}
_SERVER_STREAM_EVENT_ADAPTER: TypeAdapter[ServerStreamEvent] = TypeAdapter(ServerStreamEvent)
_TERMINAL_RESPONSE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "response.completed",
        "response.failed",
        "response.cancelled",
        "response.incomplete",
    }
)
_FENCE_EXEMPT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "response.elicitation_request",
        "response.elicitation_resolved",
    }
)
_SESSION_UPDATES_RESCAN_INTERVAL_S: float = 4.0
_SESSION_UPDATES_HEARTBEAT_INTERVAL_S: float = 30.0
_SESSION_UPDATES_MAX_WATCHED: int = 500
_SHARED_DISCOVERY_KEY = "__all__"
_RUNNER_KEEPALIVE_INTERVAL_S = 300.0
_MODEL_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
_RUNNER_SESSION_INIT_TIMEOUT_S = 10.0
_STOP_RUNNER_RESULT_TIMEOUT_S = 10.0
_DENY_SENTINEL_PREFIX = "[Denied by policy: "
_MAX_TERMINAL_LAUNCH_ARGS = 256
_MAX_TERMINAL_LAUNCH_ARG_LEN = 4096
COST_CONTROL_OVERRIDE_VALUES = frozenset({"on", "off"})
_CHILD_PREVIEW_LIMIT = 150
_UI_ADDED_AGENT_TITLE_PREFIX = "ui"

