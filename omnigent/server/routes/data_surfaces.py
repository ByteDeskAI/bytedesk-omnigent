"""Read-only data-surface routes over internal omnigent state (Phase 9a).

Additive GET endpoints (BDP-2444, ADR-0152) that expose internal state no
other route surfaced — long-term memory, per-session/per-user cost, the
spawn tree, pending elicitations, and fleet health — projected from the
EXISTING data models. Every endpoint reuses the same owner / session
access-scoping the sibling session and host reads use, so it never leaks
another owner's or tenant's data.

Mounted with ``prefix="/v1"`` by :func:`omnigent.server.app.create_app`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from omnigent.db.utils import now_epoch, utc_day
from omnigent.errors import OmnigentError
from omnigent.runtime import pending_elicitations
from omnigent.server import host_access
from omnigent.server.auth import LEVEL_READ, AuthProvider
from omnigent.server.host_access import can_access_host
from omnigent.server.routes._auth_helpers import (
    get_user_id,
    require_access_and_level,
    require_user,
)
from omnigent.server.routes.sessions import (
    _session_status_cache,
    _usage_by_model_for_display,
)
from omnigent.server.schemas import (
    DailyCostSummary,
    FleetHealth,
    MemoryListResponse,
    MemoryObject,
    PendingElicitationItem,
    PendingElicitationsSummary,
    SessionPendingElicitations,
    SpawnTree,
    SpawnTreeMetadata,
    UsageSummary,
)
from omnigent.session_lifecycle import is_session_closed
from omnigent.stores import ConversationStore
from omnigent.stores.host_store import HostStore, host_is_live
from omnigent.stores.memory_store.sqlalchemy_store import SqlAlchemyMemoryStore
from omnigent.stores.permission_store import PermissionStore

# Recursive Pydantic model needs an explicit rebuild once defined (the
# self-referential ``children: list[SpawnTree]`` forward ref).
SpawnTree.model_rebuild()

# Bound the spawn-tree walk so a deep/cyclic graph can't run away. The
# request ``depth`` query is clamped to this.
_MAX_SPAWN_TREE_DEPTH = 50


def _spawn_node_status(conv: Any) -> str:
    """Derive a coarse, honest lifecycle status for a spawn-tree node.

    Prefers the live in-memory status cache (the same signal the session
    snapshot's ``status`` uses); falls back to durable markers so the value is
    meaningful even when no runner is attached on this replica.

    :param conv: The node's :class:`omnigent.entities.Conversation`.
    :returns: One of the live statuses (``"running"`` / ``"waiting"`` /
        ``"idle"`` / ``"failed"``) when cached, else ``"archived"`` /
        ``"closed"`` / ``"active"``.
    """
    cached = _session_status_cache.get(conv.id)
    if cached is not None:
        return cached
    if conv.archived:
        return "archived"
    if is_session_closed(conv.labels, conv.title):
        return "closed"
    return "active"


def create_data_surfaces_router(
    conversation_store: ConversationStore,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    host_store: HostStore | None = None,
) -> APIRouter:
    """Build the read-only data-surface router.

    Stores are closed over (matching the other route factories). Access
    control mirrors the sibling session/host reads: session-scoped endpoints
    require READ on the session via :func:`require_access_and_level`; the
    user-cost and fleet endpoints require an authenticated user and scope to
    that user's data.

    :param conversation_store: Store for conversations and per-user daily cost.
        Its ``storage_location`` is reused to open a read-only memory store.
    :param auth_provider: Auth provider for identity extraction. ``None``
        disables auth (single-user/local), matching sibling routes.
    :param permission_store: Session permission store. ``None`` disables the
        per-session access check (single-user/local).
    :param host_store: Host registration store. Required for the fleet-health
        endpoint; when ``None`` that endpoint reports an empty fleet.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    # Read-only memory store over the SAME DB as the conversation store. Built
    # without an embedder — list_by_source_conversation is a direct column
    # lookup that never touches the (PG-only) semantic path. Lazily so test
    # setups that never call the memory route don't pay for FTS bootstrap.
    _memory_store_holder: dict[str, SqlAlchemyMemoryStore] = {}

    def _memory_store() -> SqlAlchemyMemoryStore:
        store = _memory_store_holder.get("store")
        if store is None:
            store = SqlAlchemyMemoryStore(conversation_store.storage_location)
            _memory_store_holder["store"] = store
        return store

    async def _require_session_read(request: Request, session_id: str) -> Any:
        """Authorize READ on a session and return the fetched conversation.

        Reuses :func:`require_access_and_level` (the sibling session-read
        helper): 401 unauthenticated, 403 insufficient, 404 no access / not
        found — never leaking another owner's session.

        :returns: The conversation (re-fetched if the access helper short-
            circuited it, e.g. for admins or when permissions are disabled).
        """
        user_id = get_user_id(request, auth_provider)
        access = await require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        conv = access.conversation
        if conv is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="session not found")
        return conv

    # ── GET /sessions/{id}/memories ──────────────────────────────────
    @router.get("/sessions/{session_id}/memories", response_model=MemoryListResponse)
    async def list_session_memories(
        request: Request,
        session_id: str,
        scopes: str | None = Query(default=None),  # noqa: ARG001 — reserved filter
        limit: int = Query(default=50, ge=1, le=200),
        sort_by: str = Query(default="created_at", pattern="^(created_at|weight)$"),
    ) -> MemoryListResponse:
        """List long-term memories captured from a session.

        Reads :class:`omnigent.db.db_models.SqlMemory` rows by
        ``source_conversation_id``. ``scopes`` is accepted for forward
        compatibility (memory compartments are scope/owner/topic-keyed, not
        session-keyed) but does not filter session-sourced rows today.

        :param scopes: Reserved compartment-scope filter (currently unused).
        :param limit: Max memories to return (1-200).
        :param sort_by: ``"created_at"`` (default) or ``"weight"``.
        """
        await _require_session_read(request, session_id)
        # Fetch limit+1 so has_more is exact without a second count query.
        rows = await asyncio.to_thread(
            _memory_store().list_by_source_conversation,
            session_id,
            limit=limit + 1,
            sort_by=sort_by,
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        data = [
            MemoryObject(
                id=row.id,
                content=row.content,
                weight=row.weight,
                salience=row.salience,
                confidence=row.confidence,
                created_at=row.created_at,
                last_accessed_at=row.last_accessed_at,
                access_count=row.access_count,
                archived=row.archived,
                source_conversation_id=row.source_conversation_id,
            )
            for row in rows
        ]
        return MemoryListResponse(data=data, has_more=has_more)

    # ── GET /sessions/{id}/usage/summary ─────────────────────────────
    @router.get("/sessions/{session_id}/usage/summary", response_model=UsageSummary)
    async def session_usage_summary(
        request: Request,
        session_id: str,
    ) -> UsageSummary:
        """Return cumulative token + cost usage for a session subtree.

        Reuses :func:`omnigent.runtime.policies.builder.load_session_usage`
        (the subtree-summed dict the snapshot displays) so a parent folds in
        its sub-agents.
        """
        from omnigent.runtime.policies.builder import load_session_usage

        await _require_session_read(request, session_id)
        usage = await asyncio.to_thread(load_session_usage, session_id, conversation_store)

        def _as_int(key: str) -> int:
            try:
                return int(usage.get(key, 0) or 0)
            except (TypeError, ValueError):
                return 0

        cost = usage.get("total_cost_usd")
        total_cost: float | None
        try:
            total_cost = float(cost) if cost is not None else None
        except (TypeError, ValueError):
            total_cost = None
        return UsageSummary(
            input_tokens=_as_int("input_tokens"),
            output_tokens=_as_int("output_tokens"),
            cache_read_input_tokens=_as_int("cache_read_input_tokens"),
            cache_creation_input_tokens=_as_int("cache_creation_input_tokens"),
            total_tokens=_as_int("total_tokens"),
            total_cost_usd=total_cost,
            usage_by_model=_usage_by_model_for_display(usage),
        )

    # ── GET /users/{id}/cost/daily ───────────────────────────────────
    @router.get("/users/{user_id}/cost/daily", response_model=DailyCostSummary)
    async def user_daily_cost(
        request: Request,
        user_id: str,
        date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ) -> DailyCostSummary:
        """Return a user's accumulated LLM spend for one UTC day.

        A caller may only read their own daily cost (an admin may read any
        user's). ``date`` defaults to today (UTC).

        :param date: UTC day ``"YYYY-MM-DD"``; defaults to today.
        :raises HTTPException: 403 reading another user's cost without admin.
        """
        caller = require_user(request, auth_provider)
        # Self-only unless admin / auth disabled. The permission store carries
        # the admin flag (resolve_access), consistent with session reads.
        if caller is not None and caller != user_id:
            is_admin = False
            if permission_store is not None:
                is_admin = await asyncio.to_thread(permission_store.is_admin, caller)
            if not is_admin:
                raise HTTPException(status_code=403, detail="not your cost")
        day = date if date is not None else utc_day(now_epoch())
        state = await asyncio.to_thread(
            conversation_store.get_daily_cost_state, user_id, day
        )
        return DailyCostSummary(
            date_utc=day,
            cost_usd=float(state.get("cost_usd", 0.0)),
            ask_approved_usd=float(state.get("ask_approved_usd", 0.0)),
        )

    # ── GET /sessions/{id}/spawn-tree ────────────────────────────────
    @router.get("/sessions/{session_id}/spawn-tree", response_model=SpawnTree)
    async def session_spawn_tree(
        request: Request,
        session_id: str,
        depth: int = Query(default=_MAX_SPAWN_TREE_DEPTH, ge=1, le=_MAX_SPAWN_TREE_DEPTH),
    ) -> SpawnTree:
        """Return a session and its sub-agent descendants as a tree.

        Authorizes READ on the requested session (the same check the
        child-sessions read uses), then walks ``parent_conversation_id``
        within the shared ``root_conversation_id`` — one tree read, built
        in-memory.

        :param depth: Max levels of descendants to include (children of the
            requested node are depth 1).
        """
        conv = await _require_session_read(request, session_id)
        # Load the whole spawn tree in one query, then build edges in memory.
        page = await asyncio.to_thread(
            conversation_store.list_conversations,
            limit=10_000,
            kind=None,
            root_conversation_id=conv.root_conversation_id,
            include_archived=True,
            order="asc",
            sort_by="created_at",
        )
        children_by_parent: dict[str, list[Any]] = {}
        for c in page.data:
            if c.parent_conversation_id is not None:
                children_by_parent.setdefault(c.parent_conversation_id, []).append(c)
        # Newest-first among siblings, matching the child-sessions default.
        for kids in children_by_parent.values():
            kids.sort(key=lambda c: c.created_at, reverse=True)

        def _build(node: Any, remaining_depth: int, seen: set[str]) -> SpawnTree:
            kids: list[SpawnTree] = []
            if remaining_depth > 0:
                for child in children_by_parent.get(node.id, []):
                    if child.id in seen:
                        continue  # cycle guard
                    seen.add(child.id)
                    kids.append(_build(child, remaining_depth - 1, seen))
            agent_type = node.sub_agent_name or (
                "root" if node.parent_conversation_id is None else "sub_agent"
            )
            return SpawnTree(
                session_id=node.id,
                agent_type=agent_type,
                status=_spawn_node_status(node),
                metadata=SpawnTreeMetadata(
                    sub_agent_name=node.sub_agent_name,
                    title=node.title,
                    created_at=node.created_at,
                    last_activity_at=node.updated_at,
                ),
                children=kids,
            )

        return _build(conv, depth, {conv.id})

    # ── GET /elicitations/pending ────────────────────────────────────
    @router.get("/elicitations/pending", response_model=PendingElicitationsSummary)
    async def pending_elicitations_summary(
        request: Request,
        session_ids: str | None = Query(default=None),
    ) -> PendingElicitationsSummary:
        """Summarize outstanding elicitation prompts across accessible sessions.

        Reads the in-memory pending-elicitations index, scoped to sessions the
        caller can READ — a caller never sees another owner's pending prompts.

        :param session_ids: Optional comma-separated allow-list to restrict the
            summary to specific sessions (each still access-checked).
        """
        candidate_ids = pending_elicitations.pending_session_ids()
        if session_ids:
            requested = {s.strip() for s in session_ids.split(",") if s.strip()}
            candidate_ids = [cid for cid in candidate_ids if cid in requested]

        by_session: list[SessionPendingElicitations] = []
        total = 0
        for cid in candidate_ids:
            # Scope: skip any session the caller can't READ. A 401/403/404 from
            # the access helper means "not visible" — drop it silently rather
            # than leaking existence. ``require_access_and_level`` raises
            # ``OmnigentError``; the not-found fallback raises ``HTTPException``.
            try:
                await _require_session_read(request, cid)
            except (OmnigentError, HTTPException):
                continue
            events = pending_elicitations.snapshot_for(cid)
            if not events:
                continue
            items = [
                _peek_to_item(pending_elicitations.project_for_peek(ev)) for ev in events
            ]
            total += len(items)
            by_session.append(
                SessionPendingElicitations(
                    conversation_id=cid,
                    pending_count=len(items),
                    # The index records no per-prompt timestamp.
                    oldest_created_at=None,
                    elicitations=items,
                )
            )
        return PendingElicitationsSummary(total_count=total, by_session=by_session)

    # ── GET /hosts/health ────────────────────────────────────────────
    @router.get("/hosts/health", response_model=FleetHealth)
    async def hosts_health(request: Request) -> FleetHealth:
        """Aggregate health of the hosts the caller can see.

        Scoped exactly like ``GET /v1/hosts`` (owner / visibility-scope
        filtered) so the summary never counts another owner's managed hosts.
        """
        user_id = require_user(request, auth_provider)
        if host_store is None:
            return FleetHealth()
        if user_id is None:
            hosts = await asyncio.to_thread(host_store.list_hosts, "local")
        else:
            scope = host_access.host_visibility_scope()
            if scope == "org-shared":
                all_hosts = await asyncio.to_thread(host_store.list_all_hosts)
                hosts = [h for h in all_hosts if can_access_host(h, user_id, scope=scope)]
            else:
                hosts = await asyncio.to_thread(host_store.list_hosts, user_id)

        now = now_epoch()
        online = 0
        by_provider: dict[str, int] = {}
        last_seen_ages: list[int] = []
        for host in hosts:
            if host_is_live(host, now=now):
                online += 1
            provider = host.sandbox_provider or "external"
            by_provider[provider] = by_provider.get(provider, 0) + 1
            last_seen_ages.append(max(0, now - host.updated_at))
        total = len(hosts)
        avg_age = (sum(last_seen_ages) / len(last_seen_ages)) if last_seen_ages else None
        return FleetHealth(
            total_hosts=total,
            online_hosts=online,
            offline_hosts=total - online,
            hosts_by_sandbox_provider=by_provider,
            avg_last_seen_seconds_ago=avg_age,
        )

    return router


def _peek_to_item(peek: dict[str, Any]) -> PendingElicitationItem:
    """Map a :func:`pending_elicitations.project_for_peek` dict to the API item.

    :param peek: ``{"elicitation_id", "prompt", "fields"?}``.
    :returns: The typed :class:`PendingElicitationItem`.
    """
    return PendingElicitationItem(
        elicitation_id=peek.get("elicitation_id"),
        prompt=peek.get("prompt"),
        fields=peek.get("fields"),
    )
