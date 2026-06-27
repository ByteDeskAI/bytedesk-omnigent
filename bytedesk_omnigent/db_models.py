"""SQLAlchemy models for the ByteDesk durable substrate + agent-org stores (ADR-0143).

Relocated out of the upstream-shared omnigent/db/db_models.py. They share the core
declarative Base (one metadata, one DB); the tables themselves are created by the
hand-written alembic migrations in omnigent/db/migrations/versions/, not these ORM
classes (see _run_migrations in omnigent/db/utils.py — alembic upgrade head runs at
engine creation, create_all is only belt-and-suspenders).
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from omnigent.db.db_models import Base


class SqlPendingWait(Base):
    """A durable awaited signal: a parked session waiting on a keyed signal
    (BDP-2248, ADR-0142).

    ``signal_id`` (the raw ``{runId}:{nodeId}`` colon form, kept unescaped to
    match the platform ``WorkflowSignalClient`` contract) is the **primary key
    and the idempotency key**: a second ``deliver`` of the same id resolves to
    ``AlreadyResolved`` via the guarded conditional UPDATE
    (``... WHERE status='pending'`` → rowcount 0 the second time), per the
    ADR-0009 Idempotent Receiver. ``session_id`` is a plain column (no hard FK)
    so the bus is decoupled + unit-testable standalone; orphaned waits are
    reaper-swept.
    """

    __tablename__ = "pending_waits"

    signal_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    key: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    resolved_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    meta: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    __table_args__ = (
        Index("ix_pending_waits_kind_target", "kind", "target"),
        Index("ix_pending_waits_session_status", "session_id", "status"),
        Index("ix_pending_waits_status_expires", "status", "expires_at"),
        CheckConstraint(
            "status in ('pending', 'resolved', 'expired')",
            name="ck_pending_waits_status",
        ),
    )


class SqlAgentMessage(Base):
    """A durable inter-session message / wake payload (BDP-2248, ADR-0142).

    Replaces the ephemeral in-process inbox so a wake survives a runner/process
    restart. An unmatched ``deliver`` is stored here with ``dead_lettered=True``
    (Dead Letter Channel, ADR-0009). ``session_id`` is nullable so a dead-letter
    row is not stranded when no session matched; ``seq`` gives FIFO ordering.
    """

    __tablename__ = "agent_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signal_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    dead_lettered: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    delivered_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    meta: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    __table_args__ = (
        Index(
            "ix_agent_messages_session_delivered_seq",
            "session_id",
            "delivered_at",
            "seq",
        ),
        Index("ix_agent_messages_dead_lettered", "dead_lettered"),
    )


class SqlCronTrigger(Base):
    """A durable scheduled trigger that fires an agent on a cadence (BDP-2250,
    ADR-0142).

    The native cron scheduler (the server ``_lifespan`` loop) finds due triggers
    (``enabled`` AND ``next_fire_at <= now``), **claims** each via a guarded
    UPDATE on ``(id, next_fire_at)`` — exactly-once per fire instant (ADR-0009) —
    and dispatches it by opening/resuming the agent's session and posting
    ``payload`` as a message. Replaces the no-op ``cadence:`` bundle param and the
    stubbed ``sys_timer_set`` (``timer.py`` ``NotImplementedError``). ``agent_id``
    is a plain column (no hard FK) so the scheduler is decoupled + standalone-
    testable, mirroring the signal bus.
    """

    __tablename__ = "cron_triggers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    key: Mapped[str] = mapped_column(String(256), nullable=False)
    schedule_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    schedule_expr: Mapped[str] = mapped_column(String(128), nullable=False)
    next_fire_at: Mapped[int] = mapped_column(Integer, nullable=False)
    last_fired_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    # Monotonic optimistic-concurrency ETag (BDP-2412 / ADR-0150).
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)
    meta: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("agent_id", "key", name="uq_cron_triggers_agent_key"),
        Index("ix_cron_triggers_enabled_next_fire", "enabled", "next_fire_at"),
        CheckConstraint(
            "schedule_kind in ('interval', 'cron', 'once')",
            name="ck_cron_triggers_schedule_kind",
        ),
    )


class SqlFabricOutbox(Base):
    """Durable SQL-to-NATS outbox for fabric jobs (ADR-0009).

    Producers write a canonical fabric envelope here inside the SQL source of
    truth before any NATS publish is attempted. The fabric outbox worker replays
    due rows with ``Nats-Msg-Id = idempotency_key`` and marks rows published only
    after the adapter confirms the publish. This is the transactional bridge
    between SQL-owned schedule claims and NATS-owned execution coordination.
    """

    __tablename__ = "fabric_outbox"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(256), nullable=False)
    payload_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)
    meta: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    __table_args__ = (
        Index("ix_fabric_outbox_status_next_attempt", "status", "next_attempt_at"),
        Index("ix_fabric_outbox_created", "created_at"),
        CheckConstraint(
            "status in ('pending', 'failed', 'published', 'dead_lettered')",
            name="ck_fabric_outbox_status",
        ),
    )


class SqlIdempotencyKey(Base):
    """A durable processed-message marker for at-most-once handling (BDP-2251,
    ADR-0142, aligned ADR-0009/0077).

    A consumer ``claim``s a ``(scope, key)`` before doing work; the composite
    primary key makes the claim atomic — the first claimer inserts (returns
    "claimed, new"), a duplicate delivery hits the PK conflict (returns "already
    handled"). Backs the event-trigger consumers' dedup (replaces the
    per-consumer ``DbSupportTicketIdempotencyStore`` / ``WorkflowTriggerInboxEntry``)
    and any redelivered external event. ``dead_lettered`` marks a claim whose work
    ultimately failed past redelivery.
    """

    __tablename__ = "idempotency_keys"

    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    claimed_at: Mapped[int] = mapped_column(Integer, nullable=False)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    dead_lettered: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    meta: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    __table_args__ = (Index("ix_idempotency_keys_dead_lettered", "dead_lettered"),)


class SqlAgenticInboxEvent(Base):
    """A persisted Agentic Inbox email trigger event (BDP-2455).

    The Worker sends a signed ``email.received`` webhook after an inbound email is
    stored. Omnigent records the event before dispatch so retries are idempotent,
    unmapped mailboxes are visible as dead letters, and a delivered event points
    back to the session it created.
    """

    __tablename__ = "agentic_inbox_events"

    event_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    mailbox_id: Mapped[str] = mapped_column(String(320), nullable=False)
    email_id: Mapped[str] = mapped_column(String(128), nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sender: Mapped[str | None] = mapped_column(String(320), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(512), nullable=True)
    thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    received_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="received")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)
    dispatched_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        Index("ix_agentic_inbox_events_mailbox_status", "mailbox_id", "status"),
        Index("ix_agentic_inbox_events_status_updated", "status", "updated_at"),
        Index("ix_agentic_inbox_events_agent", "agent_id"),
        CheckConstraint(
            "status in ('received', 'dispatched', 'dead_lettered', 'failed')",
            name="ck_agentic_inbox_events_status",
        ),
    )


class SqlInboundEvent(Base):
    """The observable Wire-Tap log of every inbound event (ADR-0155, BDP-2559).

    One row per inbound event across all sources (webhooks, email, signal deliveries).
    ``idempotency_key`` is the primary key — the Idempotent-Receiver guard that
    replaces the three divergent per-consumer dedupe schemes. ``raw_payload`` keeps
    the verbatim body; ``normalized`` the translator-extracted fields. This table is
    additive and observable — it is the source of truth for the live "Inbound Events"
    feed.
    """

    __tablename__ = "inbound_events"

    idempotency_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    type: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    occurred_at: Mapped[int] = mapped_column(Integer, nullable=False)
    received_at: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="received")
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized: Mapped[str | None] = mapped_column(Text, nullable=True)
    headers: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        Index("ix_inbound_events_source_type", "source", "type"),
        Index("ix_inbound_events_status_updated", "status", "updated_at"),
        Index("ix_inbound_events_received", "received_at"),
        CheckConstraint(
            "status in ('received', 'fanned_out', 'duplicate', 'dead_lettered')",
            name="ck_inbound_events_status",
        ),
    )


class SqlInboundEventResult(Base):
    """Per-processor outcome of an inbound event's fan-out (ADR-0155, BDP-2559).

    One row per ``(idempotency_key, processor)`` — which processor saw the event and
    what it did. A failed handle records ``next_retry_at`` for the Dead-Letter reaper;
    at the attempt cap the row (and parent event) become ``dead_lettered``.
    """

    __tablename__ = "inbound_event_results"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    processor: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="ok")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("idempotency_key", "processor", name="uq_inbound_result"),
        Index("ix_inbound_result_retry", "status", "next_retry_at"),
        CheckConstraint(
            "status in ('ok', 'skipped', 'failed', 'dead_lettered')",
            name="ck_inbound_event_results_status",
        ),
    )


class SqlToolStep(Base):
    """A durable deterministic tool-step with retry/timeout-over-session +
    resume-on-restart (BDP-2252, ADR-0142).

    A tool-step is the unit of deterministic work inside an orchestration. Keyed
    idempotently by ``(session_id, step_key)``: it is **claimed** once (status
    ``running``, ``attempts`` incremented, ``deadline_at = now + timeout_seconds``),
    executed, then recorded ``completed`` with its ``result`` — so a replay of the
    same step returns the cached result (deterministic re-entry, **no double side
    effect**) — or ``failed``. Retry-over-session: a failed attempt below
    ``max_attempts`` returns to ``pending`` for the next claim; at the cap it is
    ``failed`` (dead). Resume-on-restart: a ``running`` step whose ``deadline_at``
    has passed (its worker crashed / the process restarted) is reclaimed by the
    boot sweep — back to ``pending`` if attempts remain, else ``failed``. Mirrors
    the signal bus / cron scheduler single-writer guarded-UPDATE shape;
    ``session_id`` is a plain column (no hard FK) so the store is standalone-
    testable.
    """

    __tablename__ = "tool_steps"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    step_key: Mapped[str] = mapped_column(String(256), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deadline_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completed_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    meta: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("session_id", "step_key", name="uq_tool_steps_session_step"),
        Index("ix_tool_steps_status_deadline", "status", "deadline_at"),
        CheckConstraint(
            "status in ('pending', 'running', 'completed', 'failed')",
            name="ck_tool_steps_status",
        ),
    )


class SqlWebhookBinding(Base):
    """Maps an inbound external event to a durable signal (BDP-2249, ADR-0142).

    The signed-webhook ingress (``POST /v1/ingress/{source}``) verifies the HMAC,
    resolves the ``(source, match_key)`` binding, and delivers ``signal_id`` to the
    durable signal bus (BDP-2248) — waking the parked session (e.g. TeamCity
    ``build.finished`` → ``release:{version}``). An unmatched event 404s
    (BDP-1419), never 2xx. ``match_key`` ``"*"`` is a per-source catch-all.
    """

    __tablename__ = "webhook_bindings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    match_key: Mapped[str] = mapped_column(String(256), nullable=False, server_default="*")
    signal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    # Monotonic optimistic-concurrency ETag (BDP-2412 / ADR-0150).
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)
    meta: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("source", "match_key", name="uq_webhook_bindings_source_match"),
        Index("ix_webhook_bindings_source_enabled", "source", "enabled"),
    )


class SqlGoal(Base):
    """A durable backlog goal an agent can pull and own (BDP-2271 C3, ADR-0142).

    The "why-act" substrate: a cron-woken triage agent reads + assigns ``open``
    goals; agents advance them; the accountability loop reads them. ``claim_goal``
    uses a guarded UPDATE on ``(id, status='open')`` so exactly one agent claims a
    goal (ADR-0009), the same shape the signal bus + cron scheduler use.
    """

    __tablename__ = "goals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    owner_agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="organization", default="organization"
    )
    target_id: Mapped[str] = mapped_column(
        String(128), nullable=False, server_default="omnigent", default="omnigent"
    )
    target_label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    readiness_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="immediate", default="immediate"
    )
    activation_state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="ready", default="ready"
    )
    # BDP-2583 cadence: how often the goal is dispatched to its agent.
    # immediate = dispatch once when ready; recurring/until_done = a cron trigger
    # (cadence_expr) re-dispatches on a schedule.
    cadence_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="immediate", default="immediate"
    )
    cadence_expr: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cadence_tz: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # BDP-2585 economics: the goal as an economic unit (org/department/agent tier,
    # parent for roll-up, expected/realized value, confidence, risk tier, and an
    # optional success-condition AST ref). All default so existing goals are
    # unchanged; realized_value_cents is written ONLY by goals.book_outcome.
    tier: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="org", default="org"
    )
    parent_goal_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expected_value_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    realized_value_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    confidence: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="0.5", default=0.5
    )
    risk_tier: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="low", default="low"
    )
    success_condition: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)
    # BDP-2283 (C4 escalation dedup): set when the accountability loop escalates a
    # blocked goal, so it escalates ONCE per blocked episode (not every tick).
    # Reset to NULL on every (re-)transition to 'blocked' so a re-blocked goal
    # escalates again.
    escalated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    meta: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    __table_args__ = (
        Index("ix_goals_status_priority", "status", "priority"),
        Index("ix_goals_owner_status", "owner_agent_id", "status"),
        Index("ix_goals_target_status", "target_kind", "target_id", "status"),
        Index("ix_goals_activation_status", "activation_state", "status"),
        Index("ix_goals_parent", "parent_goal_id"),
        CheckConstraint(
            "status in ('open', 'assigned', 'in_progress', 'blocked', 'done')",
            name="ck_goals_status",
        ),
        CheckConstraint(
            "target_kind in ('organization', 'department', 'agent')",
            name="ck_goals_target_kind",
        ),
        CheckConstraint(
            "readiness_kind in ('immediate', 'dependent', 'deferred')",
            name="ck_goals_readiness_kind",
        ),
        CheckConstraint(
            "activation_state in ('ready', 'waiting', 'paused')",
            name="ck_goals_activation_state",
        ),
        CheckConstraint(
            "cadence_kind in ('immediate', 'recurring', 'until_done')",
            name="ck_goals_cadence_kind",
        ),
    )


class SqlGoalDependency(Base):
    """A condition that frames when a dependent goal is ready to be claimed.

    Dependencies are deliberately soft references. They can point at another
    Omnigent goal, a named system state, or a manual checklist item without
    coupling this table to platform-owned entities.
    """

    __tablename__ = "goal_dependencies"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    goal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    label: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)
    resolved_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    meta: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    __table_args__ = (
        Index("ix_goal_dependencies_goal", "goal_id"),
        Index("ix_goal_dependencies_status", "status"),
        Index("ix_goal_dependencies_goal_status", "goal_id", "status"),
        CheckConstraint(
            "kind in ('manual', 'goal', 'system_state', "
            "'milestone', 'epic', 'github_pr', 'jira_issue')",
            name="ck_goal_dependencies_kind",
        ),
        CheckConstraint(
            "status in ('pending', 'satisfied', 'waived')",
            name="ck_goal_dependencies_status",
        ),
    )


class SqlGoalBudget(Base):
    """A per-scope budget cap + spend + circuit-breaker state (BDP-2585, Phase 3).

    One row per ``(tier, target_id)`` scope (e.g. ``org:omnigent``,
    ``department:sales``). ``cap_cents`` is the spend ceiling; ``spent_cents`` is
    the running reservation total, corrected to actual on ``settle`` and refunded
    on ``replenish`` (a booked outcome refills its tier budget — the flywheel).
    ``circuit_open`` is the manual kill-switch; ``anomaly_threshold_cents`` arms
    the auto-trip (budget burned past the threshold with zero realized value).
    """

    __tablename__ = "goal_budgets"

    tier: Mapped[str] = mapped_column(String(16), primary_key=True)
    target_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    cap_cents: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    spent_cents: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cap_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    spent_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_spawns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    spawns_used: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    anomaly_threshold_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    circuit_open: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class SqlGoalOutcome(Base):
    """The realized-value ledger — one row per booked outcome (BDP-2585, Phase 3).

    Append-only; written ONLY by :func:`bytedesk_omnigent.goals.book_outcome`,
    never by an agent or the optimizer. ``realized_value_cents`` is the cash the
    goal actually produced; ``evidence`` keeps the proof (invoice id, deal ref).
    Booking bumps ``goals.realized_value_cents`` and replenishes the tier budget.
    """

    __tablename__ = "goal_outcomes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    goal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    booked_at: Mapped[int] = mapped_column(Integer, nullable=False)
    realized_value_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_goal_outcomes_goal", "goal_id"),)


class SqlGoalDecision(Base):
    """A replay/audit record of one fund/skip decision in a tick (BDP-2585).

    The optimizer writes one row per goal it considered: the ROI at decision
    time, the budget before/after, the ``reason`` (``funded`` / ``skip_*`` /
    ``approval_required`` / ``paper_trade``) and the spawned session if any. The
    decision log makes the autonomous loop replayable + auditable.
    """

    __tablename__ = "goal_decisions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tick_id: Mapped[str] = mapped_column(String(64), nullable=False)
    goal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    roi_at_decision: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    budget_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    budget_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    spawned_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        Index("ix_goal_decisions_goal", "goal_id"),
        Index("ix_goal_decisions_tick", "tick_id"),
    )


class SqlTask(Base):
    """A durable task: a goal with assignment + execution binding (BDP-2333, ADR-0142).

    Extends the goal shape (durable backlog row, guarded-UPDATE claim) with an explicit
    ``assignee_agent_id`` (who executes it) distinct from ``owner_agent_id`` (who is
    accountable for it) and a ``required_capability`` that gates which agent may be
    assigned. ``claim_task`` uses a guarded UPDATE on ``(id, status='open')`` so exactly
    one agent claims a task (ADR-0009), the same shape SqlGoal + the signal bus use.
    Agent ids + capability are plain columns (soft FKs); ``payload`` is JSON-in-Text.
    """

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    owner_agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    assignee_agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    required_capability: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)
    meta: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    __table_args__ = (
        Index("ix_tasks_status_priority", "status", "priority"),
        Index("ix_tasks_owner_status", "owner_agent_id", "status"),
        Index("ix_tasks_assignee_status", "assignee_agent_id", "status"),
        CheckConstraint(
            "status in ('open', 'assigned', 'in_progress', 'blocked', 'done')",
            name="ck_tasks_status",
        ),
    )


class SqlScoreboardEntry(Base):
    """A durable per-agent ops metric (BDP-2271 C3, ADR-0142).

    One row per ``(agent_id, metric, window)`` — the latest value is upserted.
    Feeds workload rebalance, find-specialist ranking, and the accountability
    loop (the org's scoreboard).
    """

    __tablename__ = "scoreboard_entries"

    agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    metric: Mapped[str] = mapped_column(String(64), primary_key=True)
    window: Mapped[str] = mapped_column(String(32), primary_key=True, server_default="all")
    value: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)
    meta: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    __table_args__ = (Index("ix_scoreboard_metric_window_value", "metric", "window", "value"),)


class SqlPeerMessage(Base):
    """A durable lateral peer message (BDP-2270 C2, ADR-0142).

    Lets an agent ask a peer, escalate sideways, or push up — not just answer
    down an ``allowed_subagents`` tree (the social fabric). Stored per
    ``(from_agent, to_agent | topic)``; ``seq`` gives FIFO. The ``sys_peer_message``
    tool + the always-on wake are the integration follow-up.
    """

    __tablename__ = "peer_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    from_agent: Mapped[str] = mapped_column(String(64), nullable=False)
    to_agent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    topic: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="dm")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    read_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    meta: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    __table_args__ = (
        Index("ix_peer_messages_to_read_seq", "to_agent", "read_at", "seq"),
        Index("ix_peer_messages_topic_seq", "topic", "seq"),
        CheckConstraint("kind in ('dm', 'broadcast', 'escalation')", name="ck_peer_messages_kind"),
    )


class SqlBusinessOutcome(Base):
    """A durable attributed business outcome (BDP-2268 B7, ADR-0142).

    The org's outcome ledger: an append-only record of what an agent actually
    achieved — a won deal, a resolved ticket, a shipped feature — each carrying
    the ``metric`` it rolls into and a ``value`` (deal size, count). Recording an
    outcome upserts the agent's cumulative ``scoreboard_entries`` value for that
    metric, so find-specialist ranking + the accountability loop reflect what
    worked (the org learns who is good at what). Append-only; the scoreboard is
    the derived rollup.
    """

    __tablename__ = "business_outcomes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False, server_default="1")
    ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    meta: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    __table_args__ = (
        Index("ix_business_outcomes_agent_metric", "agent_id", "metric"),
        Index("ix_business_outcomes_kind", "kind"),
    )


class SqlDeliberation(Base):
    """A durable proposal→debate→decision (BDP-2273 C6, ADR-0142).

    The org's decision organ: a company decides by *proposal + debate*, not one
    manager's prompt. A deliberation opens on a ``topic`` with a ``proposal``,
    accumulates positions (``deliberation_positions``) across rounds, and closes
    with a recorded ``decision`` — so "what did we decide about X?" is a durable
    query, not lost in a chat scroll. Decide is a guarded ``open → decided``
    transition (single writer, ADR-0009).
    """

    __tablename__ = "deliberations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    topic: Mapped[str] = mapped_column(String(256), nullable=False)
    proposal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    opened_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    decided_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    meta: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    __table_args__ = (
        Index("ix_deliberations_topic_status", "topic", "status"),
        CheckConstraint(
            "status in ('open', 'decided', 'closed')",
            name="ck_deliberations_status",
        ),
    )


class SqlDeliberationPosition(Base):
    """A position taken in a deliberation round (BDP-2273 C6, ADR-0142).

    One row per (agent, round) contribution — a ``stance`` (for / against / amend)
    plus the argument ``body``. Positions accumulate per ``(deliberation_id,
    round)`` so the debate is reconstructable.
    """

    __tablename__ = "deliberation_positions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    deliberation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    stance: Mapped[str] = mapped_column(String(16), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    meta: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    __table_args__ = (
        Index("ix_deliberation_positions_delib_round", "deliberation_id", "round"),
        CheckConstraint(
            "stance in ('for', 'against', 'amend')",
            name="ck_deliberation_positions_stance",
        ),
    )


class SqlSuppression(Base):
    """A do-not-contact suppression entry (BDP-2278 F3, ADR-0142).

    The org's outreach-compliance floor: an opt-out / GDPR-erasure / hard-bounce /
    complaint that means an address must **never** be contacted again on a channel.
    Keyed ``(channel, address)`` so the check is an O(1) PK lookup the outreach
    path consults before sending. Append-once (idempotent suppress); honoring it is
    the CAN-SPAM/GDPR obligation a sending agent cannot talk its way past.
    """

    __tablename__ = "suppressions"

    channel: Mapped[str] = mapped_column(String(16), primary_key=True)
    address: Mapped[str] = mapped_column(String(320), primary_key=True)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    meta: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "reason in ('unsubscribe', 'gdpr_erasure', 'bounce', 'complaint', 'manual')",
            name="ck_suppressions_reason",
        ),
    )
