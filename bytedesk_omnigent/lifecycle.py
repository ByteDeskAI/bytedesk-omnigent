"""Durable-store lifecycle StrEnums + a generic state machine (BDP-2356, ADR-0142).

The omnigent-native durable stores (``tasks``/``goals``/``tool_steps``/``bus``/
``scheduler``/``deliberation``/``peer``) persist their status/kind fields as bare
strings and guard their UPDATE-WHERE-status transitions by hand. This module makes
those vocabularies **closed-set StrEnums** (each member's *value* is the exact
legacy wire string, so persisted rows and the cross-language ``/sources`` wire
shape are unchanged) and adds one generic :class:`LifecycleStateMachine` so a
store can declare its **legal transitions** once and reject genuinely-illegal ones
— catching e.g. ``done -> in_progress``, not just unknown values.

Wire compatibility is the hard contract: ``str(SomeStatus.OPEN) == "open"`` and
``SomeStatus("open") is SomeStatus.OPEN``. Coercing a raw DB string through the
enum (``WorkflowLifecycleStatus(row.status)``) round-trips to the same string on
write, so the schema and existing data are untouched.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import ClassVar, Generic, TypeVar


# ── Closed-set vocabularies (values == the legacy wire strings) ──────────────
class WorkflowLifecycleStatus(StrEnum):
    """Shared lifecycle status for both :class:`Task` and :class:`Goal` rows.

    Tasks and goals share the same backlog vocabulary — an ``open`` row is
    ``assigned`` by a guarded claim, worked (``in_progress``), may stall
    (``blocked``), and finishes ``done``. One enum unifies both substrates.
    """

    OPEN = "open"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"


class WaitStatus(StrEnum):
    """Status of a durable ``pending_waits`` row (the signal/await bus)."""

    PENDING = "pending"
    RESOLVED = "resolved"
    EXPIRED = "expired"


class WaitKind(StrEnum):
    """Kind of a durable registered await (``pending_waits.kind``).

    ``subscribe`` is the default await; ``release`` is the production-release
    orchestrator's TeamCity-callback wait. A new await kind is a one-line addition
    here (the route layer that registers caller-supplied kinds is a follow-up).
    """

    SUBSCRIBE = "subscribe"
    RELEASE = "release"


class StepStatus(StrEnum):
    """Persisted row state of a durable tool-step (``tool_steps.status``).

    Distinct from :class:`bytedesk_omnigent.tool_steps.store.StepOutcome`, which
    is the *verdict* of a ``begin()`` claim, not the persisted row state.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class DeliberationStatus(StrEnum):
    """Status of a deliberation (``deliberations.status``)."""

    OPEN = "open"
    DECIDED = "decided"


class Stance(StrEnum):
    """A position's stance in a deliberation (``deliberation_positions.stance``)."""

    FOR = "for"
    AGAINST = "against"
    AMEND = "amend"


class PeerMessageKind(StrEnum):
    """Kind of a lateral peer message (``peer_messages.kind``)."""

    DM = "dm"
    BROADCAST = "broadcast"
    ESCALATION = "escalation"


class ScheduleKind(StrEnum):
    """Cadence kind of a cron trigger (``cron_triggers.schedule_kind``)."""

    INTERVAL = "interval"
    CRON = "cron"
    ONCE = "once"


# ── Generic lifecycle state machine ─────────────────────────────────────────
TStatus = TypeVar("TStatus", bound=StrEnum)


class IllegalTransition(ValueError):
    """A status transition that the store's lifecycle does not permit."""

    def __init__(self, frm: StrEnum, to: StrEnum) -> None:
        super().__init__(f"illegal transition {str(frm)!r} -> {str(to)!r}")
        self.frm = frm
        self.to = to


class LifecycleStateMachine(Generic[TStatus]):
    """A declared set of legal status transitions for one StrEnum vocabulary.

    A store declares its legal moves once::

        class TaskLifecycle(LifecycleStateMachine[WorkflowLifecycleStatus]):
            transitions = {
                WorkflowLifecycleStatus.OPEN: frozenset({...}),
                ...
            }

    and its advance/update methods consult :meth:`can` (or :meth:`check`) to
    reject genuinely-illegal transitions — catching ``done -> in_progress``, not
    just unknown values. A status absent from ``transitions`` (or a self-loop) is
    treated as having no legal *outgoing* moves unless explicitly listed.
    """

    transitions: Mapping[TStatus, frozenset[TStatus]] = {}

    def can(self, frm: TStatus, to: TStatus) -> bool:
        """Return ``True`` iff ``frm -> to`` is a declared legal transition."""
        return to in self.transitions.get(frm, frozenset())

    def check(self, frm: TStatus, to: TStatus) -> None:
        """Raise :class:`IllegalTransition` unless ``frm -> to`` is legal."""
        if not self.can(frm, to):
            raise IllegalTransition(frm, to)


# ── Per-store transition tables ─────────────────────────────────────────────
class WorkflowLifecycle(LifecycleStateMachine[WorkflowLifecycleStatus]):
    """Legal moves for the shared task/goal backlog lifecycle.

    Mirrors the moves the stores already make: an ``open`` row is claimed
    (``assigned``) or worked directly; ``assigned``/``in_progress``/``blocked``
    rows progress, stall, finish, or are returned to the backlog (``open``,
    via ``advance_*_owned`` / ``reopen_stalled``); ``done`` is terminal.
    """

    _S = WorkflowLifecycleStatus
    transitions: ClassVar[Mapping[WorkflowLifecycleStatus, frozenset[WorkflowLifecycleStatus]]] = {
        _S.OPEN: frozenset({_S.ASSIGNED, _S.IN_PROGRESS, _S.BLOCKED, _S.DONE, _S.OPEN}),
        _S.ASSIGNED: frozenset({_S.IN_PROGRESS, _S.BLOCKED, _S.DONE, _S.OPEN, _S.ASSIGNED}),
        _S.IN_PROGRESS: frozenset({_S.BLOCKED, _S.DONE, _S.OPEN, _S.ASSIGNED, _S.IN_PROGRESS}),
        _S.BLOCKED: frozenset({_S.IN_PROGRESS, _S.DONE, _S.OPEN, _S.ASSIGNED, _S.BLOCKED}),
        _S.DONE: frozenset(),  # terminal
    }


class WaitLifecycle(LifecycleStateMachine[WaitStatus]):
    """Legal moves for a pending-wait: ``pending`` resolves or expires (terminal)."""

    _S = WaitStatus
    transitions: ClassVar[Mapping[WaitStatus, frozenset[WaitStatus]]] = {
        _S.PENDING: frozenset({_S.RESOLVED, _S.EXPIRED}),
        _S.RESOLVED: frozenset(),
        _S.EXPIRED: frozenset(),
    }


class StepLifecycle(LifecycleStateMachine[StepStatus]):
    """Legal moves for a durable tool-step row.

    ``running`` completes or (on a non-final failure) returns to ``pending`` for
    a retry, or fails terminally; a ``pending`` retry is re-claimed (``running``).
    """

    _S = StepStatus
    transitions: ClassVar[Mapping[StepStatus, frozenset[StepStatus]]] = {
        _S.PENDING: frozenset({_S.RUNNING, _S.FAILED}),
        _S.RUNNING: frozenset({_S.COMPLETED, _S.PENDING, _S.FAILED}),
        _S.COMPLETED: frozenset(),
        _S.FAILED: frozenset(),
    }


class DeliberationLifecycle(LifecycleStateMachine[DeliberationStatus]):
    """Legal moves for a deliberation: ``open`` decides (terminal)."""

    _S = DeliberationStatus
    transitions: ClassVar[Mapping[DeliberationStatus, frozenset[DeliberationStatus]]] = {
        _S.OPEN: frozenset({_S.DECIDED}),
        _S.DECIDED: frozenset(),
    }


__all__ = [
    "DeliberationLifecycle",
    "DeliberationStatus",
    "IllegalTransition",
    "LifecycleStateMachine",
    "PeerMessageKind",
    "ScheduleKind",
    "Stance",
    "StepLifecycle",
    "StepStatus",
    "WaitKind",
    "WaitLifecycle",
    "WaitStatus",
    "WorkflowLifecycle",
    "WorkflowLifecycleStatus",
]
