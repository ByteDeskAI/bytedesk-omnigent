"""Durable goals backlog + ops scoreboard (BDP-2271 C3, ADR-0142).

The "why-act" substrate: a clock without a backlog wakes agents to an empty
desk. ``SqlAlchemyGoalStore`` is the durable backlog a cron-woken triage agent
pulls from (``claim_goal`` is a guarded UPDATE = exactly-once assignment,
ADR-0009) plus the ops scoreboard that workload-rebalance / find-specialist read.

Goals now carry an explicit target (organization / department / agent) and a
readiness frame (immediate / dependent / deferred). Dependent goals keep their
unblock conditions in ``goal_dependencies``. All mutations publish compact
``goal.changed`` deltas so the current Omnigent admin UI and a future Platform
consumer can reconcile from REST snapshots.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update

from bytedesk_omnigent.db_models import SqlGoal, SqlGoalDependency, SqlScoreboardEntry
from bytedesk_omnigent.lifecycle import (
    WorkflowLifecycle,
    WorkflowLifecycleStatus,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)

logger = logging.getLogger(__name__)

_LIFECYCLE = WorkflowLifecycle()
GOAL_EVENT_USER_KEY = "__all__"

TARGET_KINDS = ("organization", "department", "agent")
READINESS_KINDS = ("immediate", "dependent", "deferred")
ACTIVATION_STATES = ("ready", "waiting", "paused")
# BDP-2583 goal cadence: immediate dispatches once when ready; recurring/until_done
# register a cron trigger (cadence_expr) that re-dispatches the goal on a schedule.
CADENCE_KINDS = ("immediate", "recurring", "until_done")
_RECURRING_CADENCES = ("recurring", "until_done")
# ADR-0154 adds milestone/epic/github_pr/jira_issue kinds for goal-delivery DAGs.
DEPENDENCY_KINDS = (
    "manual",
    "goal",
    "system_state",
    "milestone",
    "epic",
    "github_pr",
    "jira_issue",
)
DEPENDENCY_STATUSES = ("pending", "satisfied", "waived")
# BDP-2585 economics.
TIERS = ("org", "department", "agent")
RISK_TIERS = ("low", "medium", "high")
# tier derives from target_kind unless explicitly set.
_TIER_FOR_TARGET = {"organization": "org", "department": "department", "agent": "agent"}

_UNSET = object()


def roi(goal: Goal, *, remaining_budget_cents: int) -> float:
    """Derived ROI (never stored): ``(expected_value_cents * confidence) / budget``.

    ``remaining_budget_cents`` is floored at 1 so a zero/over-spent budget yields a
    large-but-finite ROI rather than dividing by zero (ADR goal-engine §roi).
    """
    return (goal.expected_value_cents * goal.confidence) / max(remaining_budget_cents, 1)


@dataclass(frozen=True)
class GoalDependency:
    """A condition that frames a dependent goal."""

    id: str
    goal_id: str
    kind: str
    ref: str | None
    label: str
    status: str
    created_at: int
    updated_at: int
    resolved_at: int | None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class Goal:
    """A row of ``goals``."""

    id: str
    title: str
    owner_agent_id: str | None
    status: WorkflowLifecycleStatus
    priority: int
    source: str | None
    payload: dict[str, Any] | None
    created_at: int
    updated_at: int
    target_kind: str = "organization"
    target_id: str = "omnigent"
    target_label: str | None = None
    readiness_kind: str = "immediate"
    activation_state: str = "ready"
    cadence_kind: str = "immediate"
    cadence_expr: str | None = None
    cadence_tz: str | None = None
    dependencies: tuple[GoalDependency, ...] = ()
    # BDP-2585 economics (Phase 3): the goal as an economic unit. realized_value_cents
    # is written ONLY by ``book_outcome``.
    tier: str = "org"
    parent_goal_id: str | None = None
    expected_value_cents: int = 0
    realized_value_cents: int = 0
    confidence: float = 0.5
    risk_tier: str = "low"
    success_condition: dict[str, Any] | None = None

    @property
    def attributes(self) -> dict[str, Any]:
        """Typed accessor for ``payload["attributes"]`` (paper-trading, approval state).

        No DB column — economic flags ride in the existing JSON payload next to the
        delivery/hierarchy state (mirrors the resolver's condition-in-payload choice).
        """
        attrs = (self.payload or {}).get("attributes")
        return attrs if isinstance(attrs, dict) else {}


def _loads(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else None


def _validate(name: str, value: str, allowed: Sequence[str]) -> str:
    if value not in allowed:
        raise ValueError(f"invalid {name} {value!r}; expected one of {list(allowed)}")
    return value


def _normalize_target(
    target_kind: str,
    target_id: str | None,
    target_label: str | None,
) -> tuple[str, str, str | None]:
    target_kind = _validate("target_kind", target_kind, TARGET_KINDS)
    if target_kind == "organization":
        return target_kind, target_id or "omnigent", target_label or "Organization"
    if not target_id:
        raise ValueError(f"target_id is required for {target_kind!r} goals")
    return target_kind, target_id, target_label


def _dependency_statuses(rows: Iterable[SqlGoalDependency | GoalDependency]) -> list[str]:
    return [row.status for row in rows]


def _activation_for(readiness_kind: str, dependency_statuses: Sequence[str]) -> str:
    if readiness_kind == "deferred":
        return "paused"
    if readiness_kind == "dependent":
        if dependency_statuses and all(s != "pending" for s in dependency_statuses):
            return "ready"
        return "waiting"
    return "ready"


def _to_dependency(row: SqlGoalDependency) -> GoalDependency:
    return GoalDependency(
        id=row.id,
        goal_id=row.goal_id,
        kind=row.kind,
        ref=row.ref,
        label=row.label,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
        resolved_at=row.resolved_at,
        metadata=_loads(row.meta),
    )


def _to_goal(
    row: SqlGoal,
    dependencies: Iterable[SqlGoalDependency | GoalDependency] | None = None,
) -> Goal:
    dep_snapshot: tuple[GoalDependency, ...] = tuple(
        dep if isinstance(dep, GoalDependency) else _to_dependency(dep)
        for dep in (dependencies or ())
    )
    return Goal(
        id=row.id,
        title=row.title,
        owner_agent_id=row.owner_agent_id,
        status=WorkflowLifecycleStatus(row.status),
        priority=row.priority,
        source=row.source,
        payload=_loads(row.payload),
        created_at=row.created_at,
        updated_at=row.updated_at,
        target_kind=getattr(row, "target_kind", None) or "organization",
        target_id=getattr(row, "target_id", None) or "omnigent",
        target_label=getattr(row, "target_label", None),
        readiness_kind=getattr(row, "readiness_kind", None) or "immediate",
        activation_state=getattr(row, "activation_state", None) or "ready",
        cadence_kind=getattr(row, "cadence_kind", None) or "immediate",
        cadence_expr=getattr(row, "cadence_expr", None),
        cadence_tz=getattr(row, "cadence_tz", None),
        dependencies=dep_snapshot,
        tier=getattr(row, "tier", None) or "org",
        parent_goal_id=getattr(row, "parent_goal_id", None),
        expected_value_cents=getattr(row, "expected_value_cents", None) or 0,
        realized_value_cents=getattr(row, "realized_value_cents", None) or 0,
        confidence=row.confidence if getattr(row, "confidence", None) is not None else 0.5,
        risk_tier=getattr(row, "risk_tier", None) or "low",
        success_condition=_loads(getattr(row, "success_condition", None)),
    )


def _goal_event_payload(
    change: str,
    goal: Goal,
    *,
    dependency: GoalDependency | None = None,
    occurred_at: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "goal.changed",
        "change": change,
        "goalId": goal.id,
        "status": str(goal.status),
        "activationState": goal.activation_state,
        "readinessKind": goal.readiness_kind,
        "targetKind": goal.target_kind,
        "targetId": goal.target_id,
        "targetLabel": goal.target_label,
        "ownerAgentId": goal.owner_agent_id,
        "priority": goal.priority,
        "updatedAt": goal.updated_at,
        "occurredAt": occurred_at if occurred_at is not None else now_epoch(),
    }
    if dependency is not None:
        payload["dependency"] = {
            "id": dependency.id,
            "kind": dependency.kind,
            "ref": dependency.ref,
            "label": dependency.label,
            "status": dependency.status,
        }
    return payload


def _publish_goal_event(
    change: str,
    goal: Goal,
    *,
    dependency: GoalDependency | None = None,
    occurred_at: int | None = None,
) -> None:
    event = _goal_event_payload(change, goal, dependency=dependency, occurred_at=occurred_at)
    try:
        from bytedesk_omnigent.realtime.bridge import emit_goal_change

        emit_goal_change(event)
    except Exception:  # pragma: no cover - best-effort bridge
        logger.exception("failed to publish goal realtime delta")
    try:
        from omnigent.runtime.event_hub import publish

        publish(GOAL_EVENT_USER_KEY, event)
    except Exception:  # pragma: no cover - best-effort local event stream
        logger.exception("failed to publish goal event-hub delta")


class SqlAlchemyGoalStore:
    """Durable goals backlog + ops scoreboard (ADR-0142)."""

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)
        self._write_session = make_managed_session_maker(self._engine, immediate=True)

    @property
    def engine(self):
        return self._engine

    # -- goals ---------------------------------------------------------
    def create_goal(
        self,
        *,
        title: str,
        priority: int = 3,
        source: str | None = None,
        payload: dict[str, Any] | None = None,
        target_kind: str = "organization",
        target_id: str | None = None,
        target_label: str | None = None,
        readiness_kind: str = "immediate",
        cadence_kind: str = "immediate",
        cadence_expr: str | None = None,
        cadence_tz: str | None = None,
        dependencies: Sequence[dict[str, Any]] | None = None,
        tier: str | None = None,
        parent_goal_id: str | None = None,
        expected_value_cents: int = 0,
        confidence: float = 0.5,
        risk_tier: str = "low",
        success_condition: dict[str, Any] | None = None,
        scheduler: Any | None = None,
        now: int | None = None,
    ) -> Goal:
        """Create an ``open`` goal. Lower ``priority`` numbers sort first.

        ``cadence_kind`` defaults to ``immediate`` (dispatch once when ready, no
        trigger — existing behaviour unchanged). ``recurring`` / ``until_done``
        require ``cadence_expr`` (a five-field cron string) and register a goal
        cron trigger via ``scheduler`` (BDP-2583). ``scheduler`` is injectable for
        tests; when omitted for a recurring cadence the canonical cron scheduler
        is resolved lazily.
        """
        if not title.strip():
            raise ValueError("title is required")
        cadence_kind = _validate("cadence_kind", cadence_kind, CADENCE_KINDS)
        if cadence_kind in _RECURRING_CADENCES and not (cadence_expr and cadence_expr.strip()):
            raise ValueError(f"cadence_expr is required for {cadence_kind!r} goals")
        now = now_epoch() if now is None else now
        target_kind, target_id, target_label = _normalize_target(
            target_kind, target_id, target_label
        )
        dependency_specs = list(dependencies or ())
        if dependency_specs and readiness_kind == "immediate":
            readiness_kind = "dependent"
        readiness_kind = _validate("readiness_kind", readiness_kind, READINESS_KINDS)
        dep_statuses = [
            _validate(
                "dependency.status",
                str(spec.get("status") or "pending"),
                DEPENDENCY_STATUSES,
            )
            for spec in dependency_specs
        ]
        activation_state = _activation_for(readiness_kind, dep_statuses)
        tier = _validate("tier", tier or _TIER_FOR_TARGET[target_kind], TIERS)
        risk_tier = _validate("risk_tier", risk_tier, RISK_TIERS)

        goal: Goal
        with self._write_session() as session:
            row = SqlGoal(
                id=f"goal_{uuid.uuid4().hex}",
                title=title.strip(),
                owner_agent_id=None,
                status="open",
                priority=priority,
                source=source,
                payload=json.dumps(payload) if payload is not None else None,
                target_kind=target_kind,
                target_id=target_id,
                target_label=target_label,
                readiness_kind=readiness_kind,
                activation_state=activation_state,
                cadence_kind=cadence_kind,
                cadence_expr=cadence_expr,
                cadence_tz=cadence_tz,
                tier=tier,
                parent_goal_id=parent_goal_id,
                expected_value_cents=expected_value_cents,
                confidence=confidence,
                risk_tier=risk_tier,
                success_condition=(
                    json.dumps(success_condition) if success_condition is not None else None
                ),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            dep_rows: list[SqlGoalDependency] = []
            for spec in dependency_specs:
                kind = _validate(
                    "dependency.kind",
                    str(spec.get("kind") or "manual"),
                    DEPENDENCY_KINDS,
                )
                label = str(spec.get("label") or "").strip()
                if not label:
                    raise ValueError("dependency label is required")
                status = _validate(
                    "dependency.status",
                    str(spec.get("status") or "pending"),
                    DEPENDENCY_STATUSES,
                )
                dep_row = SqlGoalDependency(
                    id=f"goal_dep_{uuid.uuid4().hex}",
                    goal_id=row.id,
                    kind=kind,
                    ref=spec.get("ref"),
                    label=label,
                    status=status,
                    created_at=now,
                    updated_at=now,
                    resolved_at=now if status != "pending" else None,
                    meta=(
                        json.dumps(spec.get("metadata"))
                        if spec.get("metadata") is not None
                        else None
                    ),
                )
                session.add(dep_row)
                dep_rows.append(dep_row)
            session.flush()
            goal = _to_goal(row, dep_rows)
        if cadence_kind in _RECURRING_CADENCES:
            self._register_cadence_trigger(goal, scheduler=scheduler, now=now)
        _publish_goal_event("created", goal, occurred_at=now)
        return goal

    def _register_cadence_trigger(
        self, goal: Goal, *, scheduler: Any | None, now: int
    ) -> None:
        """Register a goal cron trigger for a recurring/until_done goal (BDP-2583).

        The trigger fires on ``cadence_expr``; its payload carries
        ``{goal_id, agent_id, kind:"goal"}`` so :func:`engine.cron.goal_cron_dispatch`
        routes the fire to :func:`engine.dispatcher.dispatch_goal`.
        """
        if scheduler is None:
            from bytedesk_omnigent.runtime import get_cron_scheduler

            scheduler = get_cron_scheduler()
        scheduler.register_trigger(
            agent_id=goal.owner_agent_id or goal.target_id,
            key=f"goal:{goal.id}",
            schedule_kind="cron",
            schedule_expr=goal.cadence_expr or "",
            payload={"goal_id": goal.id, "agent_id": goal.owner_agent_id, "kind": "goal"},
            now=now,
        )

    def get_goal(self, *, goal_id: str, include_dependencies: bool = True) -> Goal | None:
        """Return a single goal by id."""
        with self._session() as session:
            row = session.get(SqlGoal, goal_id)
            if row is None:
                return None
            dependencies = self._dependency_rows(session, [goal_id]).get(goal_id, ())
            return _to_goal(row, dependencies if include_dependencies else ())

    def list_goals(
        self,
        *,
        status: str | None = None,
        owner_agent_id: str | None = None,
        target_kind: str | None = None,
        target_id: str | None = None,
        readiness_kind: str | None = None,
        activation_state: str | None = None,
        ready_only: bool = False,
        include_dependencies: bool = False,
    ) -> list[Goal]:
        """List goals (by priority then age), optionally filtered."""
        stmt = select(SqlGoal)
        if status is not None:
            stmt = stmt.where(SqlGoal.status == status)
        if owner_agent_id is not None:
            stmt = stmt.where(SqlGoal.owner_agent_id == owner_agent_id)
        if target_kind is not None:
            stmt = stmt.where(SqlGoal.target_kind == target_kind)
        if target_id is not None:
            stmt = stmt.where(SqlGoal.target_id == target_id)
        if readiness_kind is not None:
            stmt = stmt.where(SqlGoal.readiness_kind == readiness_kind)
        if activation_state is not None:
            stmt = stmt.where(SqlGoal.activation_state == activation_state)
        if ready_only:
            stmt = stmt.where(SqlGoal.activation_state == "ready")
        stmt = stmt.order_by(SqlGoal.priority, SqlGoal.created_at)
        with self._session() as session:
            rows = session.execute(stmt).scalars().all()
            dependency_map = (
                self._dependency_rows(session, [row.id for row in rows])
                if include_dependencies
                else {}
            )
            return [_to_goal(r, dependency_map.get(r.id, ())) for r in rows]

    def update_goal(self, *, goal_id: str, now: int | None = None, **updates: Any) -> Goal | None:
        """Update goal metadata/status from the admin surface."""
        allowed = {
            "title",
            "priority",
            "payload",
            "status",
            "target_kind",
            "target_id",
            "target_label",
            "readiness_kind",
            "activation_state",
            "tier",
            "parent_goal_id",
            "expected_value_cents",
            "confidence",
            "risk_tier",
            "success_condition",
        }
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"unsupported goal updates: {sorted(unknown)}")

        now = now_epoch() if now is None else now
        goal: Goal | None = None
        with self._write_session() as session:
            row = session.get(SqlGoal, goal_id)
            if row is None:
                return None

            if "status" in updates:
                target = WorkflowLifecycleStatus(str(updates["status"]))
                _LIFECYCLE.check(WorkflowLifecycleStatus(row.status), target)
                row.status = str(target)
                if target is WorkflowLifecycleStatus.BLOCKED:
                    row.escalated_at = None
                if target is WorkflowLifecycleStatus.OPEN:
                    row.owner_agent_id = None
            if "title" in updates:
                title = str(updates["title"]).strip()
                if not title:
                    raise ValueError("title is required")
                row.title = title
            if "priority" in updates:
                row.priority = int(updates["priority"])
            if "payload" in updates:
                row.payload = (
                    json.dumps(updates["payload"]) if updates["payload"] is not None else None
                )

            if any(k in updates for k in ("target_kind", "target_id", "target_label")):
                target_kind, target_id, target_label = _normalize_target(
                    str(updates.get("target_kind", row.target_kind)),
                    updates.get("target_id", row.target_id),
                    updates.get("target_label", row.target_label),
                )
                row.target_kind = target_kind
                row.target_id = target_id
                row.target_label = target_label
            if "readiness_kind" in updates:
                row.readiness_kind = _validate(
                    "readiness_kind", str(updates["readiness_kind"]), READINESS_KINDS
                )
            if "tier" in updates:
                row.tier = _validate("tier", str(updates["tier"]), TIERS)
            if "parent_goal_id" in updates:
                row.parent_goal_id = updates["parent_goal_id"]
            if "expected_value_cents" in updates:
                row.expected_value_cents = int(updates["expected_value_cents"])
            if "confidence" in updates:
                row.confidence = float(updates["confidence"])
            if "risk_tier" in updates:
                row.risk_tier = _validate("risk_tier", str(updates["risk_tier"]), RISK_TIERS)
            if "success_condition" in updates:
                row.success_condition = (
                    json.dumps(updates["success_condition"])
                    if updates["success_condition"] is not None
                    else None
                )
            if "activation_state" in updates:
                row.activation_state = _validate(
                    "activation_state", str(updates["activation_state"]), ACTIVATION_STATES
                )
            else:
                self._refresh_activation(session, row, now)

            row.updated_at = now
            dependencies = self._dependency_rows(session, [goal_id]).get(goal_id, ())
            session.flush()
            goal = _to_goal(row, dependencies)
        if goal is not None:
            _publish_goal_event("updated", goal, occurred_at=now)
        return goal

    def mutate_payload(
        self,
        *,
        goal_id: str,
        mutator: Any,
        now: int | None = None,
    ) -> Goal | None:
        """Atomic read-modify-write of ``goal.payload`` under the write lock (ADR-0009).

        ``mutator(payload: dict) -> None`` mutates the payload **in place** inside
        the same transaction that re-reads the row, so a concurrent writer cannot
        lose an update. ``update_goal(payload=...)`` computes the whole payload
        *outside* the lock and clobbers concurrent writes — use this for any
        delivery transition that flips one key of a shared payload (e.g. the
        two-key milestone gate, BDP-2553). Single-writer on both dialects: the
        ``immediate`` session takes SQLite's write lock before the read, and
        ``with_for_update`` serializes the row on Postgres.
        """
        now = now_epoch() if now is None else now
        goal: Goal | None = None
        with self._write_session() as session:
            row = session.get(SqlGoal, goal_id, with_for_update=True)
            if row is None:
                return None
            payload = _loads(row.payload) or {}
            mutator(payload)
            row.payload = json.dumps(payload)
            row.updated_at = now
            self._refresh_activation(session, row, now)
            dependencies = self._dependency_rows(session, [goal_id]).get(goal_id, ())
            session.flush()
            goal = _to_goal(row, dependencies)
        if goal is not None:
            _publish_goal_event("updated", goal, occurred_at=now)
        return goal

    def activate_goal(self, *, goal_id: str, now: int | None = None) -> Goal | None:
        """Manually make a goal claimable, overriding deferred/dependent framing."""
        return self.update_goal(
            goal_id=goal_id,
            readiness_kind="immediate",
            activation_state="ready",
            now=now,
        )

    def claim_goal(self, *, goal_id: str, owner_agent_id: str, now: int | None = None) -> bool:
        """Atomically assign a ready ``open`` goal. Returns True if THIS caller claimed it."""
        now = now_epoch() if now is None else now
        goal: Goal | None = None
        with self._write_session() as session:
            result = session.execute(
                update(SqlGoal)
                .where(
                    SqlGoal.id == goal_id,
                    SqlGoal.status == "open",
                    SqlGoal.activation_state == "ready",
                )
                .values(status="assigned", owner_agent_id=owner_agent_id, updated_at=now)
            )
            if result.rowcount == 1:
                row = session.get(SqlGoal, goal_id)
                if row is not None:
                    dependencies = self._dependency_rows(session, [goal_id]).get(goal_id, ())
                    goal = _to_goal(row, dependencies)
        if goal is not None:
            _publish_goal_event("claimed", goal, occurred_at=now)
        return goal is not None

    def advance_goal(self, *, goal_id: str, status: str, now: int | None = None) -> None:
        """Move a goal to a new status (``in_progress`` / ``blocked`` / ``done`` ...)."""
        now = now_epoch() if now is None else now
        target = WorkflowLifecycleStatus(status)
        goal: Goal | None = None
        with self._write_session() as session:
            current = session.get(SqlGoal, goal_id)
            if current is not None:
                _LIFECYCLE.check(WorkflowLifecycleStatus(current.status), target)
                current.status = str(target)
                current.updated_at = now
                if target is WorkflowLifecycleStatus.BLOCKED:
                    current.escalated_at = None
                dependencies = self._dependency_rows(session, [goal_id]).get(goal_id, ())
                goal = _to_goal(current, dependencies)
        if goal is not None:
            _publish_goal_event("status_changed", goal, occurred_at=now)

    def advance_goal_owned(
        self, *, goal_id: str, status: str, owner_agent_id: str, now: int | None = None
    ) -> bool:
        """Move a goal the caller OWNS to a new status (BDP-2285 authz)."""
        now = now_epoch() if now is None else now
        target = WorkflowLifecycleStatus(status)
        goal: Goal | None = None
        with self._write_session() as session:
            current = session.get(SqlGoal, goal_id)
            if current is not None and current.owner_agent_id == owner_agent_id:
                _LIFECYCLE.check(WorkflowLifecycleStatus(current.status), target)
            values: dict[str, Any] = {"status": str(target), "updated_at": now}
            if target is WorkflowLifecycleStatus.BLOCKED:
                values["escalated_at"] = None
            if target is WorkflowLifecycleStatus.OPEN:
                values["owner_agent_id"] = None
            result = session.execute(
                update(SqlGoal)
                .where(
                    SqlGoal.id == goal_id,
                    SqlGoal.owner_agent_id == owner_agent_id,
                )
                .values(**values)
            )
            if result.rowcount == 1:
                row = session.get(SqlGoal, goal_id)
                if row is not None:
                    dependencies = self._dependency_rows(session, [goal_id]).get(goal_id, ())
                    goal = _to_goal(row, dependencies)
        if goal is not None:
            _publish_goal_event("status_changed", goal, occurred_at=now)
        return goal is not None

    def add_dependency(
        self,
        *,
        goal_id: str,
        kind: str = "manual",
        label: str,
        ref: str | None = None,
        status: str = "pending",
        metadata: dict[str, Any] | None = None,
        now: int | None = None,
    ) -> GoalDependency | None:
        """Attach a dependency to a goal and recalculate readiness."""
        now = now_epoch() if now is None else now
        kind = _validate("kind", kind, DEPENDENCY_KINDS)
        status = _validate("status", status, DEPENDENCY_STATUSES)
        label = label.strip()
        if not label:
            raise ValueError("label is required")

        dependency: GoalDependency | None = None
        goal: Goal | None = None
        with self._write_session() as session:
            row = session.get(SqlGoal, goal_id)
            if row is None:
                return None
            if row.readiness_kind == "immediate":
                row.readiness_kind = "dependent"
            dep_row = SqlGoalDependency(
                id=f"goal_dep_{uuid.uuid4().hex}",
                goal_id=goal_id,
                kind=kind,
                ref=ref,
                label=label,
                status=status,
                created_at=now,
                updated_at=now,
                resolved_at=now if status != "pending" else None,
                meta=json.dumps(metadata) if metadata is not None else None,
            )
            session.add(dep_row)
            session.flush()
            self._refresh_activation(session, row, now)
            row.updated_at = now
            dependencies = self._dependency_rows(session, [goal_id]).get(goal_id, ())
            dependency = _to_dependency(dep_row)
            goal = _to_goal(row, dependencies)
        if goal is not None and dependency is not None:
            _publish_goal_event("dependency_added", goal, dependency=dependency, occurred_at=now)
        return dependency

    def update_dependency(
        self,
        *,
        goal_id: str,
        dependency_id: str,
        kind: str | object = _UNSET,
        label: str | object = _UNSET,
        ref: str | None | object = _UNSET,
        status: str | object = _UNSET,
        metadata: dict[str, Any] | None | object = _UNSET,
        now: int | None = None,
    ) -> GoalDependency | None:
        """Update a dependency and recalculate the owning goal's activation state."""
        now = now_epoch() if now is None else now
        dependency: GoalDependency | None = None
        goal: Goal | None = None
        with self._write_session() as session:
            goal_row = session.get(SqlGoal, goal_id)
            if goal_row is None:
                return None
            dep_row = session.get(SqlGoalDependency, dependency_id)
            if dep_row is None or dep_row.goal_id != goal_id:
                return None
            if kind is not _UNSET:
                dep_row.kind = _validate("kind", str(kind), DEPENDENCY_KINDS)
            if label is not _UNSET:
                cleaned = str(label).strip()
                if not cleaned:
                    raise ValueError("label is required")
                dep_row.label = cleaned
            if ref is not _UNSET:
                dep_row.ref = ref if ref is None else str(ref)
            if status is not _UNSET:
                dep_status = _validate("status", str(status), DEPENDENCY_STATUSES)
                dep_row.status = dep_status
                dep_row.resolved_at = now if dep_status != "pending" else None
            if metadata is not _UNSET:
                dep_row.meta = json.dumps(metadata) if metadata is not None else None
            dep_row.updated_at = now
            self._refresh_activation(session, goal_row, now)
            goal_row.updated_at = now
            dependencies = self._dependency_rows(session, [goal_id]).get(goal_id, ())
            dependency = _to_dependency(dep_row)
            goal = _to_goal(goal_row, dependencies)
        if goal is not None and dependency is not None:
            _publish_goal_event("dependency_updated", goal, dependency=dependency, occurred_at=now)
        return dependency

    def escalate_blocked(self, *, now: int | None = None) -> list[Goal]:
        """Claim not-yet-escalated ``blocked`` goals, marking them escalated (C4)."""
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            rows = (
                session.execute(
                    select(SqlGoal).where(
                        SqlGoal.status == "blocked",
                        SqlGoal.escalated_at.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            dependency_map = self._dependency_rows(session, [row.id for row in rows])
            snapshot = [_to_goal(r, dependency_map.get(r.id, ())) for r in rows]
            for row in rows:
                row.escalated_at = now
            session.flush()
        for goal in snapshot:
            _publish_goal_event("escalated", goal, occurred_at=now)
        return snapshot

    def reopen_stalled(
        self, *, older_than_seconds: int, now: int | None = None
    ) -> list[Goal]:
        """Rebalance: reopen owned goals idle past ``older_than_seconds`` (BDP-2272 C4)."""
        now = now_epoch() if now is None else now
        cutoff = now - older_than_seconds
        with self._write_session() as session:
            rows = (
                session.execute(
                    select(SqlGoal).where(
                        SqlGoal.status.in_(("assigned", "in_progress")),
                        SqlGoal.updated_at <= cutoff,
                    )
                )
                .scalars()
                .all()
            )
            dependency_map = self._dependency_rows(session, [row.id for row in rows])
            reopened = [_to_goal(r, dependency_map.get(r.id, ())) for r in rows]
            for r in rows:
                r.status = "open"
                r.owner_agent_id = None
                r.updated_at = now
            session.flush()
        for goal in reopened:
            _publish_goal_event("reopened", goal, occurred_at=now)
        return reopened

    def _dependency_rows(
        self, session: Any, goal_ids: Sequence[str]
    ) -> dict[str, list[SqlGoalDependency]]:
        if not goal_ids:
            return {}
        rows = (
            session.execute(
                select(SqlGoalDependency)
                .where(SqlGoalDependency.goal_id.in_(goal_ids))
                .order_by(SqlGoalDependency.created_at, SqlGoalDependency.id)
            )
            .scalars()
            .all()
        )
        out: dict[str, list[SqlGoalDependency]] = {}
        for row in rows:
            out.setdefault(row.goal_id, []).append(row)
        return out

    def _refresh_activation(self, session: Any, row: SqlGoal, now: int) -> None:
        dependencies = self._dependency_rows(session, [row.id]).get(row.id, ())
        activation_state = _activation_for(row.readiness_kind, _dependency_statuses(dependencies))
        if row.activation_state != activation_state:
            row.activation_state = activation_state
            row.updated_at = now

    # -- scoreboard ----------------------------------------------------
    def record_score(
        self,
        *,
        agent_id: str,
        metric: str,
        value: float,
        window: str = "all",
        now: int | None = None,
    ) -> None:
        """Upsert the latest value for ``(agent_id, metric, window)``."""
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            existing = session.get(SqlScoreboardEntry, (agent_id, metric, window))
            if existing is not None:
                existing.value = value
                existing.updated_at = now
            else:
                session.add(
                    SqlScoreboardEntry(
                        agent_id=agent_id,
                        metric=metric,
                        window=window,
                        value=value,
                        updated_at=now,
                    )
                )

    def scoreboard(
        self, *, metric: str, window: str = "all", limit: int = 10
    ) -> list[tuple[str, float]]:
        """Return ``(agent_id, value)`` ranked by value desc for a metric/window."""
        stmt = (
            select(SqlScoreboardEntry)
            .where(
                SqlScoreboardEntry.metric == metric,
                SqlScoreboardEntry.window == window,
            )
            .order_by(SqlScoreboardEntry.value.desc())
            .limit(limit)
        )
        with self._session() as session:
            return [(r.agent_id, r.value) for r in session.execute(stmt).scalars().all()]


_goal_store_cache: dict[str, SqlAlchemyGoalStore] = {}


def get_goal_store() -> SqlAlchemyGoalStore:
    """Return the durable goals/scoreboard store (BDP-2271 C3, ADR-0142)."""
    from omnigent.runtime import get_conversation_store

    location = get_conversation_store().storage_location
    store = _goal_store_cache.get(location)
    if store is None:
        store = SqlAlchemyGoalStore(location)
        _goal_store_cache[location] = store
    return store
