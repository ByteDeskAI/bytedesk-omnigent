"""SQLAlchemy-backed conversation store."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    asc,
    delete,
    desc,
    func,
    literal_column,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.orm import QueryableAttribute, Session
from sqlalchemy.sql.selectable import Subquery

from omnigent._wrapper_labels import UI_MODE_LABEL_KEY, WRAPPER_LABEL_KEY
from omnigent.db.converters import sql_agent_to_entity
from omnigent.db.db_models import (
    SqlAgent,
    SqlConversation,
    SqlConversationEventAudit,
    SqlConversationItem,
    SqlConversationLabel,
    SqlUserDailyCost,
)
from omnigent.db.utils import (
    delete_fts_by_conversation,
    ensure_fts_table,
    extract_search_text,
    generate_conversation_id,
    generate_item_id,
    get_or_create_engine,
    insert_fts,
    make_managed_session_maker,
    now_epoch,
    strip_nul_bytes,
)
from omnigent.entities import (
    Conversation,
    ConversationItem,
    NewConversationItem,
    PagedList,
    parse_item_data,
)
from omnigent.stores.conversation_store import (
    _INSTANCE_SCOPED_LABEL_KEYS,
    FORK_CARRY_HISTORY_LABEL_KEY,
    FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
    FORK_SOURCE_LABEL_KEY,
    SWITCH_PREVIOUS_BUILTIN_LABEL_KEY,
    ConversationEventAudit,
    ConversationNotFoundError,
    ConversationStore,
    CreatedSession,
    NewConversationEventAudit,
    SessionConnectivity,
)


def _to_conversation(
    row: SqlConversation,
    labels: dict[str, str] | None = None,
) -> Conversation:
    """
    Convert a :class:`SqlConversation` ORM row to a
    :class:`Conversation` entity.

    :param row: The SQLAlchemy ORM row to convert.
    :param labels: Pre-fetched guardrails labels for this
        conversation. ``None`` means "no label fetch was
        performed" (callers that don't need labels pass
        ``None`` rather than forcing a second query); this
        maps to an empty dict on the entity. Populated
        callers pass the JOINed ``{key: value}`` map.
    :returns: A :class:`Conversation` dataclass instance.
    """
    import json

    session_state: dict[str, Any] = {}
    if row.session_state:
        session_state = json.loads(row.session_state)
    session_usage: dict[str, Any] = {}
    if row.session_usage:
        session_usage = json.loads(row.session_usage)
    return Conversation(
        id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        title=row.title,
        kind=row.kind,
        parent_conversation_id=row.parent_conversation_id,
        root_conversation_id=row.root_conversation_id,
        agent_id=row.agent_id,
        runner_id=row.runner_id,
        host_id=row.host_id,
        tenant_id=row.tenant_id,
        external_key=row.external_key,
        labels=labels if labels is not None else {},
        session_state=session_state,
        session_usage=session_usage,
        reasoning_effort=row.reasoning_effort,
        model_override=row.model_override,
        cost_control_mode_override=row.cost_control_mode_override,
        harness_override=row.harness_override,
        sub_agent_name=row.sub_agent_name,
        external_session_id=row.external_session_id,
        # NULL → None; a stored JSON array (e.g. ``"[]"`` or
        # ``'["--foo"]'``) decodes back to a list. ``"[]"`` is a
        # non-empty, truthy string, so an explicitly-empty arg list
        # round-trips as ``[]`` and stays distinct from NULL/None.
        terminal_launch_args=(
            json.loads(row.terminal_launch_args) if row.terminal_launch_args is not None else None
        ),
        workspace=row.workspace,
        git_branch=row.git_branch,
        archived=row.archived,
        version=row.version,
    )

def _new_session_conversation_row(
    conversation_id: str,
    now: int,
    title: str | None,
    reasoning_effort: str | None,
    workspace: str | None = None,
    terminal_launch_args: list[str] | None = None,
    parent_conversation_id: str | None = None,
    root_conversation_id: str | None = None,
    runner_id: str | None = None,
    tenant_id: str | None = None,
    external_key: str | None = None,
) -> SqlConversation:
    """
    Build the conversation row for atomic session creation.

    :param conversation_id: New conversation id, e.g.
        ``"conv_abc123"``.
    :param now: Unix epoch seconds used for created/updated fields.
    :param title: Optional session title.
    :param reasoning_effort: Optional per-session reasoning-effort
        hint, e.g. ``"high"``. ``None`` means use the agent
        default.
    :param workspace: Optional starting cwd, e.g.
        ``"/Users/corey/projects/myapp"`` (recorded for CLI
        sessions whose runner is launched locally). ``None``
        leaves the column NULL.
    :param terminal_launch_args: Optional pass-through CLI args for a
        native terminal wrapper, e.g.
        ``["--dangerously-skip-permissions"]``. ``None`` leaves the
        column NULL; a list (including ``[]``) is JSON-encoded.
    :param parent_conversation_id: Optional parent conversation id,
        e.g. ``"conv_parent1"``. When set, the row is created as a
        sub-agent child (``kind="sub_agent"``); ``None`` creates a
        top-level row.
    :param root_conversation_id: Root of the spawn tree, e.g.
        ``"conv_root1"``. Required (resolved from the parent row)
        when ``parent_conversation_id`` is set; ``None`` for
        top-level rows, where the root mirrors the primary key.
    :param runner_id: Optional runner binding inherited from the
        parent session, e.g. ``"runner_abc123"``. ``None`` leaves
        the column NULL.
    :returns: Unsaved :class:`SqlConversation` row.
    """
    return SqlConversation(
        id=conversation_id,
        created_at=now,
        updated_at=now,
        title=title,
        kind="sub_agent" if parent_conversation_id else "default",
        parent_conversation_id=parent_conversation_id,
        # Top-level row: ``root_conversation_id`` mirrors the
        # primary key so tree-scoped lookups treat it as its own
        # root. Child rows inherit their parent's root.
        root_conversation_id=root_conversation_id or conversation_id,
        agent_id=None,
        runner_id=runner_id,
        tenant_id=tenant_id,
        external_key=external_key,
        reasoning_effort=reasoning_effort,
        terminal_launch_args=(
            json.dumps(terminal_launch_args) if terminal_launch_args is not None else None
        ),
        workspace=workspace,
    )

def _new_session_agent_row(
    *,
    agent_id: str,
    agent_name: str,
    agent_bundle_location: str,
    agent_description: str | None,
    conversation_id: str,
    now: int,
) -> SqlAgent:
    """
    Build the session-scoped agent row for atomic creation.

    :param agent_id: New agent id, e.g. ``"ag_abc123"``.
    :param agent_name: Agent name loaded from the uploaded spec.
    :param agent_bundle_location: Artifact-store key for the bundle.
    :param agent_description: Optional description from the spec.
    :param conversation_id: Owning conversation id.
    :param now: Unix epoch seconds used for the created field.
    :returns: Unsaved :class:`SqlAgent` row.
    """
    return SqlAgent(
        id=agent_id,
        created_at=now,
        name=agent_name,
        bundle_location=agent_bundle_location,
        version=1,
        description=agent_description,
        session_id=conversation_id,
    )

def _created_session_from_rows(
    conversation_row: SqlConversation,
    agent_row: SqlAgent,
    labels: dict[str, str] | None,
) -> CreatedSession:
    """
    Convert committed session creation rows to store entities.

    :param conversation_row: Inserted conversation row.
    :param agent_row: Inserted session-scoped agent row.
    :param labels: Labels written during creation, or ``None``.
    :returns: :class:`CreatedSession` with entity objects.
    """
    return CreatedSession(
        conversation=_to_conversation(
            conversation_row,
            labels if labels is not None else {},
        ),
        agent=sql_agent_to_entity(agent_row),
    )

def _upsert_labels(
    session: Session,
    conversation_id: str,
    updates: dict[str, str],
    updated_at: int,
) -> None:
    """
    Atomically UPSERT multiple labels on one conversation.

    Dialect-aware: SQLite and PostgreSQL both support
    ``INSERT ... ON CONFLICT ... DO UPDATE``, so we use
    their dedicated INSERT builders. Other dialects fall
    back to a SELECT-then-INSERT/UPDATE path, which is
    race-safe inside one transaction under SERIALIZABLE or
    (for SQLite) its default single-writer semantics.

    :param session: Active SQLAlchemy session (the atomic
        unit of work).
    :param conversation_id: Owning conversation ID.
    :param updates: Non-empty dict of label key → value.
    :param updated_at: Timestamp to write on every row
        touched by this call.
    """
    dialect = session.bind.dialect.name if session.bind is not None else ""
    rows = [
        {
            "conversation_id": conversation_id,
            "key": key,
            "value": value,
            "updated_at": updated_at,
        }
        for key, value in updates.items()
    ]
    if dialect in ("sqlite", "postgresql"):
        _dialect_upsert_labels(session, dialect, rows)
        return
    # Generic dialect fallback — SELECT-then-INSERT/UPDATE in
    # one transaction. Safe for the v1 "one active workflow
    # per conversation" invariant (POLICIES.md §10); the
    # SQLite / Postgres dialect-specific paths above give
    # true atomic UPSERT for the supported production dbs.
    for row in rows:
        existing = session.get(
            SqlConversationLabel,
            (row["conversation_id"], row["key"]),
        )
        if existing is None:
            session.add(SqlConversationLabel(**row))
        else:
            # mypy sees existing.{value,updated_at} as the
            # Mapped[...] descriptor types; at runtime these
            # are plain attributes that accept the target
            # Python type directly. SQLAlchemy's ORM handles
            # the coercion.
            existing.value = row["value"]  # type: ignore[assignment]
            existing.updated_at = row["updated_at"]  # type: ignore[assignment]

def _dialect_upsert_labels(
    session: Session,
    dialect: str,
    rows: list[dict[str, Any]],
) -> None:
    """
    Dialect-specific UPSERT path for SQLite / PostgreSQL.

    Extracted from ``_upsert_labels`` so the two branches
    (which use different ``insert`` builders producing
    incompatible type variances at the mypy level) each live
    in their own narrow scope. The outer function selects the
    branch; this one executes it.

    :param session: Active SQLAlchemy session.
    :param dialect: ``"sqlite"`` or ``"postgresql"`` (the
        outer function gates all other dialects onto the
        generic fallback path).
    :param rows: Pre-built row dicts to upsert.
    """
    # Typed as Any to sidestep the mypy variance issue between
    # the two dialect-specific ``Insert`` classes; the runtime
    # shape of both classes is identical for our use.
    stmt: Any
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = sqlite_insert(SqlConversationLabel).values(rows)
    else:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(SqlConversationLabel).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["conversation_id", "key"],
        set_={
            "value": stmt.excluded.value,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    session.execute(stmt)

def _fetch_labels(
    session: Session,
    conversation_id: str,
) -> dict[str, str]:
    """
    Load all guardrails labels for a conversation.

    Returns an empty dict when no labels have been written
    yet — a conversation that was created before its spec
    declared guardrails, or before any policy wrote a label.

    :param session: The active SQLAlchemy session.
    :param conversation_id: Unique conversation identifier,
        e.g. ``"conv_abc123"``.
    :returns: Mapping from label key to value (string-typed).
        Empty dict when no rows match.
    """
    rows = session.execute(
        select(SqlConversationLabel.key, SqlConversationLabel.value).where(
            SqlConversationLabel.conversation_id == conversation_id,
        )
    ).all()
    return dict(rows)

def _fetch_labels_bulk(
    session: Session,
    conversation_ids: list[str],
) -> dict[str, dict[str, str]]:
    """
    Load labels for many conversations in a single query.

    Used by ``list_conversations`` to avoid an N+1 fan-out.
    Empty input returns an empty map without touching the
    database.

    :param session: The active SQLAlchemy session.
    :param conversation_ids: Conversation IDs to fetch labels
        for, e.g. ``["conv_a", "conv_b"]``. Duplicates are
        tolerated but yield the same map entries.
    :returns: Mapping ``{conversation_id: {key: value}}``.
        Conversations with no label rows are absent from the
        outer map (callers should default to ``{}``).
    """
    if not conversation_ids:
        return {}
    rows = session.execute(
        select(
            SqlConversationLabel.conversation_id,
            SqlConversationLabel.key,
            SqlConversationLabel.value,
        ).where(SqlConversationLabel.conversation_id.in_(conversation_ids))
    ).all()
    out: dict[str, dict[str, str]] = {}
    for conv_id, key, value in rows:
        out.setdefault(conv_id, {})[key] = value
    return out

def _to_item(row: SqlConversationItem) -> ConversationItem:
    """
    Convert a :class:`SqlConversationItem` ORM row to a
    :class:`ConversationItem` entity.

    Deserializes the JSON ``data`` column and parses it into
    the appropriate typed data model.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`ConversationItem` Pydantic model.
    """
    return ConversationItem(
        id=row.id,
        type=row.type,
        status=row.status,
        response_id=row.response_id,
        created_at=row.created_at,
        data=parse_item_data(row.type, json.loads(row.data)),
        created_by=row.created_by,
    )

def _to_event_audit(row: SqlConversationEventAudit) -> ConversationEventAudit:
    """
    Convert a raw-event audit row into the store entity.

    :param row: SQLAlchemy audit row.
    :returns: A :class:`ConversationEventAudit`.
    """
    return ConversationEventAudit(
        id=row.id,
        conversation_id=row.conversation_id,
        source=row.source,
        event_type=row.event_type,
        provider_event_id=row.provider_event_id,
        response_id=row.response_id,
        call_id=row.call_id,
        message_id=row.message_id,
        raw_payload=json.loads(row.raw_payload),
        canonical_payload=json.loads(row.canonical_payload) if row.canonical_payload else None,
        decision=row.decision,
        conversation_item_id=row.conversation_item_id,
        created_at=row.created_at,
        position=row.position,
    )

def _ranked_latest_message_item_ids(conversation_ids: list[str]) -> Subquery:
    """
    Build a ranked latest-message-id subquery for multiple conversations.

    :param conversation_ids: Conversation ids to fetch messages for,
        e.g. ``["conv_child1", "conv_child2"]``.
    :returns: SQLAlchemy subquery with ``item_id`` and per-conversation
        ``row_num`` columns, newest message first.
    """
    return (
        select(
            SqlConversationItem.id.label("item_id"),
            func.row_number()
            .over(
                partition_by=SqlConversationItem.conversation_id,
                order_by=desc(SqlConversationItem.position),
            )
            .label("row_num"),
        )
        .where(
            SqlConversationItem.conversation_id.in_(conversation_ids),
            SqlConversationItem.type == "message",
        )
        .subquery()
    )

