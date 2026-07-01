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
from ._constants import *
from ._state import *

def _utc_day(epoch_seconds: int) -> str:
    """
    Convert a Unix epoch timestamp to its UTC calendar day.

    :param epoch_seconds: Unix epoch seconds, e.g. ``1749081600``.
    :returns: The UTC date as ``"YYYY-MM-DD"``, e.g. ``"2026-06-05"``.
    """
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).date().isoformat()

def _record_daily_cost(
    conv: Conversation | None,
    delta_usd: float,
    conversation_store: ConversationStore,
) -> None:
    """
    Add a turn's LLM cost to the session owner's daily rollup.

    A no-op when *delta_usd* is not positive or the session has no
    resolvable owner. Attributes the cost to the session creator
    (:meth:`ConversationStore.get_session_owner`) and buckets it by the
    current UTC day, so a session spanning midnight splits its spend
    across both days. Recorded for every priced turn regardless of
    whether the session runs under a policy — the daily rollup is the
    backing store for the per-user daily cost-budget policy, and is now
    populated universally. (This relies on the conversation store
    implementing the daily-cost methods on every deployment that runs
    this code; the earlier policy gate that kept the managed deployment
    from touching an absent ``user_daily_cost`` table is no longer needed
    now that the managed store backs it.)

    :param conv: The conversation row for the session, or ``None``
        (a no-op — no owner to attribute to).
    :param delta_usd: The turn's cost in USD; ``<= 0`` is a no-op.
    :param conversation_store: Store for the owner lookup and the
        daily-cost UPSERT.
    """
    if conv is None or delta_usd <= 0:
        return
    owner = conversation_store.get_session_owner(conv.id)
    if owner is None:
        return
    from omnigent.db.utils import now_epoch

    conversation_store.add_daily_cost(owner, _utc_day(now_epoch()), delta_usd)

def _priced_cost_for_display(usage: dict[str, Any]) -> float | None:
    """
    Extract ``total_cost_usd`` for client display, or ``None`` when unpriced.

    The key is present only when a turn was priced, so its absence ("—" in
    the UI) is distinct from a priced ``$0.00``. The cost-budget policy is
    unaffected — it reads the value with a ``0.0`` default.

    :param usage: A conversation's ``session_usage`` dict, e.g.
        ``{"input_tokens": 1200, "total_cost_usd": 0.42}`` (priced) or
        ``{"input_tokens": 1200}`` (unpriced — no cost key).
    :returns: The cumulative cost in USD when priced, else ``None``.
    """
    if "total_cost_usd" not in usage:
        return None
    try:
        return float(usage["total_cost_usd"])
    except (TypeError, ValueError):
        # Defensive: a malformed persisted value must not break the
        # snapshot / SSE emit. Treat it as unpriced.
        return None

def _model_usage_bucket(usage: dict[str, Any], model: str) -> dict[str, float]:
    """
    Get-or-create the per-model usage sub-bucket inside ``usage["by_model"]``.

    The nested ``by_model`` map attributes token/cost usage to the specific
    LLM that produced it, keyed on the raw harness-reported model id (faithful
    and simplest — alias normalization is intentionally deferred). This mutates
    ``usage`` in place, creating ``by_model`` and the per-model dict on first
    use, and returns the model's bucket for the caller to increment / set.

    :param usage: The conversation's mutable ``session_usage`` dict.
    :param model: The raw harness model id, e.g. ``"claude-sonnet-4-6"`` or
        ``"databricks-gpt-5-5"``.
    :returns: The mutable per-model bucket, e.g. ``{"input_tokens": 1200}``.
    """
    by_model = usage.setdefault("by_model", {})
    return by_model.setdefault(model, {})

def _add_model_usage_delta(
    bucket: dict[str, float],
    token_deltas: dict[str, int],
    cost_delta: float | None,
) -> None:
    """
    Add one turn's per-model token/cost deltas into a model bucket (ADD).

    Mirrors the flat-counter increments in :func:`_accumulate_session_usage`
    so the per-model totals stay consistent with the flat totals: every flat
    increment is matched by an increment to exactly one model bucket, so the
    sum of per-model buckets equals the flat total. ``cost_delta`` is added
    only when the turn was priced (``None`` otherwise), preserving the
    "priced ⟺ ``total_cost_usd`` key present" contract at the per-model level.

    :param bucket: The model's mutable bucket from :func:`_model_usage_bucket`.
    :param token_deltas: This turn's per-bucket token counts to add, keyed by
        the same names as :data:`_TOKEN_BREAKDOWN_KEYS`, e.g.
        ``{"input_tokens": 1200, "output_tokens": 340, ...}``.
    :param cost_delta: This turn's priced cost in USD to add, or ``None`` when
        the turn was unpriced (the model's cost key stays absent).
    """
    for key, delta in token_deltas.items():
        bucket[key] = bucket.get(key, 0) + delta
    if cost_delta is not None:
        bucket["total_cost_usd"] = bucket.get("total_cost_usd", 0.0) + cost_delta

def _usage_by_model_for_display(usage: dict[str, Any]) -> dict[str, ModelUsage] | None:
    """
    Project the nested ``by_model`` usage map into typed :class:`ModelUsage`.

    Companion to :func:`_token_breakdown_for_display` for the per-model view:
    reads ``usage["by_model"]`` (the subtree-summed map from
    :func:`load_session_usage`) and builds a ``{model_id: ModelUsage}`` dict
    for the API. Token buckets are coerced to ``int`` and ``total_cost_usd``
    to ``float``; an absent bucket stays ``None`` on the model (so a model
    that was never priced has no cost), and malformed values are skipped.

    :param usage: A subtree-summed usage dict, e.g.
        ``{"input_tokens": 1500, "by_model": {"claude-sonnet-4-6":
        {"input_tokens": 1500, "total_cost_usd": 0.42}}}``.
    :returns: The per-model map, or ``None`` when no per-model usage is
        present (so ``exclude_none`` omits the field entirely).
    """
    by_model = usage.get("by_model")
    if not isinstance(by_model, dict) or not by_model:
        return None
    result: dict[str, ModelUsage] = {}
    for model, bucket in by_model.items():
        if not isinstance(bucket, dict):
            continue
        fields: dict[str, Any] = {}
        for key in _MODEL_TOKEN_KEYS:
            value = bucket.get(key)
            if value is None:
                continue
            try:
                fields[key] = int(value)
            except (TypeError, ValueError):
                continue
        cost = _priced_cost_for_display(bucket)
        if cost is not None:
            fields["total_cost_usd"] = cost
        result[model] = ModelUsage(**fields)
    return result or None

def _accumulate_session_usage(
    resp_obj: dict[str, Any],
    session_id: str,
    conversation_store: ConversationStore,
) -> float | None:
    """
    Increment the session's cumulative token counters from a
    ``response.completed`` event's usage data.

    Called synchronously from the relay loop. Reads the current
    persisted ``session_usage``, adds the delta from the
    response's ``usage`` field, and writes the updated totals
    back. No-op when the response carries no usage data.

    Cost is computed when the model's per-token pricing is
    available from the MLflow catalog (looked up once per call
    from the response's ``model`` field). The ``total_cost_usd`` key is
    written **only when pricing is available** — an unpriced session
    leaves it absent (its presence is what distinguishes a priced
    ``$0.00`` from "unpriced"; see :func:`_priced_cost_for_display`).

    :param resp_obj: The ``response`` dict from the
        ``response.completed`` SSE event.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conversation_store: Store for reading and writing
        the ``session_usage`` column.
    :returns: The session's cumulative priced cost in USD after this
        update (for the caller to broadcast on a ``session.usage``
        event), or ``None`` when the session is unpriced or carries no
        usage to accumulate.
    """
    usage_obj = resp_obj.get("usage")
    if not isinstance(usage_obj, dict):
        return None
    input_tokens = usage_obj.get("input_tokens", 0)
    output_tokens = usage_obj.get("output_tokens", 0)
    total_tokens = usage_obj.get("total_tokens", 0)
    if not any((input_tokens, output_tokens, total_tokens)):
        return None

    cache_read_input_tokens = usage_obj.get("cache_read_input_tokens", 0)
    cache_creation_input_tokens = usage_obj.get("cache_creation_input_tokens", 0)

    # Load current cumulative usage from the store.
    conv = conversation_store.get_conversation(session_id)
    current = dict(conv.session_usage) if conv else {}
    current.setdefault("input_tokens", 0)
    current.setdefault("output_tokens", 0)
    current.setdefault("total_tokens", 0)
    current.setdefault("cache_read_input_tokens", 0)
    current.setdefault("cache_creation_input_tokens", 0)

    current["input_tokens"] += input_tokens
    current["output_tokens"] += output_tokens
    current["total_tokens"] += total_tokens
    current["cache_read_input_tokens"] += cache_read_input_tokens
    current["cache_creation_input_tokens"] += cache_creation_input_tokens

    # Compute cost delta if pricing is available for the model. Resolve
    # the model to price with, most-specific first:
    #   1. ``usage.model`` — the model the harness actually used this turn.
    #      Relay executors report it; it's the only signal when the spec
    #      pins no ``llm.model`` (a supervisor that delegates / uses the
    #      harness default), so it's what makes those sessions priceable.
    #   2. the session's ``model_override`` (a ``/model`` switch).
    #   3. the agent spec's ``llm.model`` (the static default).
    # The response's top-level ``model`` is the AGENT NAME, not the LLM
    # model, so it is never used here. The ``total_cost_usd`` key is
    # created only on this priced branch, so an unpriced session never
    # gains a (misleading $0.00) cost key.
    cost_delta = 0.0
    usage_model = usage_obj.get("model")
    llm_model = (
        usage_model
        if isinstance(usage_model, str) and usage_model
        else (conv.model_override if conv and conv.model_override else _resolve_llm_model(conv))
    )
    if llm_model:
        from omnigent.llms.context_window import compute_llm_cost, fetch_model_pricing

        pricing = fetch_model_pricing(llm_model)
        priced = pricing is not None
        if pricing is not None:
            # Cache-aware: usage_obj carries cache_read/cache_creation
            # token counts when the harness reports them; compute_llm_cost
            # prices them at their own (cheaper read / pricier write) rates.
            cost_delta = compute_llm_cost(usage_obj, pricing)
            current["total_cost_usd"] = current.get("total_cost_usd", 0.0) + cost_delta
        # Per-model attribution (ADD). Tokens are attributed whenever the
        # model is known — including unpriced turns — so the per-model token
        # view is complete; cost is attributed only when this model's turn
        # was priced (passing ``None`` otherwise keeps the model's cost key
        # absent, matching the flat "priced ⟺ key present" contract).
        _add_model_usage_delta(
            _model_usage_bucket(current, llm_model),
            {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cache_read_input_tokens": cache_read_input_tokens,
                "cache_creation_input_tokens": cache_creation_input_tokens,
            },
            cost_delta if priced else None,
        )

    conversation_store.set_session_usage(session_id, current)
    # Per-user daily rollup (policy-gated; this is the per-turn delta).
    _record_daily_cost(conv, cost_delta, conversation_store)
    return _priced_cost_for_display(current)

async def _persist_external_session_usage(
    session_id: str,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> int | None:
    """
    Persist and broadcast a token-usage update from a terminal-backed runtime.

    At least one of ``data.context_tokens`` (non-negative int),
    ``data.context_window`` (positive int), or a cumulative usage field
    (:func:`_persist_native_cumulative_usage`) must be present.

    :param session_id: Session/conversation identifier.
    :param body: External session-usage event body.
    :param conversation_store: Store used to upsert the labels.
    :returns: The persisted ``context_tokens`` when present, else ``None``.
    :raises OmnigentError: On missing / malformed fields.
    """
    raw_tokens = body.data.get("context_tokens")
    if raw_tokens is not None and (not isinstance(raw_tokens, int) or raw_tokens < 0):
        raise OmnigentError(
            "external_session_usage data.context_tokens must be a non-negative int",
            code=ErrorCode.INVALID_INPUT,
        )
    raw_window = body.data.get("context_window")
    if raw_window is not None and (not isinstance(raw_window, int) or raw_window <= 0):
        raise OmnigentError(
            "external_session_usage data.context_window must be a positive int",
            code=ErrorCode.INVALID_INPUT,
        )
    _CUMULATIVE_USAGE_KEYS = (
        "cumulative_cost_usd",
        # ``policy_cost_usd`` alone is a valid post: mid-turn the displayed
        # statusLine total (``cumulative_cost_usd``) is frozen, so the
        # forwarder posts only the advancing real-time enforcement cost.
        "policy_cost_usd",
        "cumulative_input_tokens",
        "cumulative_output_tokens",
    )
    has_cumulative = any(body.data.get(k) is not None for k in _CUMULATIVE_USAGE_KEYS)
    if raw_tokens is None and raw_window is None and not has_cumulative:
        raise OmnigentError(
            "external_session_usage requires at least one of "
            "data.context_tokens, data.context_window, or a cumulative usage field",
            code=ErrorCode.INVALID_INPUT,
        )

    # Native harnesses report cumulative cost / tokens (SET semantics) — distinct
    # from the Omnigent relay's per-response accumulation. Persist this session's
    # own cumulative usage (its priced own-cost return is unused — the badge shows
    # the subtree total computed below, not own cost).
    await asyncio.to_thread(
        _persist_native_cumulative_usage,
        session_id,
        body.data,
        conversation_store,
    )

    label_updates: dict[str, str] = {}
    if raw_tokens is not None:
        label_updates[_LAST_CONTEXT_TOKENS_LABEL_KEY] = str(raw_tokens)
    if raw_window is not None:
        label_updates[_LAST_CONTEXT_WINDOW_LABEL_KEY] = str(raw_window)
    await asyncio.to_thread(
        conversation_store.set_labels,
        session_id,
        label_updates,
    )
    # The displayed cost is this session's SUBTREE total (itself + its
    # sub-agents), matching the GET snapshot. A sub-agent persists its spend on
    # its own child conversation, so broadcasting only this session's own cost
    # would drop a parent's badge back to own-cost on every parent flush and
    # hide in-flight sub-agent spend until the next child flush (the badge would
    # oscillate own ⇄ subtree). For a childless session the subtree is just
    # itself, so this equals own cost — one indexed tree query per flush.
    subtree_usage = await asyncio.to_thread(load_session_usage, session_id, conversation_store)
    subtree_cost = _priced_cost_for_display(subtree_usage)
    usage_by_model = _usage_by_model_for_display(subtree_usage)
    # Only include fields that were sent; the client treats absent
    # fields as "no change" so a window-only update doesn't zero tokens.
    # ``total_cost_usd`` is included only when the subtree is priced
    # (``exclude_none`` strips it otherwise) — an unpriced session keeps
    # showing "—" from the snapshot rather than a misleading $0.00.
    event_payload: dict[str, Any] = {
        "type": "session.usage",
        "conversation_id": session_id,
    }
    if raw_tokens is not None:
        event_payload["context_tokens"] = raw_tokens
    if raw_window is not None:
        event_payload["context_window"] = raw_window
    if subtree_cost is not None:
        event_payload["total_cost_usd"] = subtree_cost
    if usage_by_model is not None:
        event_payload["usage_by_model"] = usage_by_model
    event = SessionUsageEvent(**event_payload)
    session_stream.publish(session_id, event.model_dump(exclude_none=True))
    # This session's usage also moves its ANCESTORS' subtree cost (its spend
    # rolls up into every ancestor), so re-publish each ancestor's subtree cost
    # too — otherwise a grandparent's badge wouldn't reflect a deep descendant.
    # No-op for a top-level session (no ancestors). Threaded: it pages the
    # conversation tree per ancestor.
    await asyncio.to_thread(
        _publish_subtree_cost_to_ancestors,
        conversation_store,
        session_id,
    )
    return raw_tokens

COST_CONTROL_OVERRIDE_VALUES = frozenset({"on", "off"})

def _validated_cost_control_mode_override(value: str | None) -> str | None:
    """
    Validate a caller-supplied per-session cost-control switch.

    :param value: The candidate value, e.g. ``"on"``, or ``None``
        when the caller did not set / wants to clear the override.
    :returns: The value unchanged when valid, or ``None``.
    :raises OmnigentError: 400 (``invalid_input``) when *value* is
        anything other than ``"on"``, ``"off"``, or ``None``.
    """
    if value is None or value in COST_CONTROL_OVERRIDE_VALUES:
        return value
    raise OmnigentError(
        f"invalid cost_control_mode_override: {value!r} (expected 'on', 'off', or null to clear)",
        code=ErrorCode.INVALID_INPUT,
    )

def _reject_reserved_cost_control_label_seed(labels: dict[str, str]) -> None:
    """
    Reject a session-create body that seeds policy-owned labels.

    ``cost_control.*`` is the cost advisor's telemetry namespace and its
    only legitimate writer is the session's bound runner — which cannot
    exist yet at create time, so a seed is always a forgery.

    :param labels: The client-supplied initial labels, e.g.
        ``{"team": "ml"}``.
    :raises OmnigentError: 400 when any ``cost_control.*`` key is
        present.
    """
    reserved = reserved_cost_control_keys(labels)
    if reserved:
        raise OmnigentError(
            f"labels {', '.join(repr(key) for key in reserved)} "
            f"are in the policy-owned {COST_CONTROL_LABEL_NAMESPACE}* "
            "namespace and cannot be set at session creation",
            code=ErrorCode.INVALID_INPUT,
        )

def _require_cost_control_label_authority(
    *,
    reserved_keys: Sequence[str],
    tunnel_token: str | None,
    bound_runner_id: str | None,
    allowed_tunnel_tokens: frozenset[str] | None,
    multi_user: bool,
) -> None:
    """
    Authorize a label write touching the policy-owned ``cost_control.*`` keys.

    These are the cost advisor's telemetry labels, so ordinary session
    editors must not set them via PATCH; the advisor's persist proves
    itself with the runner tunnel binding token (allow-listed, or bound
    to this session's runner id — the tunnel route's trust model).
    Single-user servers skip the check: loopback runners may register
    under stable ids unrelated to any token, and there is no second
    identity to forge against.

    :param reserved_keys: The ``cost_control.*`` keys the request tries
        to write, e.g. ``("cost_control.plan",)``. Quoted in the error.
    :param tunnel_token: Value of the ``X-Omnigent-Runner-Tunnel-Token``
        request header, or ``None`` when absent.
    :param bound_runner_id: The session's current ``runner_id``, or
        ``None`` when no runner is bound.
    :param allowed_tunnel_tokens: The server's tunnel-token allow-list,
        or ``None`` when not configured.
    :param multi_user: ``True`` when the server enforces per-user
        permissions (a permission store is configured).
    :raises OmnigentError: 403 when the caller presents no acceptable
        runner proof on a multi-user server.
    """
    if not multi_user:
        return
    keys = ", ".join(repr(key) for key in reserved_keys)
    token = (tunnel_token or "").strip()
    if token:
        if allowed_tunnel_tokens is not None and token in allowed_tunnel_tokens:
            return
        if bound_runner_id is not None and token_bound_runner_id(token) == bound_runner_id:
            return
    raise OmnigentError(
        f"labels {keys} are in the policy-owned "
        f"{COST_CONTROL_LABEL_NAMESPACE}* namespace; only the session's "
        "bound runner may write them",
        code=ErrorCode.FORBIDDEN,
    )

